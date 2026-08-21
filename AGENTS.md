# Agent Instructions

This repository develops GridLatch, distributed as two agent-neutral Agent Skills.

Its primary skill is `ssh-cluster-ops`. Before changing or using its SSH functionality, read and follow [`skills/ssh-cluster-ops/SKILL.md`](skills/ssh-cluster-ops/SKILL.md).

Preserve the strict host-key, identity, allowlist, canonical-path, clean-tree, repository-lock, payload-size, audit-log, and exact-session checks. Do not add a generic remote shell, transfer, scheduler, delete, or broad process-control command. Tests must use the fake SSH harness and must not contact a real cluster.

Its second skill is `web-terminal-remote-dev`, for hosts that only a browser IDE can reach. Before changing or using it, read and follow [`skills/web-terminal-remote-dev/SKILL.md`](skills/web-terminal-remote-dev/SKILL.md) and the measurements in its `references/webterm-internals.md`. Prefer `ssh-cluster-ops` whenever SSH reaches the host; this skill is an operator-supervised last resort with no policy file and no audit log.

Preserve its atomic shell-tab safety gate (the check and the input dispatch must stay in one page-side expression), the separate-bash isolation, base64 payload encoding, per-chunk landing confirmation, staged-then-verified writes, and the per-terminal lock scoping that keeps parallel agents independent. Do not add a way to read terminal text from the DOM; xterm renders to a canvas, so output must go through a file. No test may drive a real browser.

The root `SKILL.md`, root `scripts/clusterctl.py`, root policy template, and `agents/openai.yaml` are compatibility surfaces for existing Codex installations of `ssh-cluster-ops`. Keep them working when changing the canonical skill.
