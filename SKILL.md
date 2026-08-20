---
name: codex-ssh-cluster-ops
description: Operate explicitly configured shared SSH cluster hosts for bounded GPU, Git, process, and tmux audits; controlled local-to-remote Git diff application; exact clean-tree verification; persistent OpenSSH control sockets; and exact managed tmux session lifecycle from a local Codex session. Do not use it as authorization for arbitrary shell access, secrets, checkpoint or dataset transfer, or scheduler changes.
---

# SSH Cluster Ops

Use the bundled `scripts/clusterctl.py` for every operation it covers. It uses native OpenSSH and Python's standard library; do not install an SSH MCP, Node package, or remote Codex wrapper for this workflow.

Read [references/configuration.md](references/configuration.md) before installing or changing a policy. Read [references/operations.md](references/operations.md) for exact commands. Read [references/security-model.md](references/security-model.md) when reviewing scope, trust, or a rejected operation.

## Operating Rules

1. Treat the JSON policy as a technical allowlist, not as permission for an unrelated action. Respect the user's requested scope and the cluster's own operational rules.
2. Begin with `validate-policy`, then `doctor` or `audit`. Resolve the exact SSH alias through `ssh -G`; stop if its user, hostname, or port differs from policy.
3. Keep source edits local-first. Before synchronization, inspect local and remote branch, HEAD, upstream, and dirty state. Prefer the team's normal Git workflow when it is available.
4. Use `apply-diff` only for a reviewed canonical diff produced by this CLI's `make-diff`, against an exact clean remote HEAD. Never turn it into a generic remote command, upload, or overwrite mechanism.
5. Use `session-start` only for a reviewed local command file, an exact local/remote code tree based on the expected HEAD, and a new run directory. The command file is executable code; inspect it before starting the session.
6. Operate only exact managed tmux session names. `session-stop` needs matching `--confirm-name`; use `--force` only when terminating that exact session is intended. Never delete run outputs automatically.
7. Do not weaken strict host-key checking, identity matching, path canonicalization, clean-tree checks, repository locking, size limits, or local audit logging to make an operation pass.
8. Never put passwords, private keys, API tokens, or other secrets in policy files, command files, diffs, argv, logs, or prompts. Configure authentication through OpenSSH outside this Skill.
9. Do not expose full process argv in audits. Report process names and resource usage only.

## Scope Boundaries

The CLI intentionally has no `exec`, interactive shell, scheduler, upload, download, copy, delete, reset, clean, commit, or push subcommand. Use established tools for source publication and approved transfer tools for datasets or checkpoints.

`session-start` is a narrow lifecycle wrapper, not a sandbox. It records a command hash, exact HEAD, run directory, and tmux name, but the remote account retains its normal permissions. Only trusted operators should receive a policy that enables sessions.

When a strict check fails, report the observed mismatch and stop. Do not add `accept-new`, use `known_hosts` as authorization, bypass a dirty tree, or target a different host merely to continue.
