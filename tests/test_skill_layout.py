import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
CANONICAL = REPOSITORY / "skills" / "ssh-cluster-ops"
PROJECT_ADAPTER = REPOSITORY / ".agents" / "skills" / "ssh-cluster-ops"


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
        expected = {
            REPOSITORY / "SKILL.md": "codex-ssh-cluster-ops",
            CANONICAL / "SKILL.md": "ssh-cluster-ops",
            PROJECT_ADAPTER / "SKILL.md": "ssh-cluster-ops",
        }
        for path, name in expected.items():
            with self.subTest(path=path):
                metadata = frontmatter(path)
                self.assertEqual(metadata["name"], name)
                self.assertTrue(1 <= len(metadata["description"]) <= 1024)
                self.assertEqual(metadata["license"], "Apache-2.0")
                self.assertRegex(name, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

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
        self.assertEqual((CANONICAL / "LICENSE").read_bytes(), (REPOSITORY / "LICENSE").read_bytes())
        self.assertLess(len((CANONICAL / "SKILL.md").read_text(encoding="utf-8").splitlines()), 500)

    def test_compatibility_files_point_to_the_canonical_skill(self):
        root_adapter = (REPOSITORY / "SKILL.md").read_text(encoding="utf-8")
        project_adapter = (PROJECT_ADAPTER / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("skills/ssh-cluster-ops/SKILL.md", root_adapter)
        self.assertIn("../../../skills/ssh-cluster-ops/SKILL.md", project_adapter)
        self.assertEqual((REPOSITORY / "CLAUDE.md").read_text(encoding="utf-8"), "@AGENTS.md\n")
        self.assertIn("skills/ssh-cluster-ops/SKILL.md", (REPOSITORY / "AGENTS.md").read_text(encoding="utf-8"))

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

    def test_canonical_package_contains_no_private_project_context(self):
        forbidden = ("/Users/", "FastLTX", "AVForcing", "SwanLab", "MUSE", "B300", "H20")
        for path in CANONICAL.rglob("*"):
            if not path.is_file() or path.name == "LICENSE" or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                for value in forbidden:
                    self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
