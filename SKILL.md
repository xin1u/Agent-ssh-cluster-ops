---
name: codex-ssh-cluster-ops
description: Compatibility entrypoint for the portable ssh-cluster-ops Agent Skill. Use for bounded, policy-controlled SSH cluster audits, reviewed Git diffs, and managed tmux sessions when an existing Codex installation still uses the legacy skill name.
license: Apache-2.0
metadata:
  author: xin1u
  version: "0.2.0"
  compatibility: Python 3.9+, OpenSSH, and Git locally; see the canonical skill for remote requirements.
---

# Legacy Codex Adapter

This entrypoint preserves existing `$codex-ssh-cluster-ops` installations. Before performing any operation, read and follow the canonical agent-neutral skill at [skills/ssh-cluster-ops/SKILL.md](skills/ssh-cluster-ops/SKILL.md).

Use [scripts/clusterctl.py](scripts/clusterctl.py) only as the compatibility launcher. It delegates to the canonical implementation. Do not treat this adapter as permission to broaden the canonical skill's policy or authorization boundaries.
