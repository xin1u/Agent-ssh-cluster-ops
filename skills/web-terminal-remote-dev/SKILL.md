---
name: web-terminal-remote-dev
description: Operate a remote server or cluster pod that is only reachable through a browser-based IDE (code-server / VS Code Web) because SSH is blocked, the ingress port is broken, or reverse tunnels are impossible. Drives the already-authenticated web terminal over the Chrome DevTools Protocol to run commands, retrieve full text output, and push and pull source files with sha256 verification. Use only with the operator's authorization on a page they are already logged into; do not use it as a way around an access policy, and prefer SSH whenever SSH works.
license: Apache-2.0
metadata:
  author: xin1u
  version: "0.1.0"
  compatibility: Node.js 18+ and a Chromium-based browser started with a remote debugging port locally; remote Bash, base64, sha256sum, and a code-server / VS Code Web page the operator is already signed into.
---

# Web Terminal Remote Dev (no SSH)

Turns a browser-only web IDE page into a scriptable remote-dev channel: run commands, read
their full text output, and push/pull real source files — all without SSH.

Resolve `SKILL_DIR` to the directory containing this `SKILL.md`, then call
`node "$SKILL_DIR/scripts/<script>.js"` for every operation below.

**Use this only when SSH is genuinely unavailable and a web IDE is the only way in. If SSH
works, use SSH** — GridLatch's `ssh-cluster-ops` skill is the policy-bound, auditable path and
should always be preferred. This skill is the last-resort channel for the case where no SSH
path exists at all.

Unlike `ssh-cluster-ops`, this skill requires a **Node.js runtime** and a local
Chromium-based browser, and it deliberately has no policy file: its authorization comes from
the fact that the operator is already signed into that page in their own browser. It performs
no host-key verification, keeps no audit log, and enforces no allowlist. Treat it as an
operator-supervised tool, not a controlled control plane.

## Why this works when nothing else does

The web IDE page is already authenticated and already inside the network perimeter. Driving
that page borrows its session, so no new ingress, no new port, no tunnel is needed. Two
channels:

| Direction | Mechanism | Speed |
|---|---|---|
| **In** (commands, file writes) | synthetic `paste` events into the page's xterm | ~9 chars/ms, constant |
| **Out** (output, file reads) | `fetch` the IDE's own `/vscode-remote-resource?path=<abs>` | fast, 1MB ≈ 1s |

**The terminal is canvas-rendered, so its text cannot be read from the DOM** — every command
therefore redirects output to a file, which is fetched back over the fast channel. Never try to
scrape terminal text.

**Tidiness contract:** every invocation puts ALL its remote intermediates in one per-call job
directory `<tmpDir>/jobs/<tag>/` (tag is timestamped, e.g. `j0822_014932_37058`). Success
deletes the directory immediately; failures leave it for diagnosis and it is auto-swept by any
later call after `POD_SWEEP_MIN` minutes (default 24h). Steady state: zero remote litter.

## Setup (once per session)

1. Launch a dedicated Chrome with a debugging port. `--user-data-dir` must be separate, or
   Chrome reuses the running instance and the port silently does nothing:

```bash
open -na "Google Chrome" --args --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.chrome-webterm-debug" --no-first-run --no-default-browser-check \
  "https://HOST/PATH/codeserver/?folder=/your/workspace"
```

The debug port exposes full control of that browser profile to any local process. Use a
throwaway profile, keep unrelated accounts out of it, and close it when finished.

2. In that window, sign in if needed and open a terminal panel (`Ctrl+\``).

3. Verify the channel and make sure a **plain bash/sh/zsh tab** is selected:

```bash
node "$SKILL_DIR/scripts/podtab.js"          # list tabs; '*' marks the selected one
node "$SKILL_DIR/scripts/podtab.js" --auto   # auto-switch to the first shell tab (refuses non-shells)
node "$SKILL_DIR/scripts/pod.js" 'hostname; pwd; df -h /tmp | tail -1'
```

