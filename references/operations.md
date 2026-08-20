# Operations

Examples below assume execution from the installed Skill directory and a policy at the default path. Put global `--policy` before the subcommand when overriding it.

## Validate And Inspect

Validate syntax, permissions, roots, and known-hosts configuration without connecting:

```bash
python3 scripts/clusterctl.py validate-policy
```

Check local dependencies, resolved SSH identity, remote identity, and required baseline tools:

```bash
python3 scripts/clusterctl.py doctor --host gpu-a
python3 scripts/clusterctl.py doctor --all
```

Audit GPU occupancy, process executable names, tmux session names, and optionally one Git worktree:

```bash
python3 scripts/clusterctl.py audit --all
python3 scripts/clusterctl.py audit \
  --host gpu-a \
  --repo /mnt/shared/code/project
```

Audits never print full process argv. An unavailable GPU or tmux command is reported without turning the audit into an arbitrary shell fallback.

## Verify Exact Source State

Require local and remote trees to have the same exact HEAD, tree object, and clean state:

```bash
python3 scripts/clusterctl.py verify-tree \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567
```

Run this before debugging or launching from a synchronized checkout. It does not fetch, pull, reset, clean, commit, or push.

## Apply A Reviewed Diff

Capture the complete Git-visible local worktree with a temporary Git index. This includes staged, unstaged, untracked, deleted, binary, and mode changes without modifying the user's real index:

```bash
python3 scripts/clusterctl.py make-diff \
  --local-repo /path/to/local/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --output /tmp/reviewed.diff
```

Inspect `/tmp/reviewed.diff` before applying it. `make-diff` refuses to overwrite an existing output file.

Apply it to an allowlisted remote worktree only when that worktree is still at the expected clean HEAD:

```bash
python3 scripts/clusterctl.py apply-diff \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --diff /tmp/reviewed.diff
```

The diff must still match the complete local worktree when `apply-diff` starts. It travels in SSH stdin, never command argv. The remote transaction verifies identity, canonical path, repository lock, exact HEAD, clean status, payload SHA256, expected result tree, `git apply --check --whitespace=error-all`, and `git diff --check`. It leaves the resulting worktree dirty and prints a stat; it never stages, commits, pushes, resets, cleans, or silently rolls back.

Codex `*** Begin Patch` grammar is deliberately rejected. Use `clusterctl make-diff` after local edits; symlink and submodule changes are rejected.

## Start And Observe A Managed Session

Prepare a reviewed command file locally. It should contain the team's normal launcher, explicit environment setup, and no embedded credentials. The working directory is set to the verified remote repository.

```bash
python3 scripts/clusterctl.py session-start \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --run-dir /mnt/shared/experiments/debug-20260820-r1 \
  --name debug-20260820-r1 \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --command-file /path/to/reviewed-command.sh
```

The remote Git worktree may be clean or may contain a diff previously applied by this tool. Its Git-visible snapshot, including untracked non-ignored files, must have the same Git tree ID as the current local worktree and the exact expected HEAD. This makes the normal flow `make-diff -> review -> apply-diff -> session-start`. Repositories containing Git submodules are rejected for managed sessions because a parent tree cannot fingerprint dirty submodule contents. The run directory's parent must already exist under an allowed run root, and the final run directory must not exist. The CLI creates it mode `0700`, stores the command and metadata privately, enables tmux `remain-on-exit`, and writes stdout/stderr to `session.log`.

If initialization fails after the new run directory is created, the tool stops any partially created exact tmux session but preserves that directory for diagnosis. Inspect it and choose a new run directory for the retry; the tool never deletes it automatically.

Inspect only the exact managed session:

```bash
python3 scripts/clusterctl.py session-status --host gpu-a --name debug-20260820-r1
python3 scripts/clusterctl.py session-log --host gpu-a --name debug-20260820-r1 --lines 200
```

Stop gracefully with an exact-name confirmation:

```bash
python3 scripts/clusterctl.py session-stop \
  --host gpu-a \
  --name debug-20260820-r1 \
  --confirm-name debug-20260820-r1 \
  --grace-seconds 30
```

If the exact session ignores `Ctrl-C`, inspect it before using `--force`. Stopping never deletes the run directory or logs. There is intentionally no bulk stop, prefix match, `pkill`, `killall`, or cleanup command.

## Persistent SSH Connection

Once a normal operation creates the configured master connection:

```bash
python3 scripts/clusterctl.py control-status --host gpu-a
python3 scripts/clusterctl.py control-close --host gpu-a
```

Both commands resolve and verify the exact alias. They never enumerate or close another alias's socket.

## Audit Records

Every remote operation appends two compact JSON objects with the same `invocation_id`: a `start` record before the operation and a `finish` record containing its outcome and duration. Records include the OS-account name resolved from the numeric UID, that UID, local host, policy hash, action, exact alias and resolved destination, relevant path, session, HEAD, and payload hash and size when applicable. Patch contents, command contents, stdout, stderr, credentials, and full process argv are not recorded.
