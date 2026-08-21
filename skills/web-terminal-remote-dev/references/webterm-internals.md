# Web Terminal Channel: Internals, Measurements, and Dead Ends

This document records every empirical conclusion behind the `web-terminal-remote-dev` skill.
Read it before changing anything in `scripts/` — each "dead end" below cost real time to
discover, and walking one again produces no new result.

The environment where this was first built: a cloud notebook instance whose SSH ingress was
broken while its browser IDE worked, 2026-08.

---

## 1. Why the browser is the only way in (direct paths were exhausted first)

Before deciding to drive a browser, every one of these was verified as unusable:

| Attempt | Result |
|---|---|
| Platform-mapped ports 80 / 22 / 2222 / 8022 / 8443 / 30022 | all closed. The platform ingress was broken, while `sshd` inside the pod was healthy and listening on 22 |
| Direct connection to the pod IP | unreachable — the container network is outside the VPN's routed range |
| Reverse tunnel from the pod back to the laptop | impossible. The VPN is one-way / behind NAT, so the laptop has no inbound address the pod can reach; port 2222 was tried against all three of the laptop's addresses from the pod, none reachable |
| Install a relay tool on the pod | `socat` / `nc` / `ncat` all absent |
| Restart the instance to fix ingress | explicitly forbidden by the user (a multi-day training job was running on it) |
| Public tunnel (cloudflared / ngrok) | the pod has egress (`curl` to a public host returns 200), so this is technically possible, but it would expose an internal machine's SSH to the public internet — **do not do this without explicit authorization** |

Conclusion: the only usable entry point is the already-authenticated web IDE page on 443.
It is already inside the network perimeter and already carries a valid session cookie, so
"drive that page" requires no new port and no tunnel.

---

## 2. Channel characteristics (measured)

### 2.1 Output channel: `/vscode-remote-resource` — fast

The IDE's own file-read endpoint. It must be built on the full base path:

```js
const base = location.origin + location.pathname.replace(/\/$/, '');
const url  = base + '/vscode-remote-resource?path=' + encodeURIComponent(absPath);
await fetch(url, { credentials: 'include', cache: 'no-store' });
```

| Property | Measured |
|---|---|
| 1MB text | ~1003 ms |
| 8MB text | ~106 ms (once cached) |
| Binary integrity | byte-identical through `arrayBuffer`; md5 matched the remote side |
| Path scope | **any absolute path**, not limited to the workspace (`/tmp`, `/root`, `/etc/hostname` all readable) |
| Passing a directory | **the server hangs for 180 seconds and then 504s** — never pass a directory; list one with `pod.js 'ls'` |
| Missing file | 404 |
| Range requests | **unsupported**; a `Range` header is ignored and the whole file is returned, so there is no resume |

This channel does not touch the terminal, so a long job's log can be polled repeatedly and
safely while that job runs.

### 2.2 Input channel v2 (current default): synthetic paste events — fast

xterm.js listens for `paste` on its `.xterm-helper-textarea`, and **a synthetic
ClipboardEvent reaches the pty directly**:

```js
const dt = new DataTransfer();
dt.setData('text/plain', line);
ta.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
```

Measured on one remote pod:

| Property | Measured |
|---|---|
| 10K characters | 1.24 s (~8.1 chars/ms) |
| 30K characters | 3.47 s (~8.6 chars/ms) |
| 60K characters | 6.63 s (~9.0 chars/ms) |
| 200K / 400K characters | 21.8 s / 42.3 s, **byte-exact** |
| Stability | 2KB × 10 consecutive sends, 10/10 |
| Synthetic Enter (keydown) | reaches the pty, and **needs no page focus** (no mouse click, no CDP Input) |
| Synthetic Ctrl+C (keydown with ctrlKey) | resets readline's continuation state |

Throughput of ~9 chars/ms is **flat, with no degradation as length grows** — completely
unlike the keystroke channel's collapse. That is why `CFG.pasteMax = 150000` (one paste in
~17s, which makes the timeout window easy to choose) and why most file pushes complete in a
single chunk.

bash's bracketed paste buffers the whole payload as one line and executes it on a single
Enter, so the semantics match typing it.

Paste does not depend on focus, but it is dispatched at the *selected* terminal's textarea,
so **the safety gate must re-check the tab on every dispatch** (the user can switch tabs at
any moment).

### 2.3 Input channel v1 (fallback, `POD_INPUT=type`): per-keystroke — slow

