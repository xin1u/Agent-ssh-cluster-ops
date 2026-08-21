---
name: web-terminal-remote-dev
description: Project discovery adapter for the canonical GridLatch browser-terminal Agent Skill. Use when a remote host is reachable only through a browser-based IDE and SSH is genuinely unavailable, to run commands and push or pull files with checksum verification while working in this repository.
license: Apache-2.0
metadata:
  author: xin1u
  version: "0.1.0"
  compatibility: Node.js 18+ and a Chromium-based browser locally; see the canonical skill for remote requirements.
---

# GridLatch Web Terminal Project Adapter

Read and follow the canonical skill at [`../../../skills/web-terminal-remote-dev/SKILL.md`](../../../skills/web-terminal-remote-dev/SKILL.md) before any operation. Resolve scripts and references relative to that canonical file. This adapter grants no additional capability or authorization.

Prefer [`../ssh-cluster-ops/SKILL.md`](../ssh-cluster-ops/SKILL.md) whenever SSH reaches the host: it is policy-bound and auditable, while this skill is an operator-supervised last resort.
