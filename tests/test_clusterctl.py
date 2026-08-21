import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "clusterctl.py"
SPEC = importlib.util.spec_from_file_location("clusterctl", MODULE_PATH)
clusterctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = clusterctl
SPEC.loader.exec_module(clusterctl)


class PolicyFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text("example.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
        os.chmod(self.known_hosts, 0o600)
        self.audit_dir = self.root / "codex-ssh-cluster-ops"
        self.policy_path = self.root / "policy.json"
        self.data = {
            "schema_version": 1,
            "settings": {
                "connect_timeout_seconds": 10,
                "command_timeout_seconds": 120,
                "control_persist_seconds": 600,
                "control_path": str(self.root / "control" / "cm-%C"),
                "max_patch_bytes": 1048576,
                "max_command_bytes": 262144,
                "parallelism": 8,
                "audit_log": str(self.audit_dir / "audit.jsonl"),
                "known_hosts_file": str(self.known_hosts),
            },
            "hosts": {
                "gpu-a": {
                    "expected_user": "tester",
                    "expected_hostname": "10.0.0.10",
                    "expected_port": 22,
                    "expected_remote_hostname": "gpu-a",
                    "worktree_roots": ["/mnt/shared/code"],
                    "run_roots": ["/mnt/shared/runs"],
                    "allow_patch": True,
                    "allow_sessions": True,
                }
            },
        }
        self.write()

    def write(self):
        self.policy_path.write_text(json.dumps(self.data), encoding="utf-8")
        os.chmod(self.policy_path, 0o600)

    def load(self):
        return clusterctl.load_policy(str(self.policy_path))

    def close(self):
        self.temporary.cleanup()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PolicyFixture()

    def tearDown(self):
        self.fixture.close()

    def test_valid_policy_loads(self):
        policy = self.fixture.load()
        self.assertEqual(policy.hosts["gpu-a"].expected_hostname, "10.0.0.10")
        self.assertEqual(policy.settings.control_persist_seconds, 600)

    def test_unknown_policy_key_is_rejected(self):
        self.fixture.data["surprise"] = True
        self.fixture.write()
        with self.assertRaisesRegex(clusterctl.ClusterError, "unknown key"):
            self.fixture.load()

    def test_duplicate_json_key_is_rejected(self):
        self.fixture.policy_path.write_text(
            '{"schema_version":1,"schema_version":1,"settings":{},"hosts":{}}',
            encoding="utf-8",
        )
        os.chmod(self.fixture.policy_path, 0o600)
        with self.assertRaisesRegex(clusterctl.ClusterError, "duplicate JSON key"):
            self.fixture.load()

    def test_group_readable_policy_is_rejected(self):
        os.chmod(self.fixture.policy_path, 0o640)
        with self.assertRaisesRegex(clusterctl.ClusterError, "group or others"):
            self.fixture.load()

    def test_policy_symlink_is_rejected(self):
        link = self.fixture.root / "policy-link.json"
        link.symlink_to(self.fixture.policy_path)
        with self.assertRaisesRegex(clusterctl.ClusterError, "non-symlink"):
            clusterctl.load_policy(str(link))

    def test_unknown_host_and_bad_alias_are_rejected(self):
        policy = self.fixture.load()
        with self.assertRaisesRegex(clusterctl.ClusterError, "not allowlisted"):
            clusterctl._host(policy, "gpu-b")
        self.fixture.data["hosts"]["-bad"] = self.fixture.data["hosts"].pop("gpu-a")
        self.fixture.write()
        with self.assertRaisesRegex(clusterctl.ClusterError, "invalid SSH alias"):
            self.fixture.load()

    def test_control_path_other_token_is_rejected(self):
        self.fixture.data["settings"]["control_path"] = str(self.fixture.root / "control" / "%h-%C")
        self.fixture.write()
        with self.assertRaisesRegex(clusterctl.ClusterError, "no other '%' token"):
            self.fixture.load()

    def test_audit_log_cannot_alias_trust_files(self):
        self.fixture.data["settings"]["audit_log"] = str(self.fixture.known_hosts)
        self.fixture.write()
        with self.assertRaisesRegex(clusterctl.ClusterError, "must not be"):
            self.fixture.load()

    def test_local_policy_paths_are_normalized_before_alias_checks(self):
        self.fixture.data["settings"]["audit_log"] = str(
            self.fixture.root / "codex-ssh-cluster-ops" / ".." / "policy.json"
        )
        self.fixture.write()
        with self.assertRaisesRegex(clusterctl.ClusterError, "must not be"):
            self.fixture.load()

    def test_policy_and_command_inputs_reject_unsafe_modes_and_hardlinks(self):
        command = self.fixture.root / "command.sh"
        command.write_text("printf ok\n", encoding="utf-8")
        os.chmod(command, 0o666)
        with self.assertRaisesRegex(clusterctl.ClusterError, "writable by group or others"):
            clusterctl._load_command_file(str(command), 1000)
        os.chmod(command, 0o600)
        hardlink = self.fixture.root / "command-hardlink.sh"
        os.link(command, hardlink)
        with self.assertRaisesRegex(clusterctl.ClusterError, "exactly one hard link"):
            clusterctl._load_command_file(str(command), 1000)

    def test_remote_path_escape_is_rejected(self):
        policy = self.fixture.load()
        host = policy.hosts["gpu-a"]
        with self.assertRaisesRegex(clusterctl.ClusterError, "outside configured roots"):
            clusterctl._ensure_under_lexical("/mnt/shared/code-other/repo", host.worktree_roots, "repo")
        with self.assertRaisesRegex(clusterctl.ClusterError, "without '..'"):
            clusterctl._ensure_under_lexical("/mnt/shared/code/../secret", host.worktree_roots, "repo")


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PolicyFixture()
        self.policy = self.fixture.load()
        self.host = self.policy.hosts["gpu-a"]

    def tearDown(self):
        self.fixture.close()

    def test_strict_ssh_options_are_unconditionally_present(self):
        options = clusterctl._ssh_options(self.policy)
        joined = " ".join(options)
        for expected in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "ForwardAgent=no",
            "ClearAllForwardings=yes",
            "PermitLocalCommand=no",
            "RequestTTY=no",
            "UpdateHostKeys=no",
            "PasswordAuthentication=no",
            "KbdInteractiveAuthentication=no",
            f"UserKnownHostsFile={self.fixture.known_hosts}",
        ):
            self.assertIn(expected, joined)
        self.assertNotIn("accept-new", joined)

    @mock.patch.object(clusterctl, "_resolved_ssh_identity", return_value={})
    @mock.patch.object(clusterctl.subprocess, "run")
    def test_payload_is_stdin_not_ssh_argv(self, run, _identity):
        payload = b"secret patch $() ' quoted\n"
        run.return_value = subprocess.CompletedProcess([], 0, b"ok", b"")
        clusterctl._run_ssh(self.policy, self.host, payload, ["safe arg"])
        argv = run.call_args.args[0]
        self.assertNotIn(payload.decode(), " ".join(argv))
        self.assertEqual(run.call_args.kwargs["input"], payload)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertIn("'safe arg'", argv[-1])

    @mock.patch.object(clusterctl.subprocess, "run")
    def test_resolved_identity_mismatch_fails_closed(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, "hostname 10.0.0.99\nuser tester\nport 22\n", ""
        )
        with self.assertRaisesRegex(clusterctl.ClusterError, "identity mismatch"):
            clusterctl._resolved_ssh_identity(self.host)

    def test_remote_guard_uses_realpath_and_exact_worktree_root(self):
        guard = clusterctl._existing_path_guard()
        self.assertIn("realpath -e", guard)
        self.assertIn("rev-parse --show-toplevel", guard)
        self.assertIn('[[ "$top" == "$resolved" ]]', guard)

    def test_doctor_probes_realpath_capability_and_tmux_when_enabled(self):
        with mock.patch.object(clusterctl, "_audited") as audited:
            audited.side_effect = lambda _policy, _host, _action, _metadata, operation: operation()
            with mock.patch.object(clusterctl, "_run_ssh") as run_ssh:
                run_ssh.return_value = subprocess.CompletedProcess([], 0, b"ok", b"")
                alias, okay, _output = clusterctl._doctor_one(self.policy, self.host)
                self.assertEqual(alias, "gpu-a")
                self.assertTrue(okay)
                script = run_ssh.call_args.args[2].decode()
                arguments = run_ssh.call_args.args[3]
                self.assertIn("realpath -e -- /", script)
                self.assertIn("command -v tmux", script)
                self.assertEqual(arguments[-1], "1")

    def test_control_commands_are_exact_and_strict(self):
        with mock.patch.object(clusterctl, "_resolved_ssh_identity", return_value={}), mock.patch.object(
            clusterctl.subprocess, "run"
        ) as run:
            run.return_value = subprocess.CompletedProcess([], 0, "Master running", "")
            clusterctl._control_command(self.policy, self.host, "check")
            argv = run.call_args.args[0]
            self.assertIn("check", argv)
            self.assertEqual(argv[-1], "gpu-a")
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertFalse(run.call_args.kwargs["shell"])


class GitDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        (self.repo / "delete.txt").write_text("delete\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "base"], check=True)
        self.head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def tearDown(self):
        self.temporary.cleanup()

    def test_make_diff_captures_complete_worktree_without_touching_index(self):
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked.txt"], check=True)
        (self.repo / "tracked.txt").write_text("three\n", encoding="utf-8")
        (self.repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        (self.repo / "delete.txt").unlink()
        index_before = subprocess.check_output(["git", "-C", str(self.repo), "diff", "--cached", "--binary"])
        fixture = PolicyFixture()
        try:
            args = argparse.Namespace(
                local_repo=str(self.repo), expected_head=self.head, output=str(self.root / "review.diff")
            )
            clusterctl.command_make_diff(fixture.load(), args)
            diff = (self.root / "review.diff").read_bytes()
            self.assertIn(b"untracked.txt", diff)
            self.assertIn(b"delete.txt", diff)
            self.assertIn(b"+three", diff)
            index_after = subprocess.check_output(["git", "-C", str(self.repo), "diff", "--cached", "--binary"])
            self.assertEqual(index_before, index_after)
            _, expected_tree = clusterctl._local_expected_patch_tree(str(self.repo), self.head, diff)
            self.assertRegex(expected_tree, r"^[0-9a-f]{40,64}$")
        finally:
            fixture.close()

    def test_make_diff_never_overwrites(self):
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        output = self.root / "exists.diff"
        output.write_text("keep", encoding="utf-8")
        fixture = PolicyFixture()
        try:
            args = argparse.Namespace(local_repo=str(self.repo), expected_head=self.head, output=str(output))
            with self.assertRaisesRegex(clusterctl.ClusterError, "will not be overwritten"):
                clusterctl.command_make_diff(fixture.load(), args)
            self.assertEqual(output.read_text(encoding="utf-8"), "keep")
        finally:
            fixture.close()

    def test_noncanonical_or_stale_diff_is_rejected(self):
        (self.repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
        tree, diff = clusterctl._temporary_index_tree_and_diff(self.repo, self.head)
        self.assertTrue(tree)
        (self.repo / "untracked.txt").write_text("later\n", encoding="utf-8")
        with self.assertRaisesRegex(clusterctl.ClusterError, "complete current local worktree"):
            clusterctl._local_expected_patch_tree(str(self.repo), self.head, diff)

    def test_symlink_and_submodule_modes_are_rejected(self):
        for body in (
            b"diff --git a/link b/link\nindex 0123456..abcdef0 120000\n--- a/link\n+++ b/link\n@@ -1 +1 @@\n-old\n+new\n",
            b"diff --git a/sub b/sub\nindex 0123456..abcdef0 160000\n--- a/sub\n+++ b/sub\n@@ -1 +1 @@\n-Subproject commit a\n+Subproject commit b\n",
        ):
            path = self.root / ("bad-" + hashlib.sha256(body).hexdigest())
            path.write_bytes(body)
            with self.assertRaises(clusterctl.ClusterError):
                clusterctl._load_git_diff(str(path), 100000)

    def test_apply_script_checks_lock_head_tree_and_whitespace(self):
        diff = b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
        script = clusterctl._apply_diff_script(diff).decode()
        for expected in (
            "flock -n 9",
            "git -C \"$repo\" apply --check --whitespace=error-all",
            "calculated_tree",
            "post_tree",
            "git -C \"$repo\" diff --check",
            "guard_git_worktree",
        ):
            self.assertIn(expected, script)
        self.assertIn('exec 9<"$git_dir"', script)
        self.assertNotIn("codex-ssh-cluster-ops.lock", script)
        self.assertNotIn('exec 9>"', script)


class SessionAndAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PolicyFixture()
        self.policy = self.fixture.load()
        self.host = self.policy.hosts["gpu-a"]

    def tearDown(self):
        self.fixture.close()

    def test_session_names_and_stop_confirmation_are_exact(self):
        for bad in ("-bad", "bad/name", "bad name", "bad:session"):
            with self.assertRaises(clusterctl.ClusterError):
                clusterctl._validate_session_name(bad)
        args = argparse.Namespace(
            host="gpu-a", name="job", confirm_name="job-old", grace_seconds=1, force=False
        )
        with self.assertRaisesRegex(clusterctl.ClusterError, "exactly match"):
            clusterctl.command_session_stop(self.policy, args)

    def test_session_scripts_use_exact_targets_and_managed_marker(self):
        start = clusterctl._session_start_script(b"printf ok\n").decode()
        stop = clusterctl._session_simple_script("stop").decode()
        self.assertIn('tmux has-session -t "=$name"', start)
        self.assertIn('tmux set-option -w -t "=$name:0" remain-on-exit on', start)
        self.assertIn("@sshops_expected_tree", start)
        self.assertIn("actual_tree", start)
        self.assertIn("160000", start)
        self.assertIn("cleanup_start_failure", start)
        self.assertIn('tmux kill-session -t "=$name"', stop)
        self.assertIn("session is not managed by clusterctl", stop)
        self.assertNotIn("pkill", stop)
        self.assertNotIn("killall", stop)

    def test_missing_session_status_is_a_normal_state(self):
        status = clusterctl._session_simple_script("status").decode()
        self.assertIn("state=missing", status)
        self.assertGreater(status.index("exit 0"), status.index("state=missing"))

    def test_audit_omits_full_argv(self):
        script = clusterctl._audit_script(True).decode()
        self.assertIn("comm=", script)
        self.assertNotIn("args=", script)
        self.assertNotIn("cmdline", script)
        self.assertIn("sed -n '1,80p'", script)

    def test_audit_log_contains_hash_not_payload_and_is_private(self):
        payload = "DO_NOT_RECORD_COMMAND_CONTENT"
        result = clusterctl._audited(
            self.policy,
            self.host,
            "test-action",
            {"content_sha256": hashlib.sha256(payload.encode()).hexdigest()},
            lambda: "ok",
        )
        self.assertEqual(result, "ok")
        log = self.policy.settings.audit_log
        content = log.read_text(encoding="utf-8")
        self.assertNotIn(payload, content)
        records = [json.loads(line) for line in content.splitlines()]
        self.assertEqual([record["phase"] for record in records], ["start", "finish"])
        self.assertEqual(records[0]["invocation_id"], records[1]["invocation_id"])
        self.assertEqual(records[0]["actor_uid"], os.getuid())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_audit_actor_ignores_spoofed_login_environment(self):
        with mock.patch.dict(os.environ, {"LOGNAME": "forged", "USER": "forged"}):
            clusterctl._audit_event(self.policy, {"action": "actor-test"})
        record = json.loads(self.policy.settings.audit_log.read_text(encoding="utf-8"))
        self.assertNotEqual(record["actor"], "forged")
        self.assertEqual(record["actor_uid"], os.getuid())

    def test_cli_has_no_generic_exec_or_transfer_command(self):
        parser = clusterctl.build_parser()
        help_text = parser.format_help()
        for forbidden in (" upload", " download", " exec", " shell", " cleanup"):
            self.assertNotIn(forbidden, help_text)


if __name__ == "__main__":
    unittest.main()
