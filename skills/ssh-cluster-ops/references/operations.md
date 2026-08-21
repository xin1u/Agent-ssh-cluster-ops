# Operations

Resolve `SKILL_DIR` to the installed skill directory. Put global `--policy` before the subcommand when overriding it.

## Validate And Inspect

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" validate-policy
python3 "$SKILL_DIR/scripts/clusterctl.py" doctor --host gpu-a
python3 "$SKILL_DIR/scripts/clusterctl.py" doctor --all
python3 "$SKILL_DIR/scripts/clusterctl.py" audit --all
python3 "$SKILL_DIR/scripts/clusterctl.py" audit \
  --host gpu-a --repo /mnt/shared/code/project
```

`doctor` checks local dependencies, resolved SSH identity, remote identity, and required baseline tools. `audit` reports GPU occupancy, process executable names, tmux session names, and optionally one Git worktree. Audits never print full process argv and never fall back to an arbitrary shell command.

## Verify Exact Source State

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" verify-tree \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567
```

This requires the exact HEAD, tree object, and clean state on both sides. It does not fetch, pull, reset, clean, commit, or push.

## Apply A Reviewed Diff

Capture the complete Git-visible local worktree with a temporary index:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" make-diff \
  --local-repo /path/to/local/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --output /tmp/reviewed.diff
```

Inspect the new diff before applying it. The output is never overwritten. Apply only to an allowlisted remote worktree still at the expected clean HEAD:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" apply-diff \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --diff /tmp/reviewed.diff
```

The transaction verifies identity, canonical path, repository lock, exact HEAD, clean status, payload SHA256, expected result tree, `git apply --check --whitespace=error-all`, and `git diff --check`. The diff travels in SSH stdin, never argv. The resulting worktree remains dirty; the tool never stages, commits, pushes, resets, cleans, or rolls back automatically. Provider-specific patch wrappers are rejected; use `make-diff`.

## Start And Observe A Managed Session

Review a local command file before starting it:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" session-start \
  --host gpu-a \
  --local-repo /path/to/local/project \
  --remote-repo /mnt/shared/code/project \
  --run-dir /mnt/shared/experiments/debug-20260820-r1 \
  --name debug-20260820-r1 \
  --expected-head 0123456789abcdef0123456789abcdef01234567 \
  --command-file /path/to/reviewed-command.sh
```

The remote snapshot must have the same Git tree as the local worktree and exact expected HEAD. The run directory's parent must already be under an allowed run root, and the final directory must not exist. The tool creates private command, metadata, log, and status files and enables tmux `remain-on-exit`. Repositories containing Git submodules are rejected for managed sessions.

Inspect and stop only the exact managed session:

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" session-status --host gpu-a --name debug-20260820-r1
python3 "$SKILL_DIR/scripts/clusterctl.py" session-log --host gpu-a --name debug-20260820-r1 --lines 200
python3 "$SKILL_DIR/scripts/clusterctl.py" session-stop \
  --host gpu-a --name debug-20260820-r1 \
  --confirm-name debug-20260820-r1 --grace-seconds 30
```

Use `--force` only after inspecting a session that ignores `Ctrl-C`. Stopping never deletes outputs. There is no bulk stop, prefix match, `pkill`, `killall`, or cleanup command.

## Persistent SSH Connection

```bash
python3 "$SKILL_DIR/scripts/clusterctl.py" control-status --host gpu-a
python3 "$SKILL_DIR/scripts/clusterctl.py" control-close --host gpu-a
```

These commands resolve and verify one exact alias and never enumerate or close another alias's socket.

## Audit Records

Every remote operation appends private JSONL `start` and `finish` records with one invocation ID, actor UID, local host, policy hash, action, exact destination, relevant paths, session/HEAD metadata, and payload hashes/sizes where applicable. Payload contents, command contents, credentials, stdout, stderr, and full process argv are not recorded.
