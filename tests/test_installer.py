import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY / "install.py"
SPEC = importlib.util.spec_from_file_location("cluster_skill_installer", MODULE_PATH)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    # Subclassed once per canonical skill so every safety guarantee below is
    # asserted for each of them, not just the default.
    skill = installer.DEFAULT_SKILL

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.source = self.repository / "skills" / self.skill
        self.source.mkdir(parents=True)
        (self.source / "SKILL.md").write_text(
            f"---\nname: {self.skill}\ndescription: Test skill.\n---\n", encoding="utf-8"
        )
        (self.source / "scripts").mkdir()
        (self.source / "scripts" / "helper.py").write_text("print('ok')\n", encoding="utf-8")
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, agent, scope, **kwargs):
        return installer.install_skill(
            agent,
            scope,
            home=self.home,
            source=self.source,
            environment={},
            skill=self.skill,
            **kwargs,
        )

    def target(self, agent, scope):
        base = self.home if scope == "user" else self.project
        return base.joinpath(*installer.INSTALL_ROOTS[agent][scope], self.skill)

    def test_every_agent_scope_uses_the_documented_path(self):
        expected_roots = {
            "agents": {"user": (".agents", "skills"), "project": (".agents", "skills")},
            "codex": {"user": (".codex", "skills"), "project": (".codex", "skills")},
            "claude": {"user": (".claude", "skills"), "project": (".claude", "skills")},
            "cursor": {"user": (".cursor", "skills"), "project": (".cursor", "skills")},
            "opencode": {
                "user": (".config", "opencode", "skills"),
                "project": (".opencode", "skills"),
            },
        }
        self.assertEqual(installer.INSTALL_ROOTS, expected_roots)
        for agent in installer.AGENTS:
            for scope in installer.SCOPES:
                with self.subTest(agent=agent, scope=scope):
                    kwargs = {"project_dir": str(self.project)} if scope == "project" else {}
                    plan = self.install(agent, scope, **kwargs)
                    target = self.target(agent, scope)
                    self.assertEqual(plan.target, target)
                    self.assertTrue(target.is_dir())
                    self.assertFalse(target.is_symlink())
                    self.assertEqual(
                        (target / "SKILL.md").read_text(encoding="utf-8"),
                        (self.source / "SKILL.md").read_text(encoding="utf-8"),
                    )

    def test_missing_canonical_source_is_an_actionable_error(self):
        missing = self.root / "missing-source"
        with self.assertRaisesRegex(installer.InstallError, "canonical source does not exist"):
            installer.install_skill(
                "agents",
                "user",
                home=self.home,
                source=missing,
                skill=self.skill,
            )

    def test_canonical_source_name_must_match_install_name(self):
        (self.source / "SKILL.md").write_text(
            "---\nname: different-skill\ndescription: Test skill.\n---\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(installer.InstallError, f"name must be {self.skill}"):
            self.install("agents", "user")

    def test_symlink_canonical_source_is_rejected(self):
        source_link = self.root / "source-link"
        source_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError, "real directory"):
            installer.install_skill(
                "agents",
                "user",
                home=self.home,
                source=source_link,
                skill=self.skill,
            )

    def test_dry_run_creates_no_target_or_parent(self):
        plan = self.install("codex", "user", dry_run=True)
        self.assertEqual(plan.target, self.home / ".codex" / "skills" / self.skill)
        self.assertFalse(plan.target.exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_user_install_honors_codex_home_and_xdg_config_home(self):
        codex_root = self.root / "custom-codex"
        codex = installer.install_skill(
            "codex",
            "user",
            home=self.home,
            source=self.source,
            environment={"CODEX_HOME": str(codex_root)},
            skill=self.skill,
        )
        self.assertEqual(codex.target, codex_root / "skills" / self.skill)

        xdg_root = self.root / "custom-config"
        opencode = installer.install_skill(
            "opencode",
            "user",
            home=self.home,
            source=self.source,
            environment={"XDG_CONFIG_HOME": str(xdg_root)},
            skill=self.skill,
        )
        self.assertEqual(opencode.target, xdg_root / "opencode" / "skills" / self.skill)

    def test_configured_user_skill_root_must_be_absolute(self):
        with self.assertRaisesRegex(installer.InstallError, "must be absolute"):
            installer.install_skill(
                "codex",
                "user",
                home=self.home,
                source=self.source,
                environment={"CODEX_HOME": "relative/codex"},
                skill=self.skill,
            )

    def test_copy_install_copies_the_canonical_source(self):
        cache = self.source / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "helper.cpython-39.pyc").write_bytes(b"generated")
        plan = self.install("agents", "user", mode="copy")
        self.assertTrue(plan.target.is_dir())
        self.assertFalse(plan.target.is_symlink())
        self.assertEqual((plan.target / "scripts" / "helper.py").read_text(encoding="utf-8"), "print('ok')\n")
        self.assertFalse((plan.target / "scripts" / "__pycache__").exists())
        (plan.target / "SKILL.md").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual((self.source / "SKILL.md").read_text(encoding="utf-8"), "changed\n")

    def test_symlink_install_links_to_the_canonical_source(self):
        plan = self.install("claude", "user", mode="symlink")
        self.assertTrue(plan.target.is_symlink())
        self.assertEqual(plan.target.resolve(), self.source.resolve())
        self.assertEqual(os.readlink(plan.target), str(self.source.resolve()))

    def test_existing_target_is_refused_without_replace(self):
        plan = self.install("cursor", "user")
        sentinel = plan.target / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "target already exists"):
            self.install("cursor", "user")
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_symlinked_discovery_parent_is_rejected_without_external_write(self):
        outside = self.root / "outside"
        outside.mkdir()
        discovery = self.home / ".codex"
        discovery.mkdir(parents=True)
        (discovery / "skills").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(installer.InstallError, "must not contain a symlink"):
            self.install("codex", "user")
        self.assertEqual(list(outside.iterdir()), [])

    def test_replace_cannot_follow_a_symlinked_parent(self):
        outside = self.root / "outside"
        external_skill = outside / self.skill
        external_skill.mkdir(parents=True)
        (external_skill / "SKILL.md").write_text(
            f"---\nname: {self.skill}\ndescription: External skill.\n---\n", encoding="utf-8"
        )
        sentinel = external_skill / "keep.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        discovery = self.project / ".agents"
        discovery.mkdir()
        (discovery / "skills").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(installer.InstallError, "must not contain a symlink"):
            self.install("agents", "project", project_dir=str(self.project), replace=True)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep\n")

    def test_target_created_during_staging_is_not_overwritten(self):
        target = self.target("agents", "user")
        original_stage = installer._stage_install

        def stage_then_create_target(source, parent, mode, skill):
            staging = original_stage(source, parent, mode, skill)
            target.mkdir()
            (target / "concurrent.txt").write_text("keep\n", encoding="utf-8")
            return staging

        with mock.patch.object(installer, "_stage_install", side_effect=stage_then_create_target):
            with self.assertRaisesRegex(installer.InstallError, "failed to install"):
                self.install("agents", "user")
        self.assertEqual((target / "concurrent.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((target / "SKILL.md").exists())

    def test_replace_only_replaces_the_exact_target_directory(self):
        plan = self.install("opencode", "project", project_dir=str(self.project))
        (plan.target / "stale.txt").write_text("old\n", encoding="utf-8")
        sibling = plan.target.parent / "unrelated-sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("keep\n", encoding="utf-8")

        replaced = self.install("opencode", "project", project_dir=str(self.project), replace=True)
        self.assertTrue(replaced.replacing)
        self.assertFalse((replaced.target / "stale.txt").exists())
        self.assertTrue((replaced.target / "SKILL.md").is_file())
        self.assertEqual((sibling / "keep.txt").read_text(encoding="utf-8"), "keep\n")

    def test_replace_refuses_a_regular_file_at_the_exact_target(self):
        target = self.target("codex", "user")
        target.parent.mkdir(parents=True)
        target.write_text("not a skill directory\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "non-directory target"):
            self.install("codex", "user", replace=True)
        self.assertEqual(target.read_text(encoding="utf-8"), "not a skill directory\n")

    def test_replace_refuses_an_unrelated_directory_at_the_exact_target(self):
        target = self.target("agents", "user")
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("unrelated\n", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "cannot read SKILL.md"):
            self.install("agents", "user", replace=True)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "unrelated\n")

    def test_replace_restores_target_that_changes_type_during_staging(self):
        first = self.install("claude", "user")
        original_stage = installer._stage_install

        def stage_then_change_target(source, parent, mode, skill):
            staging = original_stage(source, parent, mode, skill)
            shutil.rmtree(first.target)
            first.target.write_text("concurrent file\n", encoding="utf-8")
            return staging

        with mock.patch.object(installer, "_stage_install", side_effect=stage_then_change_target):
            with self.assertRaisesRegex(installer.InstallError, "target changed during replacement"):
                self.install("claude", "user", replace=True)
        self.assertEqual(first.target.read_text(encoding="utf-8"), "concurrent file\n")
        self.assertEqual(list(first.target.parent.glob(f".{self.skill}.backup-*")), [])

    def test_project_inside_source_repository_is_rejected(self):
        unsafe_project = self.repository / "example-project"
        unsafe_project.mkdir()
        with self.assertRaisesRegex(installer.InstallError, "outside the source repository"):
            self.install("agents", "project", project_dir=str(unsafe_project))
        self.assertFalse((unsafe_project / ".agents").exists())

    def test_cli_parses_project_symlink_install(self):
        with mock.patch.object(installer, "SKILLS_DIR", self.source.parent), mock.patch.object(
            installer.Path, "home", return_value=self.home
        ):
            result = installer.main(
                [
                    "--agent",
                    "cursor",
                    "--scope",
                    "project",
                    "--skill",
                    self.skill,
                    "--project-dir",
                    str(self.project),
                    "--mode",
                    "symlink",
                ]
            )
        self.assertEqual(result, 0)
        self.assertTrue(self.target("cursor", "project").is_symlink())

    def test_user_scope_rejects_project_directory(self):
        with self.assertRaisesRegex(installer.InstallError, "only valid"):
            self.install("agents", "user", project_dir=str(self.project))

    def test_project_scope_requires_project_directory(self):
        with self.assertRaisesRegex(installer.InstallError, "required"):
            self.install("agents", "project")


class WebTerminalSkillInstallerTests(InstallerTests):
    """Run the whole installer suite against the second canonical skill."""

    skill = "web-terminal-remote-dev"


class MultiSkillTests(unittest.TestCase):
    """Guarantees that only hold across the set of canonical skills."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.skills_dir = self.root / "repository" / "skills"
        for skill in installer.SKILLS:
            source = self.skills_dir / skill
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: Test skill.\n---\n", encoding="utf-8"
            )
        self.home = self.root / "home"

    def tearDown(self):
        self.temporary.cleanup()

    def test_every_canonical_skill_ships_in_the_repository(self):
        for skill in installer.SKILLS:
            with self.subTest(skill=skill):
                self.assertTrue((REPOSITORY / "skills" / skill / "SKILL.md").is_file())

    def test_default_skill_is_a_canonical_skill(self):
        self.assertIn(installer.DEFAULT_SKILL, installer.SKILLS)

    def test_unknown_skill_is_refused_before_touching_the_filesystem(self):
        with self.assertRaisesRegex(installer.InstallError, "unsupported skill"):
            installer.install_skill(
                "agents",
                "user",
                home=self.home,
                source=self.skills_dir / installer.DEFAULT_SKILL,
                environment={},
                skill="not-a-skill",
            )
        self.assertFalse(self.home.exists())

    def test_skills_install_side_by_side_under_one_discovery_root(self):
        targets = []
        with mock.patch.object(installer, "SKILLS_DIR", self.skills_dir):
            for skill in installer.SKILLS:
                plan = installer.install_skill(
                    "agents", "user", home=self.home, environment={}, skill=skill
                )
                targets.append(plan.target)
        self.assertEqual(len(set(targets)), len(installer.SKILLS))
        for target, skill in zip(targets, installer.SKILLS):
            self.assertEqual(target.name, skill)
            self.assertIn(f"name: {skill}", (target / "SKILL.md").read_text(encoding="utf-8"))

    def test_replace_refuses_to_overwrite_a_different_installed_skill(self):
        with mock.patch.object(installer, "SKILLS_DIR", self.skills_dir):
            other = installer.install_skill(
                "claude", "user", home=self.home, environment={}, skill="web-terminal-remote-dev"
            )
            # Point the default skill's target at the other skill's directory to
            # simulate a stale or hand-edited install.
            impostor = other.target.parent / installer.DEFAULT_SKILL
            shutil.copytree(other.target, impostor)
            with self.assertRaisesRegex(
                installer.InstallError,
                f"not a directory that is not {installer.DEFAULT_SKILL}|"
                f"refusing to replace a directory that is not {installer.DEFAULT_SKILL}",
            ):
                installer.install_skill(
                    "claude",
                    "user",
                    home=self.home,
                    environment={},
                    skill=installer.DEFAULT_SKILL,
                    replace=True,
                )
        self.assertIn(
            "name: web-terminal-remote-dev", (impostor / "SKILL.md").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
