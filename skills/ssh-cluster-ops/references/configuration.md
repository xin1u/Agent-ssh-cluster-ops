# Configuration

## Requirements

- Local macOS or Linux with Python 3.9 or newer.
- Local OpenSSH client and Git.
- Existing SSH aliases and trusted host keys configured by the operator.
- Remote Bash, Git, GNU-compatible `realpath -e`, `flock`, `sha256sum`, and optionally `nvidia-smi`. Hosts with `allow_sessions=true` also require tmux.
- Key-based or agent-based non-interactive authentication. Password and keyboard-interactive authentication are disabled by the CLI.

No Node runtime, npm package, SSH MCP, remote AI agent, or remote patch wrapper is required.

## Install The Skill

Use the repository's `install.py` to install the canonical `skills/ssh-cluster-ops` directory into one documented user or project discovery root. It defaults to refusing an existing target. Do not install the same skill into multiple roots discovered by one client, because that creates duplicate entries:

```bash
python3 install.py --agent agents --scope user --mode symlink --dry-run
python3 install.py --agent agents --scope user --mode symlink
```

Use `--agent codex|claude|cursor|opencode|agents` to select the discovery root, `--scope project --project-dir /path/to/project` for a project-local install, and `--mode copy` where symlinks are unsuitable. Use `--replace` only when replacing that exact skill directory is intentional. The installer never connects to a host and refuses a project inside the source repository.

For user installs, `CODEX_HOME` selects the Codex configuration root and `XDG_CONFIG_HOME` selects OpenCode's configuration root when either is set. Relative configured roots are rejected.

When invoking the CLI from an Agent, resolve `SKILL_DIR` to the directory containing this file's sibling `SKILL.md`; do not assume the Agent's current working directory is the skill directory:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" validate-policy
```

## Policy Location And Compatibility

Create a private policy from `assets/policy.example.json`:

```bash
mkdir -p ~/.config/ssh-cluster-ops
cp "$SKILL_DIR/assets/policy.example.json" ~/.config/ssh-cluster-ops/policy.json
chmod 600 ~/.config/ssh-cluster-ops/policy.json
chmod 600 ~/.ssh/known_hosts
```

Edit the copied policy with exact aliases, identities, worktree roots, and run roots. Do not add credentials. Validate it before connecting:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" \
  --policy ~/.config/ssh-cluster-ops/policy.json validate-policy
```

The policy path can be supplied with `SSH_CLUSTER_OPS_POLICY`. For existing installations, `CODEX_SSH_CLUSTER_POLICY` remains a fallback. If neither variable is set, the CLI prefers `~/.config/ssh-cluster-ops/policy.json` and uses `~/.config/codex-ssh-cluster-ops/policy.json` only when the generic file is absent and the legacy file exists.

Existing policies whose audit path ends in either `ssh-cluster-ops/audit.jsonl` or `codex-ssh-cluster-ops/audit.jsonl` remain valid. New templates use the generic name. The policy parser still requires a regular, non-symlink file owned by the current user with mode `0600` or stricter.

## Policy Semantics

The parser rejects unknown keys and unsupported schema versions.

| Field | Meaning |
| --- | --- |
| `connect_timeout_seconds` | OpenSSH connection timeout, 1-120 seconds. |
| `command_timeout_seconds` | Local ceiling for one bounded remote operation. |
| `control_persist_seconds` | Persistent connection lifetime; `0` disables ControlMaster. |
| `control_path` | Absolute local path after `~` expansion, with exactly one `%C`. |
| `max_patch_bytes` | Maximum reviewed Git diff size. |
| `max_command_bytes` | Maximum reviewed session command-file size. |
| `parallelism` | Maximum concurrent hosts for `doctor` and `audit`. |
| `audit_log` | Local JSONL metadata log; its parent is forced private. |
| `known_hosts_file` | Explicit host-key database, not a host allowlist. |

Each host key is an exact SSH config alias. Host fields pin the resolved user, destination, port, remote hostname, worktree roots, run roots, and whether patching or managed sessions are enabled. Use the narrowest practical roots; never use `/`, a home directory, or a whole shared filesystem.

The example starts with patching, sessions, and connection reuse disabled. After read-only `doctor` and `audit` work, enable only the capabilities each operator actually needs.

## SSH Trust And Multiplexing

The CLI always applies strict options including:

```text
BatchMode=yes
StrictHostKeyChecking=yes
ForwardAgent=no
ClearAllForwardings=yes
PermitLocalCommand=no
RequestTTY=no
UpdateHostKeys=no
PasswordAuthentication=no
KbdInteractiveAuthentication=no
```

It compares `ssh -G` user, hostname, and port to policy before every connection and checks remote user and hostname inside every script. A missing or changed host key fails closed; enroll or rotate keys through the team's separate SSH process.

When `control_persist_seconds` is nonzero, sockets live beneath the configured private cache directory. The skill does not edit `~/.ssh/config`; use `control-status` and `control-close` for one exact alias.
