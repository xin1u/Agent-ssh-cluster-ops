import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "ssh-cluster-ops" / "scripts" / "clusterctl.py"
SPEC = importlib.util.spec_from_file_location("clusterctl_generic", MODULE_PATH)
clusterctl = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = clusterctl
SPEC.loader.exec_module(clusterctl)


class GenericRuntimeTests(unittest.TestCase):
    def test_new_policy_environment_variable_wins_over_legacy(self):
        self.assertEqual(
            clusterctl._default_policy_path(
                {
                    "SSH_CLUSTER_OPS_POLICY": "/new/policy.json",
                    "CODEX_SSH_CLUSTER_POLICY": "/legacy/policy.json",
                }
            ),
            "/new/policy.json",
        )

    def test_legacy_policy_environment_variable_is_a_fallback(self):
        self.assertEqual(
            clusterctl._default_policy_path({"CODEX_SSH_CLUSTER_POLICY": "/legacy/policy.json"}),
            "/legacy/policy.json",
        )

    def test_default_policy_prefers_existing_generic_path_then_legacy_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            generic = home / ".config" / "ssh-cluster-ops" / "policy.json"
            legacy = home / ".config" / "codex-ssh-cluster-ops" / "policy.json"
            generic.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            generic.write_text("{}", encoding="utf-8")
            legacy.write_text("{}", encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                self.assertEqual(clusterctl._default_policy_path({}), clusterctl.DEFAULT_POLICY_PATH)
            generic.unlink()
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                self.assertEqual(clusterctl._default_policy_path({}), clusterctl.LEGACY_DEFAULT_POLICY_PATH)
            legacy.unlink()
            with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
                self.assertEqual(clusterctl._default_policy_path({}), clusterctl.DEFAULT_POLICY_PATH)

    def test_both_audit_directory_names_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            known_hosts = root / "known_hosts"
            known_hosts.write_text("example.invalid ssh-ed25519 AAAATEST\n", encoding="utf-8")
            os.chmod(known_hosts, 0o600)
            for name in ("ssh-cluster-ops", "codex-ssh-cluster-ops"):
                policy_path = root / f"{name}.json"
                policy_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "settings": {
                                "connect_timeout_seconds": 10,
                                "command_timeout_seconds": 120,
                                "control_persist_seconds": 0,
                                "control_path": str(root / "control" / "cm-%C"),
                                "max_patch_bytes": 1024,
                                "max_command_bytes": 1024,
                                "parallelism": 1,
                                "audit_log": str(root / name / "audit.jsonl"),
                                "known_hosts_file": str(known_hosts),
                            },
                            "hosts": {
                                "gpu-a": {
                                    "expected_user": "tester",
                                    "expected_hostname": "10.0.0.10",
                                    "expected_port": 22,
                                    "expected_remote_hostname": "gpu-a",
                                    "worktree_roots": ["/mnt/code"],
                                    "run_roots": ["/mnt/runs"],
                                    "allow_patch": False,
                                    "allow_sessions": False,
                                }
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                os.chmod(policy_path, 0o600)
                self.assertEqual(clusterctl.load_policy(str(policy_path)).settings.audit_log.parent.name, name)

    def test_new_runtime_artifacts_are_generic_and_old_tmux_keys_are_readable(self):
        diff = b"diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
        apply_script = clusterctl._apply_diff_script(diff).decode("utf-8")
        start_script = clusterctl._session_start_script(b"printf ok\n").decode("utf-8")
        status_script = clusterctl._session_simple_script("status").decode("utf-8")
        log_script = clusterctl._session_simple_script("log").decode("utf-8")
        stop_script = clusterctl._session_simple_script("stop").decode("utf-8")

        for script in (apply_script, start_script):
            self.assertIn("SSHOPS_", script)
            self.assertNotIn("CODEX_", script)
        self.assertIn("sshops-patch.XXXXXX", apply_script)
        self.assertIn("sshops-index.XXXXXX", apply_script)
        self.assertIn("sshops-snapshot.XXXXXX", start_script)
        self.assertIn("@sshops_run_dir", start_script)
        self.assertIn("@sshops_expected_head", start_script)
        self.assertIn("@sshops_expected_tree", start_script)
        for script in (status_script, log_script, stop_script):
            self.assertIn("@sshops_run_dir", script)
            self.assertIn("@codex_run_dir", script)
        self.assertIn("@codex_expected_head", status_script)
        self.assertIn("@codex_expected_tree", status_script)

    def test_patch_wrapper_grammar_error_is_agent_neutral(self):
        with tempfile.TemporaryDirectory() as directory:
            patch = Path(directory) / "patch.diff"
            patch.write_text("*** Begin Patch\n*** Update File: a\n", encoding="utf-8")
            with self.assertRaisesRegex(clusterctl.ClusterError, "non-Git patch grammar") as raised:
                clusterctl._load_git_diff(str(patch), 1024)
            self.assertNotIn("Codex", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