```js
await Promise.all([...line].map(ch =>
  send('Input.dispatchKeyEvent', { type: 'char', text: ch })));
await sleep(200);
await send('Input.dispatchKeyEvent', { type: 'char', text: '\r' });
```

Dispatching in bulk and awaiting together is about **4× faster** than awaiting each
character; measured 6/6 reliable. It needs real focus (a mouse click on the terminal plus an
`activeElement` confirmation).

**Length and throughput** (the key point: there is no hard ceiling, but throughput collapses):

| Line length | Time |
|---|---|
| 9 KB | 4.1 s |
| 14 KB | 14 s |
| 20 KB | 40 s |

So **splitting at ~9000 characters is about 3× faster than one giant line**, which is where
`CFG.maxLine = 9000` comes from. This channel is kept for exactly one scenario: if a future
xterm release breaks synthetic paste, there is still a measured-reliable way in.

Note: `MAX_CANON` (the termios canonical-mode 4096-byte line limit) **does not apply here** —
xterm does not use canonical mode. An early belief that MAX_CANON was the limit turned out to
be an artifact of stale scratch files (see 3.5).

---

## 3. Dead ends and traps (in the order they were hit)

### 3.1 Reading terminal text: impossible

xterm.js renders to a **canvas**, so the characters on screen are not in the DOM. All of
these were tried and all failed:

- `innerText` / `textContent` on `.xterm-screen` → empty or truncated
- the accessibility buffer (`.xterm-accessibility`) → incomplete and lagging
- reading xterm's internal buffer object → the terminal instance is not reachable from page scope

**This is the root reason the whole design must be "redirect output to a file and fetch it
back."** The only way to see the terminal's screen at all is `Page.captureScreenshot` (i.e.
`podshot.js`), which is for diagnosis only — never for retrieving data.

### 3.2 `Input.insertText`: completely ineffective

It looks like the ideal API (one call, whole payload), but **the text never reaches the pty**.
The call returns normally in 2–4ms with no error and nothing appears in the terminal. Tried at
141 / 500 / 1000 / 2000 / 4110 characters; length is irrelevant. Abandoned for good.

(**A synthetic ClipboardEvent paste is a different thing entirely**: paste goes through the
event listener xterm.js registers on its own textarea, and it is measured reliable — see 2.2.
`Input.insertText` is a CDP-level IME simulation that xterm does not consume.)

### 3.3 Sticky scroll's fake terminal: the hardest bug to find

Symptom: **the first command succeeds and every command after it is silently lost.**

Cause: VS Code's **terminal sticky scroll** (`.terminal-sticky-scroll`) creates a second
*visible* xterm instance with its own `.xterm-helper-textarea` that is **not wired to a pty**.
It only appears once output starts scrolling off screen — which is why the first command (with
the screen not yet full) works and everything afterwards has its focus stolen by the fake
terminal.

A correct selector must satisfy three conditions at once:

```js
const real = (el) => el.offsetParent && !el.closest('.terminal-sticky-scroll');
const inActive = (el) => !!el.closest('.terminal-wrapper.active');
// offsetParent: exclude hidden 0x0 elements (clicking one dispatches the click at (0,0) and loses focus)
// exclude sticky-scroll: exclude the fake terminal
// prefer .terminal-wrapper.active: the selected one when several terminals exist
```

### 3.4 Non-ASCII in a command destroys the whole line

An `echo "=== ... ==="` whose text contained CJK characters turned into a stray path on the
command line instead.

Cause: readline interprets non-ASCII bytes as **Meta key bindings** (here it triggered
`yank-last-arg`), which rewrote the line's content and left bash sitting on a `>`
continuation prompt, so everything afterwards was swallowed as an unterminated string.

Fix: **base64 every payload** so only ASCII is ever typed into the terminal. Both `pod.js`
and `podpush.js` do this, which makes CJK, single and double quotes, backslashes, `$HOME`,
backticks and pipes all safe. Verified with a file containing every one of those characters.

### 3.5 Reusing a fixed scratch filename → false success

An early version used a fixed `.out.txt`. When a command failed to run, the script silently
read **the previous command's stale output**, which looked exactly like success. The timeout
case was worse: the previous command was still writing to that same file in the background.

Fix: generate a unique tag per call and carry it in both the scratch filenames and the
completion marker.

**This bug also contaminated the early length benchmarks** — the "even short commands fail"
observation was an artifact of stale reads, not a real length limit. Any throughput conclusion
drawn before this fix is untrustworthy.

