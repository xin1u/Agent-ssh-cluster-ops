# Security Model

## What This Improves

The Skill replaces a broad third-party SSH MCP pattern with a small inspectable client based on native OpenSSH and Python's standard library. It keeps edits local-first, streams reviewed payloads through stdin, verifies exact source state, canonicalizes remote paths, locks repositories during mutation, and limits tmux operations to exact sessions created by this tool.

The design specifically avoids:

- treating every `known_hosts` entry as an authorized target;
- `StrictHostKeyChecking=accept-new` or disabled key checking;
- embedding multiline patches in shell argv;
- exposing arbitrary remote shell, upload, or download tools to the model;
- reading or printing all of `~/.ssh/config` or Codex configuration;
- prefix-based process or session termination;
- automatic Git reset, clean, staging, commit, push, or output deletion.

## Trust Assumptions

- The local operator account, policy, command files, reviewed diffs, OpenSSH configuration, private keys, agent, and installed Skill are trusted.
- The SSH server and remote account identity are trusted after strict key and identity verification.
- The remote account may access shared resources according to its normal Unix permissions.
- A configured alias may use a trusted jump host. `ssh -G` verifies the final resolved user, hostname, and port, not ownership of the route.
- Git and tmux on the remote host behave normally.

The CLI is not a sandbox. A reviewed session command can run any program available to the remote account. `allow_sessions` should therefore be enabled only for trusted operators and only after the same checks used for a manual launch.

## Authorization Boundary

The policy answers "where may this tool technically operate?" It does not answer "what did the user authorize?" It does not supersede repository ownership, scheduler policy, active-job protection, data governance, or a request to keep running jobs unchanged.

Job submission and scheduler mutations still require the team's normal workflow. Dataset and checkpoint transfers require dedicated transfer tools. Git publication requires the repository's normal review and author rules.

## Payload Handling

Git diffs and session command files are read from regular non-symlink local files, bounded by policy, hashed locally, and embedded in a randomly delimited section of the SSH stdin script. Their contents never appear in SSH argv or the local JSONL audit log. The remote side stores payloads with private modes and verifies their SHA256 before use.

Git mutation requires an exact expected HEAD and clean worktree while holding a per-repository `flock`. A patch is checked before application and checked for whitespace errors afterward. The result deliberately remains visible as a dirty worktree.

Session creation requires an exact expected HEAD and a full remote worktree snapshot whose calculated Git tree matches the local snapshot. This permits a reviewed `apply-diff` result while rejecting unrelated remote changes. The lock is released after creation; the source tree is not frozen for the lifetime of the job. For immutable production provenance, run from an immutable checkout or container and use the team's scheduler workflow.

## Residual Risks

- A malicious or compromised local operator can alter this Skill or submit a harmful reviewed command file.
- Remote commands inherit the remote account's filesystem and network permissions.
- SSH configuration can contain complex `Match`, proxy, and identity rules. Exact `ssh -G` destination checks reduce but do not eliminate configuration risk.
- A remote administrator can alter binaries, host behavior, files, or observed output.
- A failure after `git apply` can leave a partial or dirty state. The tool never performs an automatic destructive rollback; inspect and recover through the repository's normal workflow.
- Audit output and session logs may contain application-generated sensitive data even though the tool never records payload contents or full argv. Store logs on protected filesystems.
- Git-ignored files, external datasets, environment state, containers, and dependencies are outside the tree fingerprint. Repositories containing submodules are rejected for managed sessions rather than pretending the parent gitlink captures dirty child contents.

## Failure Policy

Fail closed on a missing or changed host key, resolved identity mismatch, remote identity mismatch, unknown policy key, insecure policy permissions, path escape, symbolic-link input, dirty tree, HEAD mismatch, busy repository lock, duplicate session, existing run directory, payload hash mismatch, invalid diff, timeout, or ambiguous stop request.

Do not make a failure disappear by broadening roots, switching hosts, disabling a check, accepting a new key, cleaning a worktree, terminating another session, or deleting outputs unless that distinct action has been reviewed and authorized.
