# GridLatch

Policy-bound remote operations for coding agents.

[![Tests](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml/badge.svg)](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

GridLatch is a local-first [Agent Skills](https://agentskills.io/) package for bounded, auditable SSH operations on shared compute hosts. It works with native OpenSSH, Git, and the Python standard library; no remote AI agent, SSH MCP, Node runtime, or third-party wrapper is required.

GridLatch is distributed under the stable skill ID `ssh-cluster-ops`. The GitHub repository is currently published at the legacy slug `codex-ssh-cluster-ops`, and the old Codex skill ID remains available for backwards compatibility. The maintained skill is `skills/ssh-cluster-ops/`, whose `SKILL.md` is the single source of operating rules.

It provides four narrowly scoped capabilities:

- parallel GPU, process-name, tmux, and Git-state audits;
- persistent OpenSSH `ControlMaster` connections;
- reviewed complete-worktree Git diff application to an allowlisted checkout;
- exact-name managed tmux session start, status, log, and stop.

It deliberately does not provide arbitrary remote shell execution, upload/download, scheduler mutation, bulk process termination, Git commit/push/reset/clean, or output deletion.

## Agent Compatibility

The package follows the open `SKILL.md` format. Codex, Claude Code, Cursor, and OpenCode can discover the same canonical skill through their documented skill roots. Agents that do not implement Agent Skills or `AGENTS.md` cannot automatically inherit a skill; point them to `skills/ssh-cluster-ops/SKILL.md` explicitly.

| Agent | User-level discovery root | Project-level discovery root |
| --- | --- | --- |
| [Codex](https://developers.openai.com/codex/skills/) | `~/.agents/skills/ssh-cluster-ops` or `~/.codex/skills/ssh-cluster-ops` | `.agents/skills/ssh-cluster-ops` or `.codex/skills/ssh-cluster-ops` |
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills/ssh-cluster-ops` | `.claude/skills/ssh-cluster-ops` |
| [Cursor](https://cursor.com/docs/context/skills) | `~/.agents/skills/ssh-cluster-ops` or `~/.cursor/skills/ssh-cluster-ops` | `.agents/skills/ssh-cluster-ops` or `.cursor/skills/ssh-cluster-ops` |
| [OpenCode](https://opencode.ai/docs/skills/) | `~/.agents/skills/ssh-cluster-ops` or `~/.config/opencode/skills/ssh-cluster-ops` | `.agents/skills/ssh-cluster-ops` or `.opencode/skills/ssh-cluster-ops` |

Clone the repository, then install one copy or symlink with the included local-only installer. It refuses to overwrite an existing target unless `--replace` is explicitly supplied:

```bash
git clone https://github.com/xin1u/codex-ssh-cluster-ops.git
cd codex-ssh-cluster-ops

python3 install.py --agent agents --scope user --mode symlink
python3 install.py --agent claude --scope user --mode symlink
python3 install.py --agent codex --scope user --mode symlink
python3 install.py --agent cursor --scope project --project-dir /path/to/project --mode symlink
python3 install.py --agent opencode --scope user --mode symlink
```

The commands above are alternatives, not a required sequence. Use one discovery root per client to avoid duplicate skill entries. Use `--dry-run` to inspect the exact destination. `--scope project` always requires an existing project directory outside this source repository. `copy` is available where symlinks are unsuitable.

For user installs, `CODEX_HOME` overrides the default Codex root and `XDG_CONFIG_HOME` overrides OpenCode's default `~/.config` root. Both must resolve to absolute local paths.

The root `SKILL.md`, root `scripts/clusterctl.py`, root policy template, and `agents/openai.yaml` are retained only as compatibility surfaces for older Codex installations. Do not edit those independently of the canonical skill.

## Requirements

Local: Python 3.9+, OpenSSH, Git, and an existing SSH config alias with trusted host keys. Remote: Bash, Git, GNU-compatible `realpath -e`, `flock`, and `sha256sum`; `tmux` is additionally required when managed sessions are enabled. Authentication must be non-interactive; passwords and keyboard-interactive authentication are disabled.

## Configure A Policy

Copy the canonical template to a private path, fill in exact identities and narrow roots, then validate it. The template starts with patching, sessions, and connection reuse disabled:

```bash
mkdir -p ~/.config/ssh-cluster-ops
cp skills/ssh-cluster-ops/assets/policy.example.json \
  ~/.config/ssh-cluster-ops/policy.json
chmod 600 ~/.config/ssh-cluster-ops/policy.json
chmod 600 ~/.ssh/known_hosts

python3 skills/ssh-cluster-ops/scripts/clusterctl.py \
  --policy ~/.config/ssh-cluster-ops/policy.json validate-policy
python3 skills/ssh-cluster-ops/scripts/clusterctl.py \
  --policy ~/.config/ssh-cluster-ops/policy.json doctor --all
```

The CLI uses `SSH_CLUSTER_OPS_POLICY` when set. It accepts the legacy `CODEX_SSH_CLUSTER_POLICY` as a fallback, and if neither variable is set it prefers `~/.config/ssh-cluster-ops/policy.json`, falling back to the old `~/.config/codex-ssh-cluster-ops/policy.json` only when that is the existing file. Existing audit paths ending in either `ssh-cluster-ops/audit.jsonl` or `codex-ssh-cluster-ops/audit.jsonl` remain valid.

Never put private keys, passwords, tokens, or other secrets in a policy, command file, diff, prompt, or log. Keep `known_hosts` enrollment in the team's normal SSH process; this tool never accepts an unknown key automatically.

## Typical Workflow

Resolve the installed skill directory as `SKILL_DIR` and call the bundled CLI from any current working directory:

```bash
SKILL_DIR=/path/to/installed/ssh-cluster-ops
python3 "$SKILL_DIR/scripts/clusterctl.py" validate-policy
python3 "$SKILL_DIR/scripts/clusterctl.py" audit --all
python3 "$SKILL_DIR/scripts/clusterctl.py" verify-tree \
  --host gpu-a --local-repo /path/to/project \
  --remote-repo /mnt/shared/code/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567
```

For a source change, use `make-diff`, inspect the resulting file, then use `apply-diff` against the exact expected clean remote HEAD. For a debug command, review the local command file, verify the source tree, then use `session-start`; inspect with `session-status`/`session-log` and stop only the exact session name with `session-stop --confirm-name`. The complete examples are in [`skills/ssh-cluster-ops/references/operations.md`](skills/ssh-cluster-ops/references/operations.md).

## Security Boundary

The JSON policy is a technical allowlist, not authorization for an unrelated action. Every connection enforces strict host-key checking, compares `ssh -G` identity and port, verifies remote user and hostname, canonicalizes paths, and records compact private audit metadata. Reviewed payloads travel through SSH stdin rather than argv. Session metadata is written with the neutral `@sshops_*` tmux keys; the CLI can still read legacy `@codex_*` keys from existing sessions.

Failures are fail-closed. Do not broaden roots, switch hosts, disable checks, clean a worktree, terminate another session, or delete outputs merely to make an operation pass.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
python3 /path/to/skill-creator/scripts/quick_validate.py skills/ssh-cluster-ops
git diff --check
```

The tests use a fake SSH/tmux harness and do not connect to a real cluster. The local machine used to publish this repository may not have Claude Code, Cursor, or OpenCode installed; their compatibility is validated statically against their documented discovery layouts, not claimed as end-to-end client execution.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