### 3.6 Ctrl+U is not enough to reset

Once bash is on a `>` continuation prompt (unclosed quote), **Ctrl+U clears only the line
buffer while the continuation state remains**, so everything typed afterwards continues to be
swallowed. It must be **Ctrl+C** (`String.fromCharCode(3)`).

### 3.7 Chunked push lost chunks — because Ctrl+C interrupted the previous printf

Symptom: a 40813-byte file (54420 base64 characters, 7 chunks) arrived as **853 bytes**,
containing a mid-line fragment from the tail; `python3` on the remote side reported a
`SyntaxError`.

Cause: each chunk was preceded by a Ctrl+C reset followed by a fixed `sleep(250)`. The
previous `printf` was still running, and the next round's Ctrl+C interrupted it — so most
chunks' appends were lost.

Fix (the current implementation):

1. **wait for remote confirmation that each chunk landed before sending the next** — every
   command carries `echo <tag>:<i> > prog` and the next step waits until it is polled back;
2. therefore **no Ctrl+C between chunks** (the commands complete cleanly, so there is no
   residual line to clear);
3. decode to a staging file first and only `mv` onto the destination after the sha256
   verifies — **a failure never leaves a corrupt file at the destination**.

Verification: a 40813-byte file pushed → sha256 matched → pulled back and `diff` was
byte-identical → the remote `import` produced 400/400 functions with correct call results.
About 60 seconds.

### 3.8 The worst incident: typing a command into a running job

During testing a command contained `exit 3`. It executed in **the terminal's own shell**, so
that bash tab closed outright, the panel **auto-switched to a neighbouring tab that was
running an 8-GPU training job in tmux**, and the next two commands were typed into that
training process's stdin.

(Confirmed afterwards that the training job survived: 1 day 9 hours in, step 21306/1000000,
both tmux sessions intact.)

Two defences (**do not weaken either**):

1. every command runs in a separate bash: `printf %s <b64> | base64 -d | bash`, so `exit` or
   `set -e` cannot harm the terminal;
2. before typing, check that the selected tab's name matches `/^(bash|sh|zsh)\b/i` and refuse
   otherwise; **refuse equally when the selected tab cannot be identified** (an early version
   degraded to "use the last tab", which is dangerous silent behaviour when a tmux tab exists).

DOM facts about the selected tab (check here first after a VS Code upgrade):

- the selection marker is on the **ancestor** `.monaco-list-row[aria-selected="true"]`, **not**
  on `.terminal-tabs-entry` itself;
- the entry's own class is **`is-active`** (not `active`);
- with a single terminal there is **no tab list** at all (in which case mis-selection is
  impossible, so it is allowed).

This logic exists in exactly one place, `TABNAME` in `podlib.js` — do not write a second copy
elsewhere. A subtly wrong copy would not error; it would silently let input through.

---

## 4. Intermediate file management (v2: job directories plus automatic sweeping)

v1 scattered `out_<tag>.txt` / `done_<tag>.txt` / `push_<tag>.b64` flat under the scratch
root and only cleaned up on the success path — so every failure, timeout or dropped keystroke
left debris. Within days 181 loose files had accumulated, including malformed names like
`done_T44481_4704458.txtrm` (the previous command's Enter was dropped, so the next command's
text was appended onto the filename).

v2 rules:

- **one job directory per call**, `<tmpDir>/jobs/<tag>/`, holding every intermediate that call
  produces (out.txt, done.txt, c_0…, stage, prog, list);
- the tag changed from random digits to **`jMMDD_HHMMSS_pid`** — human readable and
  time-sortable, so `ls jobs/` immediately shows which call left what;
- **success → `rm -rf` the whole directory immediately**; failure or timeout → leave it in
  place for diagnosis;
- **each call's preamble sweeps** job directories older than `POD_SWEEP_MIN` (default 1440
  minutes = 24h) on the same command line as the real work, costing no extra round trip. A
  long job whose directory is still fresh is unaffected.

Steady state: the remote host holds only the job directories of currently running calls, and
loose files never accumulate.

---

## 5. Environment facts that shaped the defaults

- The shared cluster filesystem showed `df` at 100% full → scratch files must live on the
  container-local `/tmp` (overlay, 3.0T available). That is why `CFG.tmpDir` defaults to
  `/tmp/.webterm-pod` — and it is fine, because the read endpoint can read any absolute path.
