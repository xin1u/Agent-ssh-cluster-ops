# Configuration

## Requirements

- Local macOS or Linux with Python 3.9 or newer.
- Local OpenSSH client and Git.
- Existing SSH aliases and trusted host keys configured by the operator.
- Remote Bash, Git, GNU-compatible `realpath -e`, `flock`, `sha256sum`, and optionally `nvidia-smi`. Hosts with `allow_sessions=true` also require tmux.
- Key-based or agent-based non-interactive authentication. Password and keyboard-interactive authentication are disabled by the CLI.

No Node runtime, npm package, SSH MCP, remote Codex login, or remote patch wrapper is required.

## Install The Skill

Place the complete `codex-ssh-cluster-ops` directory in a colleague's Codex skills directory. Keep the directory name unchanged so `$codex-ssh-cluster-ops` is stable.

Create a private policy from `assets/policy.example.json`:

```bash
mkdir -p ~/.config/codex-ssh-cluster-ops
cp assets/policy.example.json ~/.config/codex-ssh-cluster-ops/policy.json
chmod 600 ~/.config/codex-ssh-cluster-ops/policy.json
chmod 600 ~/.ssh/known_hosts
```

Edit the copied policy with real aliases and expected identities. Do not add credentials. Validate it before connecting:

```bash
python3 scripts/clusterctl.py \
  --policy ~/.config/codex-ssh-cluster-ops/policy.json \
  validate-policy
```

The policy path can also be supplied in `CODEX_SSH_CLUSTER_POLICY`. An explicit `--policy` is easier to audit when multiple clusters are configured.

## Policy Semantics

The parser rejects unknown keys and unsupported schema versions. The policy must be a regular, non-symlink file owned by the current user with mode `0600` or stricter.

`settings` fields:

| Field | Meaning |
| --- | --- |
| `connect_timeout_seconds` | OpenSSH connection timeout, 1-120 seconds. |
| `command_timeout_seconds` | Local ceiling for one bounded remote operation. |
| `control_persist_seconds` | Persistent connection lifetime. Set `0` to disable ControlMaster. |
| `control_path` | Absolute local path after `~` expansion. Filename must contain one `%C`. |
| `max_patch_bytes` | Maximum reviewed Git diff size. |
| `max_command_bytes` | Maximum reviewed session command-file size. |
| `parallelism` | Maximum concurrent hosts for `doctor` and `audit`. |
| `audit_log` | Local JSONL metadata log. Contents and parent directory are forced private. |
| `known_hosts_file` | Existing explicit host-key database. It is trust material, not a host allowlist. |

Each `hosts` key is an exact SSH config alias. Aliases cannot begin with `-` or contain `@`, `/`, whitespace, or shell control characters.

Host fields:

| Field | Meaning |
| --- | --- |
| `expected_user` | Expected `ssh -G` user and remote `id -un`. |
| `expected_hostname` | Expected `ssh -G` resolved destination, including an IP when that is what SSH resolves. |
| `expected_port` | Expected resolved SSH port. |
| `expected_remote_hostname` | Exact output expected from remote `hostname`. |
| `worktree_roots` | Absolute canonical roots under which Git worktrees may be inspected or patched. |
| `run_roots` | Absolute canonical roots under which new managed run directories may be created. |
| `allow_patch` | Enables `apply-diff` for this host. |
| `allow_sessions` | Enables managed tmux lifecycle commands for this host. |

Keep write capabilities off for audit-only hosts. Use the narrowest practical roots; do not use `/`, a home directory, or a whole shared filesystem.

The example deliberately starts with `allow_patch=false`, `allow_sessions=false`, and `control_persist_seconds=0`. After read-only `doctor` and `audit` work, enable only the capabilities each colleague actually needs.

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

It compares `ssh -G` user, hostname, and port to policy before every connection. It then checks remote user and hostname inside every script. A missing or changed host key fails closed; enroll or rotate keys through the team's separate SSH process.

When `control_persist_seconds` is explicitly changed to a nonzero value, sockets live beneath the configured private cache directory. The Skill does not edit `~/.ssh/config`. Use `control-status` and `control-close` for one exact alias.