Point the scripts at a different page or paths with environment variables: `POD_CDP_PORT`
(default 9222), `POD_TAB_MATCH` (substring of the page URL, default `codeserver`),
`POD_TMP_DIR` (remote scratch root, default `/tmp/.webterm-pod`), `POD_SWEEP_MIN` (job-dir
sweep age in minutes, default 1440), `POD_INPUT` (`paste` default; `type` falls back to
per-keystroke input if a future xterm breaks synthetic paste), `POD_TERM` (terminal instance
number for parallel use, see below).

## Parallel agents (`POD_TERM`)

Several callers can drive the same page **concurrently without interfering**, one terminal tab
each. Synthetic paste reaches a *background* (non-selected) terminal's own pty directly — the
human-visible selected tab never changes, and measured cross-talk is zero (5/5 stress rounds;
two 30KB pushes in parallel both byte-identical).

```bash
node "$SKILL_DIR/scripts/podtab.js"     # list tabs — each shows its POD_TERM=N instance number
POD_TERM=2 node "$SKILL_DIR/scripts/pod.js" 'make test' 300
POD_TERM=5 node "$SKILL_DIR/scripts/podpush.js" a.py /mnt/shared/project/a.py   # runs simultaneously
```

- The instance number is VS Code's stable "Terminal N" id (from the textarea aria-label); it
  does NOT drift when other tabs close (tab list indices do).
- A tab that has never been viewed has no xterm yet — run `podtab.js <idx>` once to
  instantiate it.
- Locking is per (port, page, terminal): different `POD_TERM`s never wait on each other; two
  calls with the same `POD_TERM` still serialize. Rule for parallel work: **every caller uses
  its own `POD_TERM`** (the selected-tab default mode does not mix safely with directed mode on
  the same terminal).
- The safety gate still applies per dispatch: if that instance's process is not bash/sh/zsh,
  input is refused. The gate label comes from the aria-label and can lag for foreground
  children (a `python3` started inside the bash may keep reporting "bash"), so the gate
  protects against wrong-tab targeting, not against your own terminal's foreground state —
  do not leave interactive programs running in a terminal you own.

**Multiple cluster pages** (different web IDE hosts) work the same way: open each page in the
same debug Chrome and give each caller its own `POD_TAB_MATCH` (a substring that uniquely
matches that page's URL). Locks are scoped per page, so callers on different clusters never
block each other:

```bash
POD_TAB_MATCH=cluster-a.example POD_TERM=2 node "$SKILL_DIR/scripts/pod.js" 'hostname'
POD_TAB_MATCH=cluster-b.example POD_TERM=2 node "$SKILL_DIR/scripts/pod.js" 'hostname'
```

## Core Workflow

**Run a command and get its output:**

```bash
node "$SKILL_DIR/scripts/pod.js" 'nvidia-smi -L; python3 -c "import torch; print(torch.__version__)"' 120
```

The second argument is the timeout in seconds (default 60). The exit code is propagated. Each
call runs in its own bash, so **`cd` and `export` do not persist between calls** — chain them
with `&&` or `;` in a single call instead.

**Write code to the remote machine** (this is the part that makes real work possible):

```bash
node "$SKILL_DIR/scripts/podpush.js" ./train.py /mnt/shared/project/train.py
cat <<'EOF' | node "$SKILL_DIR/scripts/podpush.js" - /tmp/patch.py
any content — CJK, quotes, backslashes, newlines are all safe
EOF
```

Content is base64-encoded, so only ASCII enters the terminal; CJK, quotes, `$` and backticks
cannot corrupt anything. Chunks are ~150K characters each (most files are one chunk), each
chunk is verified landed byte-exact before the next, the result is sha256-verified, staged,
`mv`d into place, then **independently re-read and re-hashed** — a failed push never leaves a
corrupt file at the destination. Speed: 10KB ≈ 5s, 100KB ≈ 18s, 400KB ≈ 1min. Above ~1MB,
have the remote host fetch the file itself (`curl`/`git clone`) if it has egress.

**Read files back:**

```bash
node "$SKILL_DIR/scripts/podpull.js" /mnt/shared/project/out.log              # to stdout
node "$SKILL_DIR/scripts/podpull.js" /mnt/shared/project/out.log ./out.log    # byte-exact, binary-safe
```

This does not touch the terminal, so it is fast and safe to use while a command is running —
it is the way to poll a long job's log.

**The edit loop:** pull the file, edit it locally with normal tools, push it back, run it via
`pod.js`. Do not try to drive the IDE's editor UI; it is far more fragile than push/pull.

**Long jobs:** do not hold the terminal. Launch detached and poll the log over the fast channel:

```bash
node "$SKILL_DIR/scripts/pod.js" 'cd /work && nohup python3 train.py > /tmp/train.log 2>&1 & echo started $!'
node "$SKILL_DIR/scripts/podpull.js" /tmp/train.log | tail -20     # repeat as needed
```

A `pod.js` timeout is NOT a failure — it prints partial output plus the job-dir path; the
command keeps running remotely and its `out.txt` can still be pulled.

## Safety rules

These come from real damage done while building this. Read them before your first write.

- **The terminal is shared with the human and with running jobs.** Sending input to the wrong
  tab injects your text into that program's stdin. Every input dispatch (paste, keystroke,
  Ctrl+C) re-checks that the target tab looks like a plain shell in the same atomic page-side
  expression that dispatches it, and aborts if it cannot tell which tab is selected. Do not
  weaken that check or split it into two steps; if it fires, run `podtab.js --auto` or open a
  fresh bash tab.
