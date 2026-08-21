#!/usr/bin/env python3
"""Install the canonical SSH Cluster Ops skill for supported local agents."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple


SKILL_NAME = "ssh-cluster-ops"
SOURCE_DIR = Path(__file__).resolve().parent / "skills" / SKILL_NAME
AGENTS = ("agents", "codex", "claude", "cursor", "opencode")
SCOPES = ("user", "project")
MODES = ("copy", "symlink")

# These are each client's documented skill discovery roots.  The "agents"
# entry is the portable Agent Skills convention supported by several clients.
INSTALL_ROOTS = {
    "agents": {
        "user": (".agents", "skills"),
        "project": (".agents", "skills"),
    },
    "codex": {
        "user": (".codex", "skills"),
        "project": (".codex", "skills"),
    },
    "claude": {
        "user": (".claude", "skills"),
        "project": (".claude", "skills"),
    },
    "cursor": {
        "user": (".cursor", "skills"),
        "project": (".cursor", "skills"),
    },
    "opencode": {
        "user": (".config", "opencode", "skills"),
        "project": (".opencode", "skills"),
    },
}


class InstallError(RuntimeError):
    """A safe, user-actionable installation failure."""


@dataclass(frozen=True)
class InstallPlan:
    """The fully resolved local installation action."""

    agent: str
    scope: str
    mode: str
    source: Path
    target: Path
    replacing: bool


def _read_skill_name(skill_file: Path) -> str:
    try:
        details = skill_file.lstat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > (1 << 20):
            raise InstallError(f"SKILL.md must be a regular file no larger than 1 MiB: {skill_file}")
        content = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError(f"cannot read SKILL.md: {skill_file}") from exc
    frontmatter = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", content, re.DOTALL)
    if frontmatter is None:
        raise InstallError(f"SKILL.md has invalid frontmatter: {skill_file}")
    name = re.search(r"(?m)^name:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*$", frontmatter.group(1))
    if name is None:
        raise InstallError(f"SKILL.md has no valid name: {skill_file}")
    return name.group(1)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_real_directory_chain(path: Path, *, create: bool) -> None:
    """Reject symlink/non-directory parents and optionally create missing ones."""
    if not path.is_absolute():
        raise InstallError(f"install parent must be absolute: {path}")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            details = current.lstat()
        except FileNotFoundError:
            if not create:
                return
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            try:
                details = current.lstat()
            except FileNotFoundError as exc:
                raise InstallError(f"install parent changed while being created: {current}") from exc
        if stat.S_ISLNK(details.st_mode):
            raise InstallError(f"install parent must not contain a symlink: {current}")
        if not stat.S_ISDIR(details.st_mode):
            raise InstallError(f"install parent component is not a directory: {current}")


def _require_source(source_value: Path) -> Path:
    candidate = Path(source_value).expanduser()
    if candidate.is_symlink():
        raise InstallError(f"canonical source must be a real directory: {candidate}")
    try:
        source = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise InstallError(f"canonical source does not exist: {candidate}") from exc
    if not source.is_dir() or source.is_symlink():
        raise InstallError(f"canonical source must be a real directory: {source}")
    skill_file = source / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise InstallError(f"canonical source is missing a regular SKILL.md: {source}")
    if _read_skill_name(skill_file) != SKILL_NAME:
        raise InstallError(f"canonical SKILL.md name must be {SKILL_NAME}: {skill_file}")
    return source


def _project_root(project_dir: Optional[str], source: Path) -> Path:
    if not project_dir:
        raise InstallError("--project-dir is required when --scope project")
    project = Path(project_dir).expanduser()
    try:
        project = project.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InstallError(f"--project-dir does not exist: {project}") from exc
    if not project.is_dir():
        raise InstallError(f"--project-dir is not a directory: {project}")

    # The canonical layout is <repository>/skills/ssh-cluster-ops.  Installing
    # into the repository would make a copied skill recursively contain itself.
    repository = source.parent.parent
    if _is_within(project, repository):
        raise InstallError("--project-dir must be outside the source repository")
    return project


def resolve_install_target(
    agent: str,
    scope: str,
    *,
    project_dir: Optional[str] = None,
    home: Optional[Path] = None,
    source: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> Tuple[Path, Path]:
    """Return validated canonical source and exact target for one install."""
    if agent not in AGENTS:
        raise InstallError(f"unsupported agent: {agent}")
    if scope not in SCOPES:
        raise InstallError(f"unsupported scope: {scope}")
    if scope == "user" and project_dir is not None:
        raise InstallError("--project-dir is only valid when --scope project")

    source_dir = _require_source(SOURCE_DIR if source is None else source)
    if scope == "user":
        environment = os.environ if environment is None else environment
        base = Path.home() if home is None else Path(home).expanduser()
        base = base.resolve(strict=False)
        parts = INSTALL_ROOTS[agent][scope]
        configured_root = None
        if agent == "codex":
            configured_root = environment.get("CODEX_HOME")
            if configured_root:
                parts = ("skills",)
        elif agent == "opencode":
            configured_root = environment.get("XDG_CONFIG_HOME")
            if configured_root:
                parts = ("opencode", "skills")
        if configured_root:
            base = Path(configured_root).expanduser()
            if not base.is_absolute():
                raise InstallError("configured user skill root must be absolute")
            base = base.resolve(strict=False)
    else:
        base = _project_root(project_dir, source_dir)
        parts = INSTALL_ROOTS[agent][scope]
    target = base.joinpath(*parts, SKILL_NAME)
    _require_real_directory_chain(target.parent, create=False)

    # Do not permit a custom HOME or project path to turn installation into a
    # recursive copy of this checkout.
    repository = source_dir.parent.parent
    resolved_target = target.resolve(strict=False)
    if _is_within(resolved_target, repository):
        raise InstallError("installation target must be outside the source repository")
    return source_dir, target


def _target_exists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _target_identity(path: Path) -> Tuple[int, int, int]:
    details = path.lstat()
    return details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode)


def _validate_replace_target(path: Path, identity: Tuple[int, int, int]) -> None:
    if stat.S_ISLNK(identity[2]):
        return
    if not stat.S_ISDIR(identity[2]):
        raise InstallError(f"refusing to replace a non-directory target: {path}")
    if _read_skill_name(path / "SKILL.md") != SKILL_NAME:
        raise InstallError(f"refusing to replace a directory that is not {SKILL_NAME}: {path}")


def _remove_exact_target(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        raise InstallError(f"refusing to replace a non-directory target: {path}")


def _stage_install(source: Path, parent: Path, mode: str) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.install-", dir=str(parent)))
    staging.rmdir()
    try:
        if mode == "copy":
            shutil.copytree(
                source,
                staging,
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
            )
            staging.chmod(0o700)
        else:
            staging.symlink_to(source, target_is_directory=True)
        return staging
    except Exception:
        if _target_exists(staging):
            _remove_exact_target(staging)
        raise


def _publish_no_replace(staging: Path, target: Path) -> None:
    """Publish a staged directory or symlink without replacing target."""
    if staging.is_symlink():
        target.symlink_to(os.readlink(str(staging)), target_is_directory=True)
        staging.unlink()
        return

    final_mode = stat.S_IMODE(staging.stat().st_mode)
    target.mkdir(mode=0o700)
    created_identity = _target_identity(target)
    try:
        entries = list(staging.iterdir())
        entries.sort(key=lambda entry: (entry.name == "SKILL.md", entry.name))
        for entry in entries:
            # The private target directory was atomically reserved above.
            # Publish SKILL.md last so agents cannot discover a partial skill.
            os.replace(str(entry), str(target / entry.name))
        target.chmod(final_mode)
        staging.rmdir()
    except Exception:
        if _target_exists(target) and _target_identity(target) == created_identity:
            _remove_exact_target(target)
        raise


def _restore_no_replace(backup: Path, target: Path) -> None:
    """Restore an exact backup while preserving any concurrent target."""
    mode = _target_identity(backup)[2]
    if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        _publish_no_replace(backup, target)
        return
    if stat.S_ISREG(mode):
        os.link(str(backup), str(target), follow_symlinks=False)
        backup.unlink()
        return
    raise InstallError(f"cannot automatically restore special-file backup: {backup}")


def _install_staged(
    staging: Path,
    target: Path,
    expected_identity: Optional[Tuple[int, int, int]],
) -> None:
    backup: Optional[Path] = None
    try:
        if expected_identity is not None:
            backup = target.parent / (f".{SKILL_NAME}.backup-{uuid.uuid4().hex}")
            os.replace(str(target), str(backup))
            if _target_identity(backup) != expected_identity:
                try:
                    _restore_no_replace(backup, target)
                except Exception as exc:
                    raise InstallError(
                        f"target changed during replacement; unexpected target preserved at {backup}"
                    ) from exc
                raise InstallError("target changed during replacement; no files were replaced")
        _publish_no_replace(staging, target)
    except Exception as exc:
        if backup is not None and _target_exists(backup):
            if _target_exists(target):
                raise InstallError(
                    f"installation failed; original target preserved at {backup}: {exc}"
                ) from exc
            try:
                _restore_no_replace(backup, target)
            except Exception as restore_exc:
                raise InstallError(
                    f"installation failed and automatic restore failed; original target is at {backup}"
                ) from restore_exc
        raise
    finally:
        if _target_exists(staging):
            _remove_exact_target(staging)

    if backup is not None and _target_exists(backup):
        _remove_exact_target(backup)


def install_skill(
    agent: str,
    scope: str,
    *,
    project_dir: Optional[str] = None,
    mode: str = "copy",
    dry_run: bool = False,
    replace: bool = False,
    home: Optional[Path] = None,
    source: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> InstallPlan:
    """Install one exact target skill directory without touching any cluster."""
    if mode not in MODES:
        raise InstallError(f"unsupported mode: {mode}")
    source_dir, target = resolve_install_target(
        agent,
        scope,
        project_dir=project_dir,
        home=home,
        source=source,
        environment=environment,
    )
    exists = _target_exists(target)
    expected_identity = _target_identity(target) if exists else None
    if exists and not replace:
        raise InstallError(f"target already exists; rerun with --replace: {target}")
    if expected_identity is not None and replace:
        _validate_replace_target(target, expected_identity)

    plan = InstallPlan(agent, scope, mode, source_dir, target, exists and replace)
    if dry_run:
        return plan

    _require_real_directory_chain(target.parent, create=True)
    staging = _stage_install(source_dir, target.parent, mode)
    try:
        _require_real_directory_chain(target.parent, create=False)
        _install_staged(staging, target, expected_identity if plan.replacing else None)
    except Exception as exc:
        if isinstance(exc, InstallError):
            raise
        raise InstallError(f"failed to install {target}: {exc}") from exc
    return plan


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the canonical SSH Cluster Ops skill for a local coding agent."
    )
    parser.add_argument("--agent", choices=AGENTS, required=True)
    parser.add_argument("--scope", choices=SCOPES, required=True)
    parser.add_argument("--project-dir")
    parser.add_argument("--mode", choices=MODES, default="copy")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replace", action="store_true")
    return parser


def _format_plan(plan: InstallPlan, dry_run: bool) -> str:
    action = "would replace" if plan.replacing else "would install"
    if not dry_run:
        action = "replaced" if plan.replacing else "installed"
    prefix = "dry-run: " if dry_run else ""
    return "\n".join(
        (
            f"{prefix}{action}: {plan.target}",
            f"agent: {plan.agent}",
            f"scope: {plan.scope}",
            f"mode: {plan.mode}",
            f"source: {plan.source}",
        )
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = install_skill(
            args.agent,
            args.scope,
            project_dir=args.project_dir,
            mode=args.mode,
            dry_run=args.dry_run,
            replace=args.replace,
        )
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(_format_plan(plan, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
