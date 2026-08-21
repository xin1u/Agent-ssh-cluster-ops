import getpass
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
CLI = SKILL_ROOT / "scripts" / "clusterctl.py"


FAKE_SSH = r'''#!/usr/bin/env python3
import os
import shlex
import subprocess
import sys

arguments = sys.argv[1:]
if "-G" in arguments:
    print("hostname " + os.environ["FAKE_SSH_HOSTNAME"])
    print("user " + os.environ["FAKE_SSH_USER"])
    print("port " + os.environ.get("FAKE_SSH_PORT", "22"))
    raise SystemExit(0)
remote = shlex.split(arguments[-1])
result = subprocess.run(remote, input=sys.stdin.buffer.read(), env=os.environ)
raise SystemExit(result.returncode)
'''


FAKE_TMUX = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

state_path = Path(os.environ["FAKE_TMUX_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"sessions": {}}
sessions = state["sessions"]
arguments = sys.argv[1:]
command = arguments[0]

def session_name(target):
    if target.startswith("="):
        target = target[1:]
    return target.split(":", 1)[0]

def target_value():
    return arguments[arguments.index("-t") + 1]

def save():
    state_path.write_text(json.dumps(state))

if command == "has-session":
    raise SystemExit(0 if session_name(target_value()) in sessions else 1)
if command == "new-session":
    name = arguments[arguments.index("-s") + 1]
    if name in sessions:
        raise SystemExit(1)
    sessions[name] = {"options": {}, "dead": "0", "exit": "", "command": "bash"}
    save()
    raise SystemExit(0)
if command == "set-option":
    name = session_name(target_value())
    key = arguments[arguments.index("-t") + 2]
    value = arguments[arguments.index("-t") + 3]
    sessions[name]["options"][key] = value
    save()
    raise SystemExit(0)
if command == "show-options":
    name = session_name(target_value())
    key = arguments[-1]
    print(sessions[name]["options"].get(key, ""))
    raise SystemExit(0)
if command == "respawn-pane":
    name = session_name(target_value())
    remote = shlex.split(arguments[-1])
    if remote and remote[0] == "exec":
        remote = remote[1:]
    result = subprocess.run(remote, env=os.environ)
    sessions[name]["dead"] = "1"
    sessions[name]["exit"] = str(result.returncode)
    sessions[name]["command"] = "bash"
    save()
    raise SystemExit(0)
if command == "list-panes":
    name = session_name(target_value())
    value = sessions[name]
    output = arguments[-1]
    output = output.replace("#{pane_id}", "%0")
    output = output.replace("#{pane_dead}", value["dead"])
    output = output.replace("#{pane_dead_status}", value["exit"])
    output = output.replace("#{pane_current_command}", value["command"])
    print(output)
    raise SystemExit(0)
if command == "display-message":
    name = session_name(target_value())
    token = arguments[-1]
    if token == "#{pane_dead}":
        print(sessions[name]["dead"])
    raise SystemExit(0)
if command == "send-keys":
    raise SystemExit(0)
if command == "kill-session":
    name = session_name(target_value())
    if name not in sessions:
        raise SystemExit(1)
    del sessions[name]
    save()
    raise SystemExit(0)
if command == "list-sessions":
    for name in sorted(sessions):
        print(name + "\tattached=0\tpanes=1")
    raise SystemExit(0)
raise SystemExit("unsupported fake tmux command: " + repr(arguments))
'''


class ForwardSimulationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self._write_executable("ssh", FAKE_SSH)
        self._write_executable("tmux", FAKE_TMUX)
        self._write_executable("flock", "#!/bin/sh\nexit 0\n")
        self._write_executable(
            "realpath",
            """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
arguments = [item for item in sys.argv[1:] if item not in ("-e", "--")]
if len(arguments) != 1 or not Path(arguments[0]).exists():
    raise SystemExit(1)
print(os.path.realpath(arguments[0]))
""",
        )
        self._write_executable(
            "nvidia-smi",
            "#!/bin/sh\ncase \"$*\" in *query-gpu*) echo '0, Fake GPU, GPU-FAKE, 80000, 0, 0, 30';; esac\n",
        )

        self.local_repo = self.root / "local"
        self.worktree_root = self.root / "worktrees"
        self.remote_repo = self.worktree_root / "remote"
        self.run_root = self.root / "runs"
        self.local_repo.mkdir()
        self.worktree_root.mkdir()
        self.run_root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.local_repo)], check=True)
        subprocess.run(["git", "-C", str(self.local_repo), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(self.local_repo), "config", "user.email", "test@example.invalid"], check=True
        )
        (self.local_repo / "code.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(self.local_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.local_repo), "commit", "-qm", "base"], check=True)
        subprocess.run(["git", "clone", "-q", str(self.local_repo), str(self.remote_repo)], check=True)
        self.head = subprocess.check_output(
            ["git", "-C", str(self.local_repo), "rev-parse", "HEAD"], text=True
        ).strip()

        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("fake ssh-ed25519 AAAATEST\n")
        os.chmod(self.known_hosts, 0o600)
        self.policy = self.root / "policy.json"
        policy_data = {
            "schema_version": 1,
            "settings": {
                "connect_timeout_seconds": 5,
                "command_timeout_seconds": 30,
                "control_persist_seconds": 0,
                "control_path": str(self.root / "control" / "cm-%C"),
                "max_patch_bytes": 1048576,
                "max_command_bytes": 262144,
                "parallelism": 2,
                "audit_log": str(self.root / "codex-ssh-cluster-ops" / "audit.jsonl"),
                "known_hosts_file": str(self.known_hosts),
            },
            "hosts": {
                "gpu-a": {
                    "expected_user": getpass.getuser(),
                    "expected_hostname": "127.0.0.1",
                    "expected_port": 22,
                    "expected_remote_hostname": socket.gethostname(),
                    "worktree_roots": [str(self.worktree_root)],
                    "run_roots": [str(self.run_root)],
                    "allow_patch": True,
                    "allow_sessions": True,
                }
            },
        }
        self.policy.write_text(json.dumps(policy_data))
        os.chmod(self.policy, 0o600)
        self.environment = os.environ.copy()
        self.environment["PATH"] = str(self.fake_bin) + os.pathsep + self.environment["PATH"]
        self.environment["FAKE_SSH_HOSTNAME"] = "127.0.0.1"
        self.environment["FAKE_SSH_USER"] = getpass.getuser()
        self.environment["FAKE_TMUX_STATE"] = str(self.root / "tmux-state.json")

    def tearDown(self):
        self.temporary.cleanup()

    def _write_executable(self, name, content):
        path = self.fake_bin / name
        path.write_text(textwrap.dedent(content))
        os.chmod(path, 0o700)

    def run_cli(self, *arguments):
        process = subprocess.run(
            [sys.executable, str(CLI), "--policy", str(self.policy), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment,
        )
        if process.returncode:
            self.fail(
                "clusterctl failed ({}):\nstdout:\n{}\nstderr:\n{}".format(
                    " ".join(arguments), process.stdout, process.stderr
                )
            )
        return process.stdout

    def test_full_forwarded_workflow(self):
        audit = self.run_cli("audit", "--host", "gpu-a", "--repo", str(self.remote_repo))
        self.assertIn("Fake GPU", audit)
        self.assertIn("dirty_entries=0", audit)

        patch_secret = "SIMULATED_PATCH_CONTENT_91a8"
        (self.local_repo / "code.txt").write_text(patch_secret + "\n")
        (self.local_repo / "new.txt").write_text("untracked is included\n")
        lock_target = self.root / "must-not-be-truncated.txt"
        lock_target.write_text("preserve me\n")
        (self.remote_repo / ".git" / "codex-ssh-cluster-ops.lock").symlink_to(lock_target)
        diff_path = self.root / "review.diff"
        self.run_cli(
            "make-diff",
            "--local-repo",
            str(self.local_repo),
            "--expected-head",
            self.head,
            "--output",
            str(diff_path),
        )
        apply_output = self.run_cli(
            "apply-diff",
            "--host",
            "gpu-a",
            "--local-repo",
            str(self.local_repo),
            "--remote-repo",
            str(self.remote_repo),
            "--expected-head",
            self.head,
            "--diff",
            str(diff_path),
        )
        self.assertIn("post_tree=", apply_output)
        self.assertEqual((self.remote_repo / "code.txt").read_text().strip(), patch_secret)
        self.assertTrue((self.remote_repo / "new.txt").is_file())
        self.assertEqual(lock_target.read_text(), "preserve me\n")

        command_secret = "SIMULATED_COMMAND_CONTENT_72b4"
        command_file = self.root / "command.sh"
        command_file.write_text("printf '%s\\n' '" + command_secret + "'\n")
        run_dir = self.run_root / "debug-r1"
        start = self.run_cli(
            "session-start",
            "--host",
            "gpu-a",
            "--local-repo",
            str(self.local_repo),
            "--remote-repo",
            str(self.remote_repo),
            "--run-dir",
            str(run_dir),
            "--name",
            "debug-r1",
            "--expected-head",
            self.head,
            "--command-file",
            str(command_file),
        )
        self.assertIn("session=debug-r1", start)
        status = self.run_cli("session-status", "--host", "gpu-a", "--name", "debug-r1")
        self.assertIn("dead=1", status)
        log = self.run_cli("session-log", "--host", "gpu-a", "--name", "debug-r1", "--lines", "20")
        self.assertIn(command_secret, log)

        state_path = Path(self.environment["FAKE_TMUX_STATE"])
        state = json.loads(state_path.read_text())
        state["sessions"]["debug-r1-old"] = {
            "options": {"@codex_run_dir": str(self.run_root)},
            "dead": "1",
            "exit": "0",
            "command": "bash",
        }
        state_path.write_text(json.dumps(state))
        legacy_status = self.run_cli(
            "session-status", "--host", "gpu-a", "--name", "debug-r1-old"
        )
        self.assertIn("state=present", legacy_status)
        self.assertIn(f"run_dir={self.run_root}", legacy_status)
        self.run_cli(
            "session-stop",
            "--host",
            "gpu-a",
            "--name",
            "debug-r1",
            "--confirm-name",
            "debug-r1",
            "--grace-seconds",
            "1",
        )
        final_state = json.loads(state_path.read_text())
        self.assertNotIn("debug-r1", final_state["sessions"])
        self.assertIn("debug-r1-old", final_state["sessions"])

        audit_log = (self.root / "codex-ssh-cluster-ops" / "audit.jsonl").read_text()
        self.assertNotIn(patch_secret, audit_log)
        self.assertNotIn(command_secret, audit_log)
        for line in audit_log.splitlines():
            json.loads(line)


if __name__ == "__main__":
    unittest.main()