- **Never run `exit`, `set -e`, or anything that kills the shell** in a command. `pod.js`
  already isolates commands in a separate bash via `base64 -d | bash` for exactly this reason.
  Killing the tab makes the panel auto-switch to a neighbouring tab — which may be a live job.
- **The terminal is an exclusive resource per instance.** `pod.js` and `podpush.js` share a PID
  lockfile scoped to (port, page, `POD_TERM`); a second invocation against the same terminal
  fails fast instead of interleaving input. Parallel callers use distinct `POD_TERM`s.
  `podpull.js` and `podshot.js` take no lock (they never type).
- **Do not use this for interactive or TUI programs** (vim, top, nvitop). Their output cannot
  be read back.
- **This channel has the remote account's full permissions and keeps no audit log.** Stay
  inside the scope the operator asked for. Do not use it to transfer secrets, and do not use it
  to reach a host the operator has not asked you to touch.
- Check `df` before writing: on shared cluster filesystems the workspace is often full. The
  default scratch root is the container-local `/tmp` for this reason.

## Troubleshooting

- **No output file / "the command does not appear to have run"** — the input never landed. Run
  `podshot.js` and look: a clean prompt means just retry; a leading `>` means bash is stuck on
  a continuation line (the scripts send Ctrl+C to clear this, **not Ctrl+U** — Ctrl+U clears
  the buffer but leaves the continuation state).
- **The gate refuses but the tab looks like bash** — VS Code tab labels put the process in
  `.label-name` and the cwd in `.label-description`; if the DOM changed after an IDE upgrade
  the gate reads `__UNKNOWN__` and refuses. Check `TABNAME` in `podlib.js` first.
- **First command works, everything after is silently lost** — VS Code's *terminal sticky
  scroll* creates a second visible xterm with its own textarea that is **not** wired to the
  pty. The picker must exclude `.terminal-sticky-scroll` and prefer `.terminal-wrapper.active`.
- **Push fails its checksum** — a chunk was lost. Just retry; the destination file was not
  touched.
- **`podpull.js` hangs then 504s** — the path is a directory. That endpoint reads files only;
  use `pod.js 'ls -la ...'` to list.
- **404 from `podpull.js`** — the file genuinely does not exist. Any absolute path is readable,
  not just the workspace, so this is not a permissions or scope issue.
- **Cannot connect to the debug port** — Chrome was started without a separate
  `--user-data-dir`, so it reused the existing process. Quit that Chrome and relaunch as in
  Setup.
- **Paste input stops reaching the pty** (after an xterm or VS Code upgrade) — set
  `POD_INPUT=type` to fall back to per-keystroke input (slower: ~9K characters per 4s) and file
  an update to `podlib.js`.

## Detailed Runbook

Measured throughput numbers, the full list of dead ends (`Input.insertText` never reaches the
pty; xterm buffer reads are impossible; `MAX_CANON` does not apply; why synthetic paste works),
DOM selector facts, and the port/tunnel elimination that justified this approach:

[references/webterm-internals.md](references/webterm-internals.md)
