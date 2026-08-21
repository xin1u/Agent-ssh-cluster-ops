# Agent Instructions

This repository develops the agent-neutral `ssh-cluster-ops` Agent Skill. Before changing or using its SSH functionality, read and follow [`skills/ssh-cluster-ops/SKILL.md`](skills/ssh-cluster-ops/SKILL.md).

Preserve the strict host-key, identity, allowlist, canonical-path, clean-tree, repository-lock, payload-size, audit-log, and exact-session checks. Do not add a generic remote shell, transfer, scheduler, delete, or broad process-control command. Tests must use the fake SSH harness and must not contact a real cluster.

The root `SKILL.md`, root `scripts/clusterctl.py`, root policy template, and `agents/openai.yaml` are compatibility surfaces for existing Codex installations. Keep them working when changing the canonical skill.