- Terminal tabs may hold long-running tmux or python jobs — this is the reason the safety gate
  exists at all.
- The pod had egress but no ingress.

---

## 6. File responsibilities

| File | Responsibility |
|---|---|
| `podlib.js` | shared CDP library: `connect` / `readFile` / `sendLine` (paste or keystrokes) / `resetLine` / `runConfirmed` / `waitFor` / `lockGuard`, plus `CFG`, the `PICK` selector, the `TABNAME` safety gate (`assertSafeTab` runs before every input dispatch), and job-directory helpers `newTag` + `preamble` |
| `pod.js` | run a command and retrieve its text output. Job directory + PID lock + an honest timeout report (distinguishing "still running" from "input never landed"); deletes the directory on success |
| `podpush.js` | write a file to the remote host. ~150K-character paste chunks + per-chunk read-back verification + sha256 + staging then mv + destination re-check; takes the same PID lock |
| `podpull.js` | read a remote file (no terminal, no lock). Always transferred via arrayBuffer, byte faithful, binary safe |
| `podtab.js` | list terminal tabs / change the selected tab. Mouse events only; an allowlist permits switching to bash/sh/zsh only, and the result is read back to confirm |
| `podshot.js` | screenshot plus a print of tab and focus state. Diagnosis only; deliberately does not change page state |

`pod.js` and `podpush.js` share one PID lock (in `$TMPDIR`, with stale locks cleared
automatically) because the terminal is an exclusive resource; parallel calls against the same
terminal fail with an error instead of trampling each other. `podpull.js` and `podshot.js`
send no input and take no lock.

## 7. Measured performance (v1 → v2, same host)

| Operation | v1 (keystrokes) | v2 (paste) |
|---|---|---|
| Run one `echo` | 5.5 s | **1.6 s** |
| Push a 10KB file | 14.6 s (2 chunks) | **5.3 s** (1 chunk) |
| Push a 100KB file | ~90 s (15 chunks) | **17.9 s** (1 chunk) |
| Push a 400KB file | impractical | **62 s** (4 chunks, byte-exact on re-check) |

## 8. Parallel agents (POD_TERM directed dispatch, measured)

The core fact: **a background (non-selected) terminal keeps both its xterm DOM and its
textarea alive**, and a synthetic paste dispatched at it lands in **its own pty**. Verified by:

- background Terminal 2 (`/dev/pts/7`) and foreground Terminal 5 (`/dev/pts/1`) receiving
  different commands at the same time, each executing in its own tty — the input streams are
  isolated at the pty layer;
- two terminals pasting 520 characters concurrently for 5 rounds, 5/5 with no interleaving
  (each dispatch is one atomic JS block in the page, and xterm's event handling is synchronous
  and single-threaded, so it cannot be split apart);
- two agents concurrently pushing 30KB each to different paths, both sha256-identical; three
  agents running `pod.js` concurrently, all succeeding.

Addressing: the terminal **instance number** comes from the textarea's aria-label
`"Terminal N, <proc>"`. VS Code assigns N and it **does not drift when other tabs close** (tab
list indices do drift and cannot be used as identity). `podtab.js` prints each tab's
`POD_TERM=N` directly.

Limits and gate semantics:

- **a tab that has never been viewed has no xterm DOM** (lazy instantiation); switch to it once
  with `podtab.js <idx>`;
- directed mode's gate reads the process name from that instance's aria-label. Measured, that
  label **updates very sluggishly for foreground children**: 20s after starting `python3`
  inside the bash the label still read "bash" (a marker file confirmed python was running). So
  the gate protects against **typing into the wrong tab**, not against a foreground program
  left running in your own terminal — an agent must leave its own terminal at a clean prompt
  and not leave interactive programs in it;
- the keystroke channel (POD_INPUT=type) must take real focus and therefore cannot target a
  background terminal; the two are mutually exclusive and the scripts error out at startup;
- lock scope = (CDP port, page match string, terminal instance). Different `POD_TERM`s never
  wait on each other; the same `POD_TERM` serializes; **default mode (driving the selected tab)
  and directed mode are not mutually exclusive**, so a parallel fleet must give every member
  its own `POD_TERM`.

Multiple cluster pages: open several web IDE pages in the same debug Chrome and give each agent
a `POD_TAB_MATCH` that uniquely matches its own page URL. Locks are scoped per page, so agents
on different clusters are fully independent.
