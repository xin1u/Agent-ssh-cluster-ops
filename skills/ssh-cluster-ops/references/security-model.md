# Security Model

## Boundaries

The skill is a small inspectable client based on native OpenSSH and Python's standard library. It keeps edits local-first, streams reviewed payloads through stdin, verifies exact source state, canonicalizes remote paths, locks repositories during mutation, and limits tmux operations to exact sessions created by this tool.

It intentionally avoids:

- treating every `known_hosts` entry as an authorized target;
- `StrictHostKeyChecking=accept-new` or disabled key checking;
- embedding multiline patches in shell argv;
- exposing arbitrary remote shell, upload, or download tools;
- reading or printing all of `~/.ssh/config` or agent configuration;
- prefix-based process or session termination;
- automatic Git reset, clean, staging, commit, push, or output deletion.

## Trust Assumptions

The local operator account, policy, command files, reviewed diffs, OpenSSH configuration, private keys, agent, and installed skill are trusted. The SSH server and remote account are trusted after strict key and identity verification. The remote account retains its normal Unix permissions. Git and tmux on the remote host are expected to behave normally.

The CLI is not a sandbox. A reviewed session command can run any program available to the remote account, so `allow_sessions` is for trusted operators only.

## Authorization Boundary

The policy answers where the tool may technically operate; it does not answer what the user authorized. It does not supersede repository ownership, scheduler policy, active-job protection, data governance, or a request to keep running jobs unchanged. Job submission, scheduler changes, dataset/checkpoint transfers, and Git publication remain separate workflows.

## Payload Handling

Diffs and session command files are read from regular non-symlink local files, bounded by policy, hashed locally, and embedded in a random heredoc section of SSH stdin. Their contents never appear in SSH argv or the JSONL audit log. The remote side stores payloads with private modes and verifies their SHA256 before use.

Git mutation requires exact expected HEAD and clean state while holding a per-repository `flock`. A patch is checked before application and for whitespace errors afterward. Session creation requires a full remote worktree snapshot whose calculated Git tree matches the local snapshot; the lock is released after creation.

New sessions write neutral `@sshops_*` tmux metadata. Status, log, and stop also read legacy `@codex_*` metadata so existing managed sessions remain operable.

## Residual Risks

- A malicious local operator can alter the skill or submit a harmful reviewed command file.
- Remote commands inherit the remote account's filesystem and network permissions.
- Complex SSH `Match`, proxy, and identity rules can still have effects outside the final identity checks.
- A remote administrator can alter binaries, host behavior, files, or observed output.
- A failure after `git apply` can leave a partial or dirty state; there is no destructive automatic rollback.
- Audit output and session logs may contain application-generated sensitive data; store them on protected filesystems.
- Git-ignored files, external datasets, environment state, containers, and dependencies are outside the tree fingerprint. Submodule repositories are rejected for managed sessions.

## Failure Policy

Fail closed on missing or changed host keys, resolved or remote identity mismatch, unknown policy keys, insecure permissions, path escape, symlink input, dirty tree, HEAD mismatch, busy repository lock, duplicate sessions, existing run directories, payload hash mismatch, invalid diffs, timeouts, or ambiguous stop requests. Do not broaden roots, switch hosts, disable checks, clean a worktree, terminate another session, or delete outputs merely to make an operation pass.
