#!/usr/bin/env python3
"""Constrained SSH cluster operations using only the Python standard library."""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
DESTINATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HEAD_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
MAX_POLICY_BYTES = 1 << 20


class ClusterError(RuntimeError):
    """An expected, user-actionable policy or remote-operation failure."""


@dataclasses.dataclass(frozen=True)
class Settings:
    connect_timeout_seconds: int
    command_timeout_seconds: int
    control_persist_seconds: int
    control_path: Path
    max_patch_bytes: int
    max_command_bytes: int
    parallelism: int
    audit_log: Path
    known_hosts_file: Path


@dataclasses.dataclass(frozen=True)
class HostPolicy:
    alias: str
    expected_user: str
    expected_hostname: str
    expected_port: int
    expected_remote_hostname: str
    worktree_roots: Tuple[str, ...]
    run_roots: Tuple[str, ...]
    allow_patch: bool
    allow_sessions: bool


@dataclasses.dataclass(frozen=True)
class Policy:
    path: Path
    digest: str
    settings: Settings
    hosts: Mapping[str, HostPolicy]


def _strict_keys(value: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ClusterError(f"unknown key(s) in {where}: {', '.join(unknown)}")


def _require_dict(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ClusterError(f"{where} must be a JSON object")
    return value


def _no_duplicate_pairs(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ClusterError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_int(value: Any, where: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ClusterError(f"{where} must be an integer in [{minimum}, {maximum}]")
    return value


def _require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ClusterError(f"{where} must be true or false")
    return value


def _require_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ClusterError(f"{where} must be a non-empty single-line string")
    return value


def _expand_local_path(value: Any, where: str) -> Path:
    raw = _require_string(value, where)
    expanded = Path(os.path.expanduser(raw))
    if not expanded.is_absolute():
        raise ClusterError(f"{where} must be absolute after '~' expansion")
    return Path(os.path.abspath(expanded))


def _remote_path(value: Any, where: str) -> str:
    raw = _require_string(value, where)
    path = PurePosixPath(raw)
    if not path.is_absolute() or ".." in path.parts or raw == "/":
        raise ClusterError(f"{where} must be an absolute non-root path without '..'")
    return str(path)


def _read_validated_local_file(
    path: Path,
    label: str,
    max_bytes: int,
    *,
    require_private: bool,
) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ClusterError(f"{label} must be a readable regular, non-symlink file: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ClusterError(f"{label} must be a regular, non-symlink file: {path}")
        if before.st_uid != os.getuid():
            raise ClusterError(f"{label} must be owned by uid {os.getuid()}: {path}")
        if before.st_nlink != 1:
            raise ClusterError(f"{label} must have exactly one hard link: {path}")
        disallowed_mode = 0o077 if require_private else 0o022
        if stat.S_IMODE(before.st_mode) & disallowed_mode:
            requirement = "accessible by group or others" if require_private else "writable by group or others"
            raise ClusterError(f"{label} must not be {requirement}: {path}")
        if before.st_size > max_bytes:
            raise ClusterError(f"{label} exceeds the {max_bytes}-byte limit")
        chunks: List[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ClusterError(f"{label} exceeds the {max_bytes}-byte limit")
        after = os.fstat(descriptor)
        fingerprint_before = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        fingerprint_after = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if fingerprint_before != fingerprint_after or size != after.st_size:
            raise ClusterError(f"{label} changed while it was being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _check_known_hosts(path: Path) -> None:
    _read_validated_local_file(
        path,
        "known_hosts_file",
        64 << 20,
        require_private=False,
    )
    parent = path.parent
    info = parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ClusterError(f"known_hosts_file parent must be a real directory: {parent}")
    if info.st_uid != os.getuid() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ClusterError(f"known_hosts_file parent must be owned by the current user and not group/world writable: {parent}")


def load_policy(path_value: str) -> Policy:
    path = Path(os.path.abspath(os.path.expanduser(path_value)))
    raw = _read_validated_local_file(path, "policy", MAX_POLICY_BYTES, require_private=True)
    try:
        data = _require_dict(
            json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_pairs),
            "policy",
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClusterError(f"invalid UTF-8 JSON policy: {exc}") from exc
    _strict_keys(data, {"schema_version", "settings", "hosts"}, "policy")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ClusterError(f"schema_version must equal {SCHEMA_VERSION}")

    settings_data = _require_dict(data.get("settings"), "settings")
    settings_keys = {
        "connect_timeout_seconds",
        "command_timeout_seconds",
        "control_persist_seconds",
        "control_path",
        "max_patch_bytes",
        "max_command_bytes",
        "parallelism",
        "audit_log",
        "known_hosts_file",
    }
    _strict_keys(settings_data, settings_keys, "settings")
    missing_settings = sorted(settings_keys - set(settings_data))
    if missing_settings:
        raise ClusterError(f"missing setting(s): {', '.join(missing_settings)}")
    control_path = _expand_local_path(settings_data["control_path"], "settings.control_path")
    control_text = str(control_path)
    if control_text.count("%C") != 1 or "%" in control_text.replace("%C", ""):
        raise ClusterError("settings.control_path must contain exactly one '%C' token and no other '%' token")
    settings = Settings(
        connect_timeout_seconds=_require_int(
            settings_data["connect_timeout_seconds"], "settings.connect_timeout_seconds", 1, 120
        ),
        command_timeout_seconds=_require_int(
            settings_data["command_timeout_seconds"], "settings.command_timeout_seconds", 1, 86400
        ),
        control_persist_seconds=_require_int(
            settings_data["control_persist_seconds"], "settings.control_persist_seconds", 0, 86400
        ),
        control_path=control_path,
        max_patch_bytes=_require_int(settings_data["max_patch_bytes"], "settings.max_patch_bytes", 1, 64 << 20),
        max_command_bytes=_require_int(
            settings_data["max_command_bytes"], "settings.max_command_bytes", 1, 4 << 20
        ),
        parallelism=_require_int(settings_data["parallelism"], "settings.parallelism", 1, 64),
        audit_log=_expand_local_path(settings_data["audit_log"], "settings.audit_log"),
        known_hosts_file=_expand_local_path(
            settings_data["known_hosts_file"], "settings.known_hosts_file"
        ),
    )
    _check_known_hosts(settings.known_hosts_file)
    if settings.audit_log in {path, settings.known_hosts_file}:
        raise ClusterError("settings.audit_log must not be the policy or known_hosts file")
    if settings.audit_log.name != "audit.jsonl" or settings.audit_log.parent.name != "codex-ssh-cluster-ops":
        raise ClusterError("settings.audit_log must end in codex-ssh-cluster-ops/audit.jsonl")

    hosts_data = _require_dict(data.get("hosts"), "hosts")
    if not hosts_data:
        raise ClusterError("hosts must not be empty")
    host_keys = {
        "expected_user",
        "expected_hostname",
        "expected_port",
        "expected_remote_hostname",
        "worktree_roots",
        "run_roots",
        "allow_patch",
        "allow_sessions",
    }
    hosts: Dict[str, HostPolicy] = {}
    for alias, raw_host in hosts_data.items():
        if not isinstance(alias, str) or not HOST_RE.fullmatch(alias):
            raise ClusterError(f"invalid SSH alias: {alias!r}")
        host_data = _require_dict(raw_host, f"hosts.{alias}")
        _strict_keys(host_data, host_keys, f"hosts.{alias}")
        missing = sorted(host_keys - set(host_data))
        if missing:
            raise ClusterError(f"missing key(s) in hosts.{alias}: {', '.join(missing)}")
        user = _require_string(host_data["expected_user"], f"hosts.{alias}.expected_user")
        hostname = _require_string(host_data["expected_hostname"], f"hosts.{alias}.expected_hostname")
        remote_hostname = _require_string(
            host_data["expected_remote_hostname"], f"hosts.{alias}.expected_remote_hostname"
        )
        if not USER_RE.fullmatch(user):
            raise ClusterError(f"invalid expected_user for {alias}")
        if not DESTINATION_RE.fullmatch(hostname) or not DESTINATION_RE.fullmatch(remote_hostname):
            raise ClusterError(f"invalid expected hostname for {alias}")
        worktree_values = host_data["worktree_roots"]
        run_values = host_data["run_roots"]
        if not isinstance(worktree_values, list) or not worktree_values:
            raise ClusterError(f"hosts.{alias}.worktree_roots must be a non-empty array")
        if not isinstance(run_values, list) or not run_values:
            raise ClusterError(f"hosts.{alias}.run_roots must be a non-empty array")
        worktree_roots = tuple(
            _remote_path(item, f"hosts.{alias}.worktree_roots[{index}]")
            for index, item in enumerate(worktree_values)
        )
        run_roots = tuple(
            _remote_path(item, f"hosts.{alias}.run_roots[{index}]")
            for index, item in enumerate(run_values)
        )
        if len(set(worktree_roots)) != len(worktree_roots) or len(set(run_roots)) != len(run_roots):
            raise ClusterError(f"duplicate root in hosts.{alias}")
        hosts[alias] = HostPolicy(
            alias=alias,
            expected_user=user,
            expected_hostname=hostname,
            expected_port=_require_int(host_data["expected_port"], f"hosts.{alias}.expected_port", 1, 65535),
            expected_remote_hostname=remote_hostname,
            worktree_roots=worktree_roots,
            run_roots=run_roots,
            allow_patch=_require_bool(host_data["allow_patch"], f"hosts.{alias}.allow_patch"),
            allow_sessions=_require_bool(host_data["allow_sessions"], f"hosts.{alias}.allow_sessions"),
        )
    return Policy(
        path=path,
        digest=hashlib.sha256(raw).hexdigest(),
        settings=settings,
        hosts=hosts,
    )


def _host(policy: Policy, alias: str) -> HostPolicy:
    try:
        return policy.hosts[alias]
    except KeyError as exc:
        raise ClusterError(f"host is not allowlisted by policy: {alias}") from exc


def _resolved_ssh_identity(host: HostPolicy) -> Mapping[str, str]:
    process = subprocess.run(
        ["ssh", "-G", "--", host.alias],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
    )
    if process.returncode:
        raise ClusterError(f"ssh -G failed for {host.alias}: {process.stderr.strip()}")
    resolved: Dict[str, str] = {}
    for line in process.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in {"hostname", "user", "port"} and key not in resolved:
            resolved[key] = value.strip()
    expected = {
        "hostname": host.expected_hostname,
        "user": host.expected_user,
        "port": str(host.expected_port),
    }
    mismatches = [
        f"{key}={resolved.get(key)!r} (expected {value!r})"
        for key, value in expected.items()
        if resolved.get(key) != value
    ]
    if mismatches:
        raise ClusterError(f"resolved SSH identity mismatch for {host.alias}: " + "; ".join(mismatches))
    return resolved


def _ensure_control_dir(settings: Settings) -> None:
    directory = settings.control_path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ClusterError(f"ControlPath directory must be private and owned by the current user: {directory}")


def _ssh_options(policy: Policy, use_control: bool = True) -> List[str]:
    settings = policy.settings
    options = [
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={settings.known_hosts_file}",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-o", "RequestTTY=no",
        "-o", "UpdateHostKeys=no",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "ConnectionAttempts=1",
        "-o", f"ConnectTimeout={settings.connect_timeout_seconds}",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
    ]
    if use_control and settings.control_persist_seconds:
        _ensure_control_dir(settings)
        options.extend(
            [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPersist={settings.control_persist_seconds}",
                "-o", f"ControlPath={settings.control_path}",
            ]
        )
    else:
        options.extend(["-o", "ControlMaster=no"])
    return options


def _remote_command(arguments: Sequence[str]) -> str:
    return "bash -s -- " + " ".join(shlex.quote(argument) for argument in arguments)


def _run_ssh(
    policy: Policy,
    host: HostPolicy,
    script: bytes,
    arguments: Sequence[str] = (),
    timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    _resolved_ssh_identity(host)
    argv = ["ssh", *_ssh_options(policy), "--", host.alias, _remote_command(arguments)]
    try:
        process = subprocess.run(
            argv,
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or policy.settings.command_timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClusterError(f"SSH command timed out on {host.alias}") from exc
    if process.returncode:
        stderr = process.stderr.decode("utf-8", "replace").strip()
        raise ClusterError(f"remote operation failed on {host.alias} (exit {process.returncode}): {stderr}")
    return process


def _identity_prelude() -> str:
    return r'''set -euo pipefail
expected_user=$1
expected_host=$2
actual_user=$(id -un)
actual_host=$(hostname)
if [[ "$actual_user" != "$expected_user" || "$actual_host" != "$expected_host" ]]; then
  printf 'remote identity mismatch: user=%s host=%s\n' "$actual_user" "$actual_host" >&2
  exit 73
fi
'''


def _existing_path_guard() -> str:
    return r'''guard_existing_dir() {
  local requested=$1
  shift
  local resolved root root_resolved
  resolved=$(realpath -e -- "$requested") || return 74
  [[ -d "$resolved" ]] || return 74
  for root in "$@"; do
    root_resolved=$(realpath -e -- "$root") || continue
    [[ -d "$root_resolved" ]] || continue
    if [[ "$resolved" == "$root_resolved" || "$resolved" == "$root_resolved"/* ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  printf 'path escaped configured roots: %s\n' "$requested" >&2
  return 74
}
guard_git_worktree() {
  local requested=$1
  shift
  local resolved top
  resolved=$(guard_existing_dir "$requested" "$@") || return
  [[ "$(git -C "$resolved" rev-parse --is-bare-repository 2>/dev/null)" == false ]] || {
    printf 'path is not a non-bare Git worktree: %s\n' "$requested" >&2
    return 74
  }
  top=$(git -C "$resolved" rev-parse --show-toplevel 2>/dev/null) || return 74
  top=$(realpath -e -- "$top") || return 74
  [[ "$top" == "$resolved" ]] || {
    printf 'path must name the exact Git worktree root: %s\n' "$top" >&2
    return 74
  }
  printf '%s\n' "$resolved"
}
'''


def _ensure_under_lexical(path_value: str, roots: Sequence[str], label: str) -> str:
    path = _remote_path(path_value, label)
    candidate = PurePosixPath(path)
    for root_value in roots:
        root = PurePosixPath(root_value)
        if candidate == root or root in candidate.parents:
            return path
    raise ClusterError(f"{label} is outside configured roots: {path}")


def _validate_head(value: str) -> str:
    if not HEAD_RE.fullmatch(value):
        raise ClusterError("expected HEAD must be a full 40-64 digit hexadecimal object id")
    return value.lower()


def _validate_session_name(value: str) -> str:
    if not SESSION_RE.fullmatch(value):
        raise ClusterError("session name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    return value


def _private_parent(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ClusterError(f"audit directory must be a real directory: {path.parent}")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ClusterError(f"audit directory must be private and owned by the current user: {path.parent}")


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short write")
        remaining = remaining[written:]


def _audit_event(policy: Policy, event: Mapping[str, Any]) -> None:
    target = policy.settings.audit_log
    _private_parent(target)
    uid = os.getuid()
    try:
        actor = pwd.getpwuid(uid).pw_name
    except KeyError:
        actor = f"uid:{uid}"
    record = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "actor": actor,
        "actor_uid": uid,
        "local_hostname": socket.gethostname(),
        "policy_sha256": policy.digest,
        **event,
    }
    encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ClusterError(f"audit log must be a singly-linked regular file owned by the current user: {target}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ClusterError(f"audit log must not be accessible by group or others: {target}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        original_size = os.lseek(descriptor, 0, os.SEEK_END)
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
        except Exception:
            os.ftruncate(descriptor, original_size)
            os.fsync(descriptor)
            raise
    finally:
        os.close(descriptor)


def _audited(
    policy: Policy,
    host: HostPolicy,
    action: str,
    metadata: Mapping[str, Any],
    operation: Callable[[], Any],
) -> Any:
    started = time.monotonic()
    invocation_id = secrets.token_hex(16)
    base_event: Dict[str, Any] = {
        "invocation_id": invocation_id,
        "action": action,
        "host_alias": host.alias,
        "destination": f"{host.expected_user}@{host.expected_hostname}:{host.expected_port}",
        **metadata,
    }
    _audit_event(policy, {**base_event, "phase": "start"})
    try:
        result = operation()
    except Exception as exc:
        try:
            _audit_event(
                policy,
                {
                    **base_event,
                    "phase": "finish",
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
            )
        except Exception as audit_exc:
            raise ClusterError(
                f"operation failed and final audit record failed for invocation {invocation_id}: {audit_exc}"
            ) from exc
        raise
    try:
        _audit_event(
            policy,
            {
                **base_event,
                "phase": "finish",
                "outcome": "success",
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
    except Exception as exc:
        raise ClusterError(
            f"operation succeeded but final audit record failed for invocation {invocation_id}; do not retry: {exc}"
        ) from exc
    return result


def _decode(process: subprocess.CompletedProcess) -> str:
    return process.stdout.decode("utf-8", "replace").rstrip()


def command_validate_policy(policy: Policy, _args: argparse.Namespace) -> None:
    print(f"policy: {policy.path}")
    print(f"sha256: {policy.digest}")
    print(f"hosts: {', '.join(sorted(policy.hosts))}")
    print("status: valid")


def _selected_hosts(policy: Policy, aliases: Optional[Sequence[str]], all_hosts: bool) -> List[HostPolicy]:
    if all_hosts:
        if aliases:
            raise ClusterError("use either --all or --host, not both")
        return [policy.hosts[name] for name in sorted(policy.hosts)]
    if not aliases:
        raise ClusterError("at least one --host is required unless --all is used")
    if len(set(aliases)) != len(aliases):
        raise ClusterError("duplicate --host value")
    return [_host(policy, alias) for alias in aliases]


def _doctor_one(policy: Policy, host: HostPolicy) -> Tuple[str, bool, str]:
    script = (_identity_prelude() + r'''allow_sessions=$3
printf 'remote_user=%s\n' "$(id -un)"
printf 'remote_hostname=%s\n' "$(hostname)"
printf 'remote_time=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
command -v bash >/dev/null
command -v realpath >/dev/null
[[ "$(realpath -e -- /)" == / ]] || { printf 'GNU-compatible realpath -e is required\n' >&2; exit 89; }
command -v git >/dev/null
command -v flock >/dev/null
command -v sha256sum >/dev/null
if [[ "$allow_sessions" == 1 ]]; then command -v tmux >/dev/null; fi
''').encode()
    try:
        output = _audited(
            policy,
            host,
            "doctor",
            {},
            lambda: _decode(
                _run_ssh(
                    policy,
                    host,
                    script,
                    [
                        host.expected_user,
                        host.expected_remote_hostname,
                        "1" if host.allow_sessions else "0",
                    ],
                )
            ),
        )
        return host.alias, True, output
    except Exception as exc:
        return host.alias, False, str(exc)


def command_doctor(policy: Policy, args: argparse.Namespace) -> None:
    for executable in ("ssh", "git", "python3"):
        path = shutil.which(executable)
        if not path:
            raise ClusterError(f"required local executable not found: {executable}")
        print(f"local {executable}: {path}")
    hosts = _selected_hosts(policy, args.host, args.all)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(policy.settings.parallelism, len(hosts))) as pool:
        futures = [pool.submit(_doctor_one, policy, host) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            alias, okay, output = future.result()
            print(f"\n[{alias}] {'ok' if okay else 'FAILED'}")
            print(output)
            failures += int(not okay)
    if failures:
        raise ClusterError(f"doctor failed for {failures} host(s)")


def _audit_script(include_repo: bool) -> bytes:
    repo_section = r'''
repo=$3
shift 3
repo=$(guard_git_worktree "$repo" "$@")
printf '\n== git ==\n'
printf 'repo=%s\n' "$repo"
printf 'branch=%s\n' "$(git -C "$repo" symbolic-ref --quiet --short HEAD || printf DETACHED)"
printf 'head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
printf 'tree=%s\n' "$(git -C "$repo" rev-parse HEAD^{tree})"
printf 'upstream=%s\n' "$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || printf NONE)"
dirty=$(git -C "$repo" status --porcelain=v1 --untracked-files=all | wc -l | tr -d ' ')
printf 'dirty_entries=%s\n' "$dirty"
''' if include_repo else "shift 2\n"
    return (
        _identity_prelude()
        + _existing_path_guard()
        + r'''printf '== identity ==\n'
printf 'hostname=%s\n' "$(hostname)"
printf 'user=%s\n' "$(id -un)"
printf 'utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'uptime='; uptime
printf '\n== gpu ==\n'
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,uuid,memory.total,memory.used,utilization.gpu,temperature.gpu --format=csv,noheader,nounits || true
  printf '\n== gpu compute processes ==\n'
  nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv,noheader,nounits || true
else
  printf 'nvidia-smi unavailable\n'
fi
printf '\n== processes (argv intentionally omitted) ==\n'
ps -eo pid=,user=,comm=,%cpu=,%mem=,etime= | sed -n '1,80p'
printf '\n== tmux ==\n'
if command -v tmux >/dev/null 2>&1; then
  tmux list-sessions -F '#{session_name}\tattached=#{session_attached}\tpanes=#{session_panes}' 2>/dev/null || printf 'none\n'
else
  printf 'tmux unavailable\n'
fi
'''
        + repo_section
    ).encode()


def _audit_one(policy: Policy, host: HostPolicy, repo: Optional[str]) -> Tuple[str, bool, str]:
    metadata: Dict[str, Any] = {}
    arguments: List[str] = [host.expected_user, host.expected_remote_hostname]
    if repo is not None:
        repo = _ensure_under_lexical(repo, host.worktree_roots, "repo")
        metadata["remote_path"] = repo
        arguments.extend([repo, *host.worktree_roots])
    try:
        output = _audited(
            policy,
            host,
            "audit",
            metadata,
            lambda: _decode(_run_ssh(policy, host, _audit_script(repo is not None), arguments)),
        )
        return host.alias, True, output
    except Exception as exc:
        return host.alias, False, str(exc)


def command_audit(policy: Policy, args: argparse.Namespace) -> None:
    hosts = _selected_hosts(policy, args.host, args.all)
    failures = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(policy.settings.parallelism, len(hosts))) as pool:
        futures = [pool.submit(_audit_one, policy, host, args.repo) for host in hosts]
        for future in concurrent.futures.as_completed(futures):
            alias, okay, output = future.result()
            print(f"\n===== {alias}: {'ok' if okay else 'FAILED'} =====")
            print(output)
            failures += int(not okay)
    if failures:
        raise ClusterError(f"audit failed for {failures} host(s)")


def _git_local(repo: Path, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
    )
    if process.returncode:
        raise ClusterError(f"local git command failed: {process.stderr.strip()}")
    return process.stdout.rstrip()


def _local_repo_root(repo_value: str) -> Path:
    repo = Path(repo_value).expanduser().resolve()
    if not repo.is_dir():
        raise ClusterError(f"local repo is not a directory: {repo}")
    root = Path(_git_local(repo, "rev-parse", "--show-toplevel")).resolve()
    if root != repo:
        raise ClusterError(f"local repo must name the exact worktree root: {root}")
    return repo


def _temporary_index_tree_and_diff(repo: Path, expected_head: str) -> Tuple[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="codex-ssh-cluster-ops-") as directory:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        outputs: Dict[str, bytes] = {}
        operations: Sequence[Tuple[str, Sequence[str]]] = (
            ("read", ("git", "-C", str(repo), "read-tree", expected_head)),
            ("add", ("git", "-C", str(repo), "add", "-A", "--", ".")),
            ("tree", ("git", "-C", str(repo), "write-tree")),
            (
                "diff",
                (
                    "git",
                    "-C",
                    str(repo),
                    "diff",
                    "--cached",
                    "--binary",
                    "--full-index",
                    "--no-renames",
                    expected_head,
                ),
            ),
        )
        for label, argv in operations:
            process = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                shell=False,
            )
            if process.returncode:
                detail = process.stderr.decode("utf-8", "replace").strip()
                raise ClusterError(f"temporary-index {label} failed: {detail}")
            outputs[label] = process.stdout
        tree = outputs["tree"].decode("ascii", "strict").strip().lower()
        if not HEAD_RE.fullmatch(tree):
            raise ClusterError("temporary-index tree calculation returned malformed output")
        return tree, outputs["diff"]


def _reject_local_submodules(repo: Path) -> None:
    staged = _git_local(repo, "ls-files", "--stage")
    if any(line.startswith("160000 ") for line in staged.splitlines()):
        raise ClusterError("managed sessions do not support repositories containing Git submodules")


def _local_tree_state(repo_value: str, expected_head: str) -> Tuple[Path, str]:
    repo = _local_repo_root(repo_value)
    head = _git_local(repo, "rev-parse", "HEAD")
    if head.lower() != expected_head:
        raise ClusterError(f"local HEAD mismatch: {head} != {expected_head}")
    dirty = _git_local(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise ClusterError("local worktree must be clean")
    return repo, _git_local(repo, "rev-parse", "HEAD^{tree}")


def _remote_tree_state(policy: Policy, host: HostPolicy, repo: str, expected_head: str) -> Mapping[str, str]:
    script = (
        _identity_prelude()
        + _existing_path_guard()
        + r'''repo=$3
expected_head=$4
shift 4
repo=$(guard_git_worktree "$repo" "$@")
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null
head=$(git -C "$repo" rev-parse HEAD)
tree=$(git -C "$repo" rev-parse HEAD^{tree})
dirty=$(git -C "$repo" status --porcelain=v1 --untracked-files=all | wc -l | tr -d ' ')
[[ "$head" == "$expected_head" ]] || { printf 'HEAD mismatch: %s != %s\n' "$head" "$expected_head" >&2; exit 75; }
[[ "$dirty" == 0 ]] || { printf 'remote worktree is dirty (%s entries)\n' "$dirty" >&2; exit 76; }
printf 'head\t%s\n' "$head"
printf 'tree\t%s\n' "$tree"
printf 'dirty\t%s\n' "$dirty"
'''
    ).encode()
    output = _decode(
        _run_ssh(
            policy,
            host,
            script,
            [host.expected_user, host.expected_remote_hostname, repo, expected_head, *host.worktree_roots],
        )
    )
    result: Dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("\t")
        if separator:
            result[key] = value
    if set(result) != {"head", "tree", "dirty"}:
        raise ClusterError("remote tree verification returned malformed output")
    return result


def command_verify_tree(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    expected_head = _validate_head(args.expected_head)
    remote_repo = _ensure_under_lexical(args.remote_repo, host.worktree_roots, "remote_repo")
    local_repo, local_tree = _local_tree_state(args.local_repo, expected_head)

    def operation() -> Mapping[str, str]:
        return _remote_tree_state(policy, host, remote_repo, expected_head)

    remote = _audited(
        policy,
        host,
        "verify-tree",
        {"remote_path": remote_repo, "local_path": str(local_repo), "expected_head": expected_head},
        operation,
    )
    if remote["tree"].lower() != local_tree.lower():
        raise ClusterError(f"tree mismatch: local {local_tree}, remote {remote['tree']}")
    print(f"host: {host.alias}")
    print(f"head: {expected_head}")
    print(f"tree: {local_tree}")
    print("local: clean")
    print("remote: clean")


def _load_git_diff(path_value: str, limit: int) -> bytes:
    path = Path(os.path.abspath(os.path.expanduser(path_value)))
    data = _read_validated_local_file(path, "diff", limit, require_private=False)
    if not data:
        raise ClusterError(f"diff size must be in [1, {limit}] bytes")
    if b"\x00" in data:
        raise ClusterError("diff contains a NUL byte")
    if b"*** Begin Patch" in data or b"*** Update File:" in data:
        raise ClusterError("Codex apply_patch grammar is not accepted; use clusterctl make-diff")
    if not data.startswith(b"diff --git "):
        raise ClusterError("diff must start with a standard 'diff --git' header")
    if re.search(br"(?m)^(?:index [^\n]*|(?:new|deleted) file mode|(?:old|new) mode) (?:120000|160000)$", data):
        raise ClusterError("diff must not add or modify symlinks or Git submodules")
    if any(marker in data for marker in (b"Subproject commit ", b"\nrename from ", b"\nrename to ", b"\ncopy from ", b"\ncopy to ")):
        raise ClusterError("diff must not contain submodule, rename, or copy records")
    if not data.endswith(b"\n"):
        raise ClusterError("diff must end with a newline")
    return data


def _local_expected_patch_tree(repo_value: str, expected_head: str, diff: bytes) -> Tuple[Path, str]:
    repo = _local_repo_root(repo_value)
    head = _git_local(repo, "rev-parse", "HEAD")
    if head.lower() != expected_head:
        raise ClusterError(f"local HEAD mismatch: {head} != {expected_head}")
    with tempfile.TemporaryDirectory(prefix="codex-ssh-cluster-ops-") as directory:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        operations: Sequence[Tuple[Sequence[str], Optional[bytes]]] = (
            (("git", "-C", str(repo), "read-tree", expected_head), None),
            (("git", "-C", str(repo), "apply", "--cached", "--check", "--whitespace=error-all", "-"), diff),
            (("git", "-C", str(repo), "apply", "--cached", "--whitespace=error-all", "-"), diff),
            (("git", "-C", str(repo), "write-tree"), None),
        )
        tree = ""
        for argv, input_data in operations:
            process = subprocess.run(
                list(argv),
                input=input_data,
                stdin=subprocess.DEVNULL if input_data is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
                shell=False,
            )
            if process.returncode:
                detail = process.stderr.decode("utf-8", "replace").strip()
                raise ClusterError(f"diff is not valid against local expected HEAD: {detail}")
            if argv[-1] == "write-tree":
                tree = process.stdout.decode("ascii", "strict").strip().lower()
        if not HEAD_RE.fullmatch(tree):
            raise ClusterError("local temporary-index tree calculation returned malformed output")
    current_tree, current_diff = _temporary_index_tree_and_diff(repo, expected_head)
    if current_tree != tree:
        raise ClusterError("reviewed diff does not represent the complete current local worktree")
    if current_diff != diff:
        raise ClusterError("reviewed diff bytes differ from the canonical current local worktree diff")
    return repo, tree


def command_make_diff(policy: Policy, args: argparse.Namespace) -> None:
    expected_head = _validate_head(args.expected_head)
    repo = _local_repo_root(args.local_repo)
    head = _git_local(repo, "rev-parse", "HEAD").lower()
    if head != expected_head:
        raise ClusterError(f"local HEAD mismatch: {head} != {expected_head}")
    tree, diff = _temporary_index_tree_and_diff(repo, expected_head)
    if not diff:
        raise ClusterError("local worktree has no changes relative to expected HEAD")
    if len(diff) > policy.settings.max_patch_bytes:
        raise ClusterError(f"generated diff exceeds max_patch_bytes ({len(diff)} bytes)")
    with tempfile.NamedTemporaryFile(prefix="clusterctl-review-", delete=False) as temporary:
        temporary.write(diff)
        temporary_path = Path(temporary.name)
    try:
        canonical_diff = _load_git_diff(str(temporary_path), policy.settings.max_patch_bytes)
    finally:
        temporary_path.unlink(missing_ok=True)
    output = Path(os.path.abspath(os.path.expanduser(args.output)))
    if not output.parent.is_dir():
        raise ClusterError(f"output parent directory does not exist: {output.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags, 0o600)
    except FileExistsError as exc:
        raise ClusterError(f"output file already exists and will not be overwritten: {output}") from exc
    try:
        _write_all(descriptor, canonical_diff)
        os.fsync(descriptor)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    print(f"output: {output}")
    print(f"head: {expected_head}")
    print(f"expected_tree: {tree}")
    print(f"bytes: {len(canonical_diff)}")
    print(f"sha256: {hashlib.sha256(canonical_diff).hexdigest()}")


def _heredoc(payload: bytes, stem: str) -> Tuple[str, str]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClusterError(f"{stem} must be UTF-8") from exc
    while True:
        delimiter = f"CODEX_{stem.upper()}_{secrets.token_hex(16)}"
        if delimiter not in text.splitlines():
            return delimiter, text


def _apply_diff_script(diff: bytes) -> bytes:
    delimiter, text = _heredoc(diff, "diff")
    script = (
        _identity_prelude()
        + _existing_path_guard()
        + r'''repo=$3
expected_head=$4
expected_sha=$5
expected_tree=$6
shift 6
repo=$(guard_git_worktree "$repo" "$@")
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null
git_dir=$(git -C "$repo" rev-parse --absolute-git-dir)
[[ -d "$git_dir" && ! -L "$git_dir" ]] || { printf 'Git directory must be a real directory\n' >&2; exit 74; }
exec 9<"$git_dir"
flock -n 9 || { printf 'repository operation lock is busy\n' >&2; exit 77; }
head=$(git -C "$repo" rev-parse HEAD)
[[ "$head" == "$expected_head" ]] || { printf 'HEAD mismatch: %s != %s\n' "$head" "$expected_head" >&2; exit 75; }
[[ -z "$(git -C "$repo" status --porcelain=v1 --untracked-files=all)" ]] || { printf 'remote worktree is dirty\n' >&2; exit 76; }
patch_file=$(mktemp "$git_dir/codex-cluster-patch.XXXXXX")
index_file=$(mktemp "$git_dir/codex-cluster-index.XXXXXX")
rm -f -- "$index_file"
chmod 600 "$patch_file"
trap 'rm -f -- "$patch_file" "$index_file"' EXIT
cat >"$patch_file" <<''' + "'" + delimiter + "'\n" + text + delimiter + "\n" + r'''actual_sha=$(sha256sum "$patch_file" | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || { printf 'patch SHA256 mismatch\n' >&2; exit 78; }
git -C "$repo" apply --check --whitespace=error-all "$patch_file"
GIT_INDEX_FILE="$index_file" git -C "$repo" read-tree "$expected_head"
GIT_INDEX_FILE="$index_file" git -C "$repo" apply --cached --check --whitespace=error-all "$patch_file"
GIT_INDEX_FILE="$index_file" git -C "$repo" apply --cached --whitespace=error-all "$patch_file"
calculated_tree=$(GIT_INDEX_FILE="$index_file" git -C "$repo" write-tree)
[[ "$calculated_tree" == "$expected_tree" ]] || { printf 'expected result tree mismatch before apply\n' >&2; exit 86; }
git -C "$repo" apply --whitespace=error-all "$patch_file"
git -C "$repo" diff --check
rm -f -- "$index_file"
GIT_INDEX_FILE="$index_file" git -C "$repo" read-tree "$expected_head"
GIT_INDEX_FILE="$index_file" git -C "$repo" add -A -- .
post_tree=$(GIT_INDEX_FILE="$index_file" git -C "$repo" write-tree)
[[ "$post_tree" == "$expected_tree" ]] || { printf 'post-apply result tree mismatch\n' >&2; exit 87; }
printf 'head=%s\n' "$(git -C "$repo" rev-parse HEAD)"
printf 'dirty_entries=%s\n' "$(git -C "$repo" status --porcelain=v1 --untracked-files=all | wc -l | tr -d ' ')"
printf 'post_tree=%s\n' "$post_tree"
printf 'post_diff_sha256=%s\n' "$(GIT_INDEX_FILE="$index_file" git -C "$repo" diff --cached --binary --full-index HEAD | sha256sum | awk '{print $1}')"
GIT_INDEX_FILE="$index_file" git -C "$repo" diff --cached --stat --no-ext-diff HEAD
'''
    )
    return script.encode("utf-8")


def command_apply_diff(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    if not host.allow_patch:
        raise ClusterError(f"patching is disabled for host {host.alias}")
    expected_head = _validate_head(args.expected_head)
    repo = _ensure_under_lexical(args.remote_repo, host.worktree_roots, "remote_repo")
    diff = _load_git_diff(args.diff, policy.settings.max_patch_bytes)
    local_repo, expected_tree = _local_expected_patch_tree(args.local_repo, expected_head, diff)
    digest = hashlib.sha256(diff).hexdigest()
    script = _apply_diff_script(diff)

    output = _audited(
        policy,
        host,
        "apply-diff",
        {
            "remote_path": repo,
            "local_path": str(local_repo),
            "expected_head": expected_head,
            "expected_tree": expected_tree,
            "content_sha256": digest,
            "bytes": len(diff),
        },
        lambda: _decode(
            _run_ssh(
                policy,
                host,
                script,
                [
                    host.expected_user,
                    host.expected_remote_hostname,
                    repo,
                    expected_head,
                    digest,
                    expected_tree,
                    *host.worktree_roots,
                ],
            )
        ),
    )
    print(f"host: {host.alias}")
    print(f"patch_sha256: {digest}")
    print(f"expected_tree: {expected_tree}")
    print(output)


def _load_command_file(path_value: str, limit: int) -> bytes:
    path = Path(os.path.abspath(os.path.expanduser(path_value)))
    data = _read_validated_local_file(path, "command file", limit, require_private=False)
    if not data:
        raise ClusterError(f"command file size must be in [1, {limit}] bytes")
    if b"\x00" in data:
        raise ClusterError("command file contains a NUL byte")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClusterError("command file must be UTF-8") from exc
    return data if data.endswith(b"\n") else data + b"\n"


def _session_start_script(command: bytes) -> bytes:
    delimiter, text = _heredoc(command, "command")
    script = (
        _identity_prelude()
        + _existing_path_guard()
        + r'''repo=$3
run_dir=$4
name=$5
expected_head=$6
expected_sha=$7
expected_tree=$8
worktree_count=$9
shift 9
worktree_roots=("${@:1:$worktree_count}")
shift "$worktree_count"
run_roots=("$@")
repo=$(guard_git_worktree "$repo" "${worktree_roots[@]}")
command -v tmux >/dev/null
command -v flock >/dev/null
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null
if git -C "$repo" ls-files --stage | awk '$1 == "160000" { found=1 } END { exit !found }'; then
  printf 'managed sessions do not support repositories containing Git submodules\n' >&2
  exit 88
fi
git_dir=$(git -C "$repo" rev-parse --absolute-git-dir)
[[ -d "$git_dir" && ! -L "$git_dir" ]] || { printf 'Git directory must be a real directory\n' >&2; exit 74; }
exec 9<"$git_dir"
flock -n 9 || { printf 'repository operation lock is busy\n' >&2; exit 77; }
head=$(git -C "$repo" rev-parse HEAD)
[[ "$head" == "$expected_head" ]] || { printf 'HEAD mismatch: %s != %s\n' "$head" "$expected_head" >&2; exit 75; }
snapshot_index=$(mktemp "$git_dir/codex-cluster-snapshot.XXXXXX")
rm -f -- "$snapshot_index"
trap 'rm -f -- "$snapshot_index"' EXIT
GIT_INDEX_FILE="$snapshot_index" git -C "$repo" read-tree "$expected_head"
GIT_INDEX_FILE="$snapshot_index" git -C "$repo" add -A -- .
actual_tree=$(GIT_INDEX_FILE="$snapshot_index" git -C "$repo" write-tree)
[[ "$actual_tree" == "$expected_tree" ]] || {
  printf 'remote worktree tree mismatch: %s != %s\n' "$actual_tree" "$expected_tree" >&2
  exit 76
}
if tmux has-session -t "=$name" 2>/dev/null; then
  printf 'exact tmux session already exists: %s\n' "$name" >&2
  exit 79
fi
[[ ! -e "$run_dir" ]] || { printf 'run directory already exists: %s\n' "$run_dir" >&2; exit 80; }
parent=$(dirname -- "$run_dir")
parent_resolved=$(realpath -e -- "$parent") || { printf 'run parent does not exist\n' >&2; exit 81; }
allowed=0
for root in "${run_roots[@]}"; do
  root_resolved=$(realpath -e -- "$root") || continue
  [[ -d "$root_resolved" ]] || continue
  if [[ "$parent_resolved" == "$root_resolved" || "$parent_resolved" == "$root_resolved"/* ]]; then allowed=1; break; fi
done
[[ "$allowed" == 1 ]] || { printf 'run directory escaped configured roots\n' >&2; exit 74; }
session_created=0
cleanup_start_failure() {
  status=$?
  rm -f -- "$snapshot_index"
  if [[ "$status" != 0 ]]; then
    if [[ "$session_created" == 1 ]] && tmux has-session -t "=$name" 2>/dev/null; then
      tmux kill-session -t "=$name" || true
    fi
  fi
  return "$status"
}
trap cleanup_start_failure EXIT
mkdir -m 700 -- "$run_dir"
command_file=$run_dir/command.sh
runner=$run_dir/runner.sh
log_file=$run_dir/session.log
status_file=$run_dir/exit.status
cat >"$command_file" <<''' + "'" + delimiter + "'\n" + text + delimiter + "\n" + r'''chmod 700 "$command_file"
actual_sha=$(sha256sum "$command_file" | awk '{print $1}')
[[ "$actual_sha" == "$expected_sha" ]] || { printf 'command SHA256 mismatch\n' >&2; exit 78; }
cat >"$runner" <<'CODEX_RUNNER'
#!/usr/bin/env bash
set -uo pipefail
repo=$1
command_file=$2
log_file=$3
status_file=$4
cd -- "$repo" || exit 90
set +e
bash "$command_file" >"$log_file" 2>&1
status=$?
printf '%s\n' "$status" >"$status_file"
exit "$status"
CODEX_RUNNER
chmod 700 "$runner"
: >"$log_file"
chmod 600 "$log_file"
printf '{"expected_head":"%s","expected_tree":"%s","command_sha256":"%s"}\n' "$expected_head" "$expected_tree" "$expected_sha" >"$run_dir/metadata.json"
chmod 600 "$run_dir/metadata.json"
tmux new-session -d -s "$name" -c "$repo"
session_created=1
tmux set-option -w -t "=$name:0" remain-on-exit on
tmux set-option -t "=$name" @codex_run_dir "$run_dir"
tmux set-option -t "=$name" @codex_expected_head "$expected_head"
tmux set-option -t "=$name" @codex_expected_tree "$expected_tree"
printf -v runner_q '%q' "$runner"
printf -v repo_q '%q' "$repo"
printf -v command_q '%q' "$command_file"
printf -v log_q '%q' "$log_file"
printf -v status_q '%q' "$status_file"
tmux respawn-pane -k -t "=$name:0.0" "exec bash $runner_q $repo_q $command_q $log_q $status_q"
session_created=0
trap - EXIT
rm -f -- "$snapshot_index"
printf 'session=%s\nrun_dir=%s\nhead=%s\ntree=%s\ncommand_sha256=%s\n' "$name" "$run_dir" "$expected_head" "$expected_tree" "$expected_sha"
'''
    )
    return script.encode("utf-8")


def command_session_start(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    if not host.allow_sessions:
        raise ClusterError(f"session operations are disabled for host {host.alias}")
    name = _validate_session_name(args.name)
    expected_head = _validate_head(args.expected_head)
    repo = _ensure_under_lexical(args.remote_repo, host.worktree_roots, "remote_repo")
    run_dir = _ensure_under_lexical(args.run_dir, host.run_roots, "run_dir")
    local_repo = _local_repo_root(args.local_repo)
    _reject_local_submodules(local_repo)
    local_head = _git_local(local_repo, "rev-parse", "HEAD").lower()
    if local_head != expected_head:
        raise ClusterError(f"local HEAD mismatch: {local_head} != {expected_head}")
    expected_tree, _ = _temporary_index_tree_and_diff(local_repo, expected_head)
    command = _load_command_file(args.command_file, policy.settings.max_command_bytes)
    digest = hashlib.sha256(command).hexdigest()
    script = _session_start_script(command)
    arguments = [
        host.expected_user,
        host.expected_remote_hostname,
        repo,
        run_dir,
        name,
        expected_head,
        digest,
        expected_tree,
        str(len(host.worktree_roots)),
        *host.worktree_roots,
        *host.run_roots,
    ]
    output = _audited(
        policy,
        host,
        "session-start",
        {
            "session_name": name,
            "remote_path": repo,
            "run_dir": run_dir,
            "local_path": str(local_repo),
            "expected_head": expected_head,
            "expected_tree": expected_tree,
            "content_sha256": digest,
            "bytes": len(command),
        },
        lambda: _decode(_run_ssh(policy, host, script, arguments)),
    )
    print(output)


def _session_simple_script(operation: str, lines: int = 0, force: bool = False, grace: int = 0) -> bytes:
    base = _identity_prelude() + r'''name=$3
command -v tmux >/dev/null
'''
    if operation == "status":
        body = _existing_path_guard() + r'''shift 3
if ! tmux has-session -t "=$name" 2>/dev/null; then
  printf 'session=%s\nstate=missing\n' "$name"
  exit 0
fi
run_dir=$(tmux show-options -qv -t "=$name" @codex_run_dir)
[[ -n "$run_dir" ]] || { printf 'session is not managed by clusterctl\n' >&2; exit 85; }
run_dir=$(guard_existing_dir "$run_dir" "$@")
printf 'session=%s\nstate=present\n' "$name"
printf 'run_dir=%s\n' "$run_dir"
printf 'expected_head=%s\n' "$(tmux show-options -qv -t "=$name" @codex_expected_head)"
printf 'expected_tree=%s\n' "$(tmux show-options -qv -t "=$name" @codex_expected_tree)"
tmux list-panes -t "=$name" -F 'pane=#{pane_id} dead=#{pane_dead} exit=#{pane_dead_status} command=#{pane_current_command}'
'''
    elif operation == "log":
        body = _existing_path_guard() + r'''line_count=$4
shift 4
if ! tmux has-session -t "=$name" 2>/dev/null; then printf 'session missing\n' >&2; exit 82; fi
run_dir=$(tmux show-options -qv -t "=$name" @codex_run_dir)
[[ -n "$run_dir" ]] || { printf 'session is not managed by clusterctl\n' >&2; exit 85; }
run_dir=$(guard_existing_dir "$run_dir" "$@")
log_file=$run_dir/session.log
[[ -f "$log_file" && ! -L "$log_file" ]] || { printf 'session log unavailable\n' >&2; exit 83; }
tail -n "$line_count" -- "$log_file"
'''
    elif operation == "stop":
        body = _existing_path_guard() + r'''grace=$4
force=$5
shift 5
if ! tmux has-session -t "=$name" 2>/dev/null; then printf 'session missing\n' >&2; exit 82; fi
run_dir=$(tmux show-options -qv -t "=$name" @codex_run_dir)
[[ -n "$run_dir" ]] || { printf 'session is not managed by clusterctl\n' >&2; exit 85; }
run_dir=$(guard_existing_dir "$run_dir" "$@")
dead=$(tmux display-message -p -t "=$name:0.0" '#{pane_dead}')
if [[ "$dead" != 1 ]]; then
  tmux send-keys -t "=$name:0.0" C-c
  deadline=$((SECONDS + grace))
  while (( SECONDS < deadline )); do
    dead=$(tmux display-message -p -t "=$name:0.0" '#{pane_dead}')
    [[ "$dead" == 1 ]] && break
    sleep 1
  done
fi
dead=$(tmux display-message -p -t "=$name:0.0" '#{pane_dead}')
if [[ "$dead" != 1 && "$force" != 1 ]]; then
  printf 'session still running after graceful interrupt; rerun with --force if authorized\n' >&2
  exit 84
fi
tmux kill-session -t "=$name"
printf 'session=%s\nstate=stopped\nforced=%s\n' "$name" "$force"
'''
    else:
        raise AssertionError(operation)
    return (base + body).encode()


def _require_sessions(host: HostPolicy) -> None:
    if not host.allow_sessions:
        raise ClusterError(f"session operations are disabled for host {host.alias}")


def command_session_status(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    _require_sessions(host)
    name = _validate_session_name(args.name)
    output = _audited(
        policy,
        host,
        "session-status",
        {"session_name": name},
        lambda: _decode(
            _run_ssh(
                policy,
                host,
                _session_simple_script("status"),
                [host.expected_user, host.expected_remote_hostname, name, *host.run_roots],
            )
        ),
    )
    print(output)


def command_session_log(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    _require_sessions(host)
    name = _validate_session_name(args.name)
    lines = _require_int(args.lines, "lines", 1, 10000)
    output = _audited(
        policy,
        host,
        "session-log",
        {"session_name": name, "lines": lines},
        lambda: _decode(
            _run_ssh(
                policy,
                host,
                _session_simple_script("log"),
                [host.expected_user, host.expected_remote_hostname, name, str(lines), *host.run_roots],
            )
        ),
    )
    print(output)


def command_session_stop(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    _require_sessions(host)
    name = _validate_session_name(args.name)
    if args.confirm_name != name:
        raise ClusterError("--confirm-name must exactly match --name")
    grace = _require_int(args.grace_seconds, "grace_seconds", 1, 600)
    output = _audited(
        policy,
        host,
        "session-stop",
        {"session_name": name, "force": bool(args.force)},
        lambda: _decode(
            _run_ssh(
                policy,
                host,
                _session_simple_script("stop"),
                [
                    host.expected_user,
                    host.expected_remote_hostname,
                    name,
                    str(grace),
                    "1" if args.force else "0",
                    *host.run_roots,
                ],
                timeout=max(policy.settings.command_timeout_seconds, grace + 30),
            )
        ),
    )
    print(output)


def _control_command(policy: Policy, host: HostPolicy, operation: str) -> str:
    if not policy.settings.control_persist_seconds:
        raise ClusterError("ControlMaster is disabled by policy")
    _resolved_ssh_identity(host)
    _ensure_control_dir(policy.settings)
    argv = [
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={policy.settings.known_hosts_file}",
        "-o", "ForwardAgent=no",
        "-o", "ClearAllForwardings=yes",
        "-o", "PermitLocalCommand=no",
        "-o", "RequestTTY=no",
        "-o", "UpdateHostKeys=no",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-S", str(policy.settings.control_path),
        "-O", operation,
        "--", host.alias,
    ]
    process = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=policy.settings.connect_timeout_seconds + 5,
        check=False,
        shell=False,
    )
    combined = "\n".join(part.strip() for part in (process.stdout, process.stderr) if part.strip())
    if process.returncode:
        raise ClusterError(f"ControlMaster {operation} failed for {host.alias}: {combined}")
    return combined or "ok"


def command_control_status(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    output = _audited(
        policy,
        host,
        "control-status",
        {},
        lambda: _control_command(policy, host, "check"),
    )
    print(output)


def command_control_close(policy: Policy, args: argparse.Namespace) -> None:
    host = _host(policy, args.host)
    output = _audited(
        policy,
        host,
        "control-close",
        {},
        lambda: _control_command(policy, host, "exit"),
    )
    print(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        default=os.environ.get("CODEX_SSH_CLUSTER_POLICY", "~/.config/codex-ssh-cluster-ops/policy.json"),
        help="explicit JSON policy path (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-policy", help="validate policy schema and permissions")
    validate.set_defaults(handler=command_validate_policy)

    for name, handler in (("doctor", command_doctor), ("audit", command_audit)):
        command = commands.add_parser(name, help=f"run the bounded {name} checks")
        command.add_argument("--host", action="append", help="exact allowlisted SSH alias; repeatable")
        command.add_argument("--all", action="store_true", help="operate on every allowlisted host")
        if name == "audit":
            command.add_argument("--repo", help="optional absolute remote Git worktree")
        command.set_defaults(handler=handler)

    verify = commands.add_parser("verify-tree", help="compare exact clean local and remote Git trees")
    verify.add_argument("--host", required=True)
    verify.add_argument("--local-repo", required=True)
    verify.add_argument("--remote-repo", required=True)
    verify.add_argument("--expected-head", required=True)
    verify.set_defaults(handler=command_verify_tree)

    make_diff = commands.add_parser("make-diff", help="capture the complete local worktree as a canonical reviewed Git diff")
    make_diff.add_argument("--local-repo", required=True)
    make_diff.add_argument("--expected-head", required=True)
    make_diff.add_argument("--output", required=True, help="new local output file; existing files are never overwritten")
    make_diff.set_defaults(handler=command_make_diff)

    apply_diff = commands.add_parser("apply-diff", help="apply a reviewed canonical diff produced by make-diff")
    apply_diff.add_argument("--host", required=True)
    apply_diff.add_argument("--local-repo", required=True)
    apply_diff.add_argument("--remote-repo", required=True)
    apply_diff.add_argument("--expected-head", required=True)
    apply_diff.add_argument("--diff", required=True, help="new local file produced by clusterctl make-diff")
    apply_diff.set_defaults(handler=command_apply_diff)

    session_start = commands.add_parser("session-start", help="start an exact tmux session from a reviewed command file")
    session_start.add_argument("--host", required=True)
    session_start.add_argument("--local-repo", required=True)
    session_start.add_argument("--remote-repo", required=True)
    session_start.add_argument("--run-dir", required=True, help="new absolute directory under an allowed run root")
    session_start.add_argument("--name", required=True, help="exact tmux session name")
    session_start.add_argument("--expected-head", required=True)
    session_start.add_argument("--command-file", required=True)
    session_start.set_defaults(handler=command_session_start)

    session_status = commands.add_parser("session-status", help="show bounded metadata for one exact tmux session")
    session_status.add_argument("--host", required=True)
    session_status.add_argument("--name", required=True)
    session_status.set_defaults(handler=command_session_status)

    session_log = commands.add_parser("session-log", help="tail the managed log for one exact tmux session")
    session_log.add_argument("--host", required=True)
    session_log.add_argument("--name", required=True)
    session_log.add_argument("--lines", type=int, default=200)
    session_log.set_defaults(handler=command_session_log)

    session_stop = commands.add_parser("session-stop", help="interrupt and stop one exact tmux session without deleting outputs")
    session_stop.add_argument("--host", required=True)
    session_stop.add_argument("--name", required=True)
    session_stop.add_argument("--confirm-name", required=True)
    session_stop.add_argument("--grace-seconds", type=int, default=30)
    session_stop.add_argument("--force", action="store_true", help="kill the exact session if graceful interrupt times out")
    session_stop.set_defaults(handler=command_session_stop)

    for name, handler, help_text in (
        ("control-status", command_control_status, "check the exact host's persistent SSH control socket"),
        ("control-close", command_control_close, "close the exact host's persistent SSH control socket"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--host", required=True)
        command.set_defaults(handler=handler)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        args.handler(policy, args)
        return 0
    except (ClusterError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
