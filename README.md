# GridLatch: An Agent-Agnostic Framework for Safe and Auditable Remote Cluster Operations

[![Tests](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml/badge.svg)](https://github.com/xin1u/codex-ssh-cluster-ops/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

GridLatch gives coding agents a portable, policy-bound control plane for auditable SSH operations on shared compute clusters. Its primary skill works with native OpenSSH, Git, and the Python standard library; no remote AI agent, SSH MCP, Node runtime, or third-party wrapper is required.

GridLatch's primary skill is distributed under the stable skill ID `ssh-cluster-ops`. The GitHub repository is currently published at the legacy slug `codex-ssh-cluster-ops`, and the old Codex skill ID remains available for backwards compatibility. The maintained skill is `skills/ssh-cluster-ops/`, whose `SKILL.md` is the single source of operating rules.

It provides four narrowly scoped capabilities:

- parallel GPU, process-name, tmux, and Git-state audits;
- persistent OpenSSH `ControlMaster` connections;
- reviewed complete-worktree Git diff application to an allowlisted checkout;
- exact-name managed tmux session start, status, log, and stop.

It deliberately does not provide arbitrary remote shell execution, upload/download, scheduler mutation, bulk process termination, Git commit/push/reset/clean, or output deletion.

## Skills In This Repository

| Skill | Use it when | Transport |
| --- | --- | --- |
| [`ssh-cluster-ops`](skills/ssh-cluster-ops/SKILL.md) | SSH reaches the host. This is the default and should be preferred whenever it is possible. | OpenSSH, policy-bound and audited |
| [`web-terminal-remote-dev`](skills/web-terminal-remote-dev/SKILL.md) | SSH is genuinely unavailable — ingress is broken, the port is blocked, and reverse tunnels are impossible — but a browser IDE (code-server / VS Code Web) still works. | Chrome DevTools Protocol driving the page the operator is already signed into |

The two skills sit at deliberately different points on the safety spectrum, and that difference is the reason they are separate skills rather than one:

- `ssh-cluster-ops` is a *control plane*. It verifies host keys, enforces a JSON allowlist, canonicalizes paths, and writes an audit log. It refuses arbitrary shell execution by design.
- `web-terminal-remote-dev` is an *operator-supervised last resort*. It runs arbitrary commands with the remote account's full permissions, has no policy file, and keeps no audit log; its authorization comes solely from the operator already being signed into that page in their own browser. It requires a Node.js runtime and a local Chromium-based browser, which the primary skill deliberately does not.

Use the browser skill only with the operator's authorization, and never as a way around an access policy. Both entrypoints state this, and its `SKILL.md` documents the safety gate that refuses to type into a terminal tab that is not a plain shell.

## Agent Compatibility

The package follows the open `SKILL.md` format. Codex, Claude Code, Cursor, and OpenCode can discover the same canonical skills through their documented skill roots. Agents that do not implement Agent Skills or `AGENTS.md` cannot automatically inherit a skill; point them to `skills/<skill>/SKILL.md` explicitly.

| Agent | User-level discovery root | Project-level discovery root |
| --- | --- | --- |
| [Codex](https://developers.openai.com/codex/skills/) | `~/.agents/skills/<skill>` or `~/.codex/skills/<skill>` | `.agents/skills/<skill>` or `.codex/skills/<skill>` |
| [Claude Code](https://code.claude.com/docs/en/skills) | `~/.claude/skills/<skill>` | `.claude/skills/<skill>` |
| [Cursor](https://cursor.com/docs/context/skills) | `~/.agents/skills/<skill>` or `~/.cursor/skills/<skill>` | `.agents/skills/<skill>` or `.cursor/skills/<skill>` |
| [OpenCode](https://opencode.ai/docs/skills/) | `~/.agents/skills/<skill>` or `~/.config/opencode/skills/<skill>` | `.agents/skills/<skill>` or `.opencode/skills/<skill>` |

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

`--skill` selects which skill to install and defaults to `ssh-cluster-ops`, so existing commands are unchanged. Install the browser skill only on machines that need it:

```bash
python3 install.py --agent claude --scope user --mode symlink --skill web-terminal-remote-dev
```

Each skill installs into its own directory under the same discovery root, so the two never collide and either can be installed without the other.

For user installs, `CODEX_HOME` overrides the default Codex root and `XDG_CONFIG_HOME` overrides OpenCode's default `~/.config` root. Both must resolve to absolute local paths.

The root `SKILL.md`, root `scripts/clusterctl.py`, root policy template, and `agents/openai.yaml` are retained only as compatibility surfaces for older Codex installations of `ssh-cluster-ops`. Do not edit those independently of the canonical skill.

## Requirements

`ssh-cluster-ops` — Local: Python 3.9+, OpenSSH, Git, and an existing SSH config alias with trusted host keys. Remote: Bash, Git, GNU-compatible `realpath -e`, `flock`, and `sha256sum`; `tmux` is additionally required when managed sessions are enabled. Authentication must be non-interactive; passwords and keyboard-interactive authentication are disabled.

`web-terminal-remote-dev` — Local: Node.js 18+ and a Chromium-based browser launched with a remote debugging port. Remote: Bash, `base64`, and `sha256sum`, plus a code-server / VS Code Web page the operator is already signed into.

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

Resolve the installed `ssh-cluster-ops` directory as `SKILL_DIR` and call the bundled CLI from any current working directory:

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

### When SSH Is Unavailable

Only if SSH cannot reach the host at all, `web-terminal-remote-dev` drives the browser IDE page instead. Launch a dedicated Chrome with a debugging port and a throwaway profile, open the already-authenticated IDE page in it, then:

```bash
SKILL_DIR=/path/to/installed/web-terminal-remote-dev
node "$SKILL_DIR/scripts/podtab.js"                                  # list terminal tabs
node "$SKILL_DIR/scripts/pod.js" 'hostname; df -h /tmp | tail -1'    # run a command, get its output
node "$SKILL_DIR/scripts/podpush.js" ./train.py /mnt/shared/train.py # push, sha256-verified
node "$SKILL_DIR/scripts/podpull.js" /tmp/train.log | tail -20       # read back without touching the terminal
```

Several agents can drive one page concurrently by giving each its own `POD_TERM` terminal instance, and several cluster pages by giving each its own `POD_TAB_MATCH`. The measurements, the DOM facts, and the full list of dead ends are in [`skills/web-terminal-remote-dev/references/webterm-internals.md`](skills/web-terminal-remote-dev/references/webterm-internals.md).

## Security Boundary

The JSON policy is a technical allowlist, not authorization for an unrelated action. Every connection enforces strict host-key checking, compares `ssh -G` identity and port, verifies remote user and hostname, canonicalizes paths, and records compact private audit metadata. Reviewed payloads travel through SSH stdin rather than argv. Session metadata is written with the neutral `@sshops_*` tmux keys; the CLI can still read legacy `@codex_*` keys from existing sessions.

Failures are fail-closed. Do not broaden roots, switch hosts, disable checks, clean a worktree, terminate another session, or delete outputs merely to make an operation pass.

`web-terminal-remote-dev` sits outside that boundary and does not inherit it. It has no policy file, no host-key verification, and no audit log; it borrows the operator's own authenticated browser session and can run anything that account can run. Its own protections are narrower and local: every input dispatch re-checks in one atomic page-side expression that the target tab is a plain `bash`/`sh`/`zsh` and refuses otherwise, commands run in a separate bash so `exit` cannot kill the operator's terminal, payloads are base64-encoded so no content can corrupt the command line, and pushes are staged and re-verified so a failed transfer never leaves a corrupt file at the destination. Prefer `ssh-cluster-ops` whenever SSH works.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
node --check skills/web-terminal-remote-dev/scripts/*.js
python3 /path/to/skill-creator/scripts/quick_validate.py skills/ssh-cluster-ops
python3 /path/to/skill-creator/scripts/quick_validate.py skills/web-terminal-remote-dev
git diff --check
```

The tests use a fake SSH/tmux harness and do not connect to a real cluster, and no test drives a browser. The local machine used to publish this repository may not have Claude Code, Cursor, or OpenCode installed; their compatibility is validated statically against their documented discovery layouts, not claimed as end-to-end client execution.

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
