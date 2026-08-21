import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SKILLS = REPOSITORY / "skills"
ADAPTERS = REPOSITORY / ".agents" / "skills"
CANONICAL = SKILLS / "ssh-cluster-ops"
PROJECT_ADAPTER = ADAPTERS / "ssh-cluster-ops"
WEB_TERMINAL = SKILLS / "web-terminal-remote-dev"
WEB_TERMINAL_ADAPTER = ADAPTERS / "web-terminal-remote-dev"

# Every skill this repository distributes, and the project adapter that makes it
# discoverable from within this checkout.
DISTRIBUTED_SKILLS = ("ssh-cluster-ops", "web-terminal-remote-dev")


def frontmatter(path):
    content = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", content, re.DOTALL)
    if match is None:
        raise AssertionError(f"missing frontmatter: {path}")
    values = {}
    for line in match.group(1).splitlines():
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip().strip('"')
    return values


class SkillLayoutTests(unittest.TestCase):
    def test_entrypoints_have_standard_names_and_metadata(self):
        expected = {REPOSITORY / "SKILL.md": "codex-ssh-cluster-ops"}
        for skill in DISTRIBUTED_SKILLS:
            expected[SKILLS / skill / "SKILL.md"] = skill
            expected[ADAPTERS / skill / "SKILL.md"] = skill
        for path, name in expected.items():
            with self.subTest(path=path):
                metadata = frontmatter(path)
                self.assertEqual(metadata["name"], name)
                self.assertTrue(1 <= len(metadata["description"]) <= 1024)
                self.assertEqual(metadata["license"], "Apache-2.0")
                self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

    def test_every_distributed_skill_is_self_contained(self):
        for skill in DISTRIBUTED_SKILLS:
            directory = SKILLS / skill
            with self.subTest(skill=skill):
                self.assertTrue((directory / "SKILL.md").is_file())
                self.assertTrue((directory / "agents" / "openai.yaml").is_file())
                self.assertEqual(
                    (directory / "LICENSE").read_bytes(), (REPOSITORY / "LICENSE").read_bytes()
                )
                self.assertLess(
                    len((directory / "SKILL.md").read_text(encoding="utf-8").splitlines()), 500
                )

    def test_canonical_skill_is_standalone_and_references_exist(self):
        required = (
            "LICENSE",
            "agents/openai.yaml",
            "assets/policy.example.json",
            "references/configuration.md",
            "references/operations.md",
            "references/security-model.md",
            "scripts/clusterctl.py",
        )
        for relative in required:
            self.assertTrue((CANONICAL / relative).is_file(), relative)

    def test_web_terminal_skill_is_standalone_and_references_exist(self):
        required = (
            "LICENSE",
            "agents/openai.yaml",
            "references/webterm-internals.md",
            "scripts/podlib.js",
            "scripts/pod.js",
            "scripts/podpush.js",
            "scripts/podpull.js",
            "scripts/podtab.js",
            "scripts/podshot.js",
        )
        for relative in required:
            self.assertTrue((WEB_TERMINAL / relative).is_file(), relative)

    @unittest.skipIf(shutil.which("node") is None, "node is not installed")
    def test_web_terminal_scripts_parse(self):
        for script in sorted((WEB_TERMINAL / "scripts").glob("*.js")):
            with self.subTest(script=script.name):
                process = subprocess.run(
                    ["node", "--check", str(script)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(process.returncode, 0, process.stderr)

    def test_web_terminal_scripts_only_depend_on_the_node_standard_library(self):
        allowed = {"fs", "os", "path", "http", "https", "crypto", "child_process", "./podlib.js"}
        for script in sorted((WEB_TERMINAL / "scripts").glob("*.js")):
            text = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                for module in re.findall(r"require\('([^']+)'\)", text):
                    self.assertIn(module, allowed)

    def test_compatibility_files_point_to_the_canonical_skill(self):
        root_adapter = (REPOSITORY / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("skills/ssh-cluster-ops/SKILL.md", root_adapter)
        for skill in DISTRIBUTED_SKILLS:
            with self.subTest(skill=skill):
                adapter = (ADAPTERS / skill / "SKILL.md").read_text(encoding="utf-8")
                self.assertIn(f"../../../skills/{skill}/SKILL.md", adapter)
        self.assertEqual((REPOSITORY / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n")
        instructions = (REPOSITORY / "AGENTS.md").read_text(encoding="utf-8")
        for skill in DISTRIBUTED_SKILLS:
            self.assertIn(f"skills/{skill}/SKILL.md", instructions)

    def test_web_terminal_skill_defers_to_ssh_cluster_ops(self):
        # The browser channel is unauditable and policy-free, so both its
        # entrypoints must state that SSH is the preferred path.
        for path in (WEB_TERMINAL / "SKILL.md", WEB_TERMINAL_ADAPTER / "SKILL.md"):
            with self.subTest(path=path):
                self.assertIn("ssh-cluster-ops", path.read_text(encoding="utf-8"))

    def test_policy_templates_are_identical(self):
        self.assertEqual(
            (REPOSITORY / "assets" / "policy.example.json").read_bytes(),
            (CANONICAL / "assets" / "policy.example.json").read_bytes(),
        )

    def test_cli_entrypoints_work_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for cli in (REPOSITORY / "scripts" / "clusterctl.py", CANONICAL / "scripts" / "clusterctl.py"):
                with self.subTest(cli=cli):
                    process = subprocess.run(
                        [sys.executable, str(cli), "--help"],
                        cwd=directory,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertIn("validate-policy", process.stdout)
                    self.assertNotIn("{exec,", process.stdout)

    def test_distributed_packages_contain_no_private_project_context(self):
        forbidden = ("/Users/", "FastLTX", "AVForcing", "SwanLab", "MUSE", "B300", "H20")
        for skill in DISTRIBUTED_SKILLS:
            for path in (SKILLS / skill).rglob("*"):
                if not path.is_file() or path.name == "LICENSE" or "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8")
                with self.subTest(path=path):
                    for value in forbidden:
                        self.assertNotIn(value, text)

    def test_web_terminal_package_contains_no_private_hosts_or_addresses(self):
        # This skill was extracted from a private cluster session, so guard
        # against a hostname, address, or non-English working note leaking back
        # in.  ssh-cluster-ops is exempt: its policy template documents an
        # RFC 1918 example address on purpose.
        patterns = (
            re.compile(r"\b(?:10|172|192)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
            re.compile(r"[一-鿿]"),
        )
        for path in WEB_TERMINAL.rglob("*"):
            if not path.is_file() or path.name == "LICENSE":
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                for pattern in patterns:
                    self.assertIsNone(pattern.search(text), pattern.pattern)


if __name__ == "__main__":
    unittest.main()
