#!/usr/bin/env node
// Shared CDP driver for a browser-based IDE terminal (code-server / VS Code Web).
// Used by pod.js / podpush.js / podpull.js / podtab.js / podshot.js.
//
// Why this exists: on some hosts SSH is genuinely unreachable (broken platform ingress,
// no route to the container network, reverse tunnels impossible) while the web IDE page
// opens fine. That page is already authenticated and already inside the perimeter, so
// driving it needs no new port and no tunnel. Two channels:
//   input  = dispatch a synthetic paste event at the xterm textarea (v2, ~9 chars/ms and
//            flat with length; the older per-keystroke channel was ~2 chars/ms and got
//            rapidly worse with length -- it is kept as the POD_INPUT=type fallback);
//   output = redirect to a file, then fetch it back through the IDE's own
//            /vscode-remote-resource endpoint.
//
// Intermediate files (v2): every remote scratch file lives in one per-call job directory
//   under CFG.jobRoot (jobs/<tag>/out.txt, done.txt, c_0...). Success deletes it at once,
//   failure keeps it for diagnosis, and each call's preamble sweeps job directories older
//   than POD_SWEEP_MIN minutes -- so the steady state leaves no remote litter.
//
// Every measurement and dead end is recorded in references/webterm-internals.md. Read it
// before changing anything here.

const fs = require('fs');
const os = require('os');
const path = require('path');

const CFG = {
  port: Number(process.env.POD_CDP_PORT || 9222),
  tabMatch: process.env.POD_TAB_MATCH || 'codeserver',
  // Scratch root. The default is the container-local /tmp rather than a shared cluster
  // filesystem: shared mounts are frequently at 100% capacity, so writing there can
  // ENOSPC at any moment. /vscode-remote-resource reads any absolute path, not just the
  // workspace, so /tmp works fine as a staging area.
  tmpDir: process.env.POD_TMP_DIR || '/tmp/.webterm-pod',
  // Input channel: 'paste' (default, fast) | 'type' (per-keystroke, slow, kept as an
  // escape hatch in case a future xterm release breaks synthetic paste).
  input: process.env.POD_INPUT || 'paste',
  // Per-paste character ceiling. A single 400K-character paste was measured byte-exact
  // (~9 chars/ms, flat); 150K keeps one paste under ~17s, which makes timeouts easy to set.
  pasteMax: Number(process.env.POD_PASTE_MAX || 150000),
  // Per-line ceiling for type mode. Throughput collapses past ~12K characters
  // (9KB -> 4.1s, 20KB -> 40s).
  maxLine: Number(process.env.POD_MAX_LINE || 9000),
  // Job-directory sweep threshold in minutes. Directories left by a failure or timeout
  // live at most this long.
  sweepMin: Number(process.env.POD_SWEEP_MIN || 1440),
  safeShells: /^(bash|sh|zsh)\b/i,
};
// tmpDir is interpolated bare into remote command lines (redirect targets, rm, a for
// loop), so it must be restricted to a safe character set -- otherwise a space, quote or
// semicolon silently changes command semantics. Reject it once here and no later
// interpolation has to worry about it.
if (!/^\/[A-Za-z0-9._\/-]+$/.test(CFG.tmpDir)) {
  console.error(`POD_TMP_DIR contains unsafe characters (allowed: letters, digits, . _ / -): ${CFG.tmpDir}`);
  process.exit(1);
}
CFG.jobRoot = CFG.tmpDir + '/jobs';

// ---- Parallel agents: POD_TERM=<terminal instance> for directed dispatch ----
// Measured fact: a background (non-selected) terminal keeps its xterm DOM and textarea
// alive, and a synthetic paste dispatched at it lands in *its own* pty (distinct
// /dev/pts/N). Two terminals pasting concurrently showed no interleaving in 5/5 rounds
// (each dispatch is one atomic JS block in the page, and xterm's event handling is
// synchronous and single-threaded). So each agent claims one terminal instance and gets
// fully isolated input and output -- without ever touching the selected tab, which means
// the human's visible terminal never moves.
// The terminal instance number comes from the textarea aria-label "Terminal N". VS Code
// assigns it and it does NOT drift when other tabs close (tab list indices do drift, so
// they cannot be used as identity). Use podtab.js to see each tab's instance number.
CFG.term = process.env.POD_TERM ? Number(process.env.POD_TERM) : null;
if (CFG.term !== null && (!Number.isInteger(CFG.term) || CFG.term < 1)) {
  console.error(`POD_TERM must be a positive integer (terminal instance number; see podtab.js): ${process.env.POD_TERM}`);
  process.exit(1);
}
if (CFG.term !== null && CFG.input === 'type') {
  console.error('POD_TERM directed mode requires the paste channel: per-keystroke input (POD_INPUT=type) needs real page focus and cannot target a background terminal.');
  process.exit(1);
}

// Locate the textarea that is actually wired to the pty. All three filters are required:
//   offsetParent: excludes hidden 0x0 elements;
//   exclude .terminal-sticky-scroll: VS Code's sticky scroll builds a second *visible*
//     xterm that is NOT wired to a pty (symptom: the first command works and every
//     command after it is silently lost);
//   prefer .terminal-wrapper.active: the selected one when several terminals exist.
const PICK = `(() => {
  const real = (el) => el.offsetParent && !el.closest('.terminal-sticky-scroll');
  const inActive = (el) => !!el.closest('.terminal-wrapper.active');
  const scr = Array.from(document.querySelectorAll('.xterm-screen')).filter(real);
  const ta = Array.from(document.querySelectorAll('.xterm-helper-textarea')).filter(real);
  return [scr.find(inActive) || scr[0], ta.find(inActive) || ta[0]];
})`;

// Read the *process name* of the currently selected terminal tab. This is the safety gate
// that keeps commands out of a long-running job's stdin, so there is exactly one
// implementation of it -- do not write a second one elsewhere (a subtly wrong copy would
// not error, it would silently allow input through).
// DOM facts (check here first after a VS Code upgrade):
//   - the selection marker sits on the ancestor .monaco-list-row[aria-selected="true"],
//     not on the .terminal-tabs-entry itself;
//   - the entry's own class is "is-active" (not "active");
//   - the process name is in .label-name and **the cwd is in .label-description**. Only
//     .label-name may be read: innerText concatenates both (observed
//     "bashpush_T50880_9493875"), so /^bash\b/ stops matching and the gate rejects a
//     perfectly normal bash;
//   - with a single terminal there is no tab list at all.
const TABNAME = `(() => {
  const rows = Array.from(document.querySelectorAll('.terminal-tabs-entry'));
  if (!rows.length) return '__NO_TABS__';
  const sel = rows.find(r => r.closest('[aria-selected="true"]')) ||
              rows.find(r => /\\bis-active\\b/.test(r.className));
  if (!sel) return '__UNKNOWN__';
  const n = sel.querySelector('.label-name');
  return ((n ? n.textContent : sel.innerText) || '').trim().split('\\n')[0];
})`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Local mutex: **one terminal instance may only have one process typing into it**,
// otherwise the command lines interleave into garbage.
// Lock scope = (CDP port, page match string, terminal instance):
//   - different cluster pages (different POD_TAB_MATCH / POD_CDP_PORT) are separate
//     terminal universes and never block each other;
//   - within one page, POD_TERM=N locks terminal N; default mode takes that page's
//     global lock (it drives whichever terminal is selected, and the selection can
//     change, so it can only be mutually exclusive as a whole).
// Parallel agents: give each agent its own POD_TERM (or its own page) and they never wait.
// Note: within one page the default-mode global lock and a POD_TERM lock are **not**
// mutually exclusive -- if the selected terminal happens to be some agent's directed
// terminal, mixing the two modes still interleaves. For parallel work either give every
// caller a POD_TERM, or keep exactly one default-mode caller.
const lockScope = `${CFG.port}-${CFG.tabMatch.replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 40)}-${CFG.term === null ? 'sel' : CFG.term}`;
const LOCK = path.join(os.tmpdir(), `webterm-pod-terminal-${lockScope}.lock`);
function acquireLock(depth = 0) {
  try {
    fs.writeFileSync(LOCK, String(process.pid), { flag: 'wx' });
    // Read back to verify: if two processes both see a stale lock they both unlink it, so
    // whoever won the wx race can be deleted by the other's unlink and then recreated --
    // leaving someone else's pid in the file. If the read-back is not us, concede and retry.
    const got = fs.readFileSync(LOCK, 'utf8').trim();
    if (got !== String(process.pid)) return acquireLock(depth + 1);
    return;
  } catch (e) {
    if (e.code !== 'EEXIST') throw e;
    if (depth > 5) { console.error('error: abnormal lock contention, please retry shortly.'); process.exit(1); }
    let pid = NaN;
    try { pid = Number(fs.readFileSync(LOCK, 'utf8').trim()); } catch { /* someone just removed it */ }
    try { process.kill(pid, 0); } catch { try { fs.unlinkSync(LOCK); } catch {} return acquireLock(depth + 1); }  // stale, clear it
    console.error(`error: another pod command is running (pid ${pid}). The web terminal is an exclusive resource and cannot be driven in parallel.\n` +
                  `If that process is definitely dead, delete ${LOCK} and retry.`);
    process.exit(1);
  }
}
// Only remove a lock we hold -- an unconditional unlink would delete the lock a
// concurrent new process just acquired.
function releaseLock() {
  try { if (fs.readFileSync(LOCK, 'utf8').trim() === String(process.pid)) fs.unlinkSync(LOCK); } catch {}
}
// Register a last-resort release after acquiring; the normal path should still call
// releaseLock explicitly.
function lockGuard() {
  acquireLock();
  process.on('exit', releaseLock);
  process.on('SIGINT', () => { releaseLock(); process.exit(130); });
  process.on('SIGTERM', () => { releaseLock(); process.exit(143); });
}

async function connect() {
  let targets;
  try {
    targets = await (await fetch(`http://127.0.0.1:${CFG.port}/json`)).json();
  } catch {
    throw new Error(
      `cannot reach the Chrome debug port ${CFG.port}. Start Chrome with a debug port first:\n` +
      `  open -na "Google Chrome" --args --remote-debugging-port=${CFG.port} \\\n` +
      `    --user-data-dir="$HOME/.chrome-webterm-debug" --no-first-run --no-default-browser-check "<web IDE URL>"\n` +
      `(--user-data-dir must be separate, or Chrome reuses the running instance and the debug port does nothing)`
    );
  }
  const pages = targets.filter((t) => t.type === 'page' && t.url.includes(CFG.tabMatch));
  if (!pages.length) {
    throw new Error(`no Chrome tab has a URL containing "${CFG.tabMatch}". Open the web IDE page in that Chrome window.` +
      `\ncurrent tabs: ${targets.filter(t => t.type === 'page').map(t => t.url.slice(0, 70)).join(' | ') || '(none)'}`);
  }
  if (pages.length > 1) {
    console.error(`note: ${pages.length} tabs matched, using the first (${pages[0].url.slice(0, 60)}). ` +
      `Narrow the match with POD_TAB_MATCH.`);
  }
  const page = pages[0];

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let id = 0;
  const pending = new Map();
  let deadReason = null;
  // Every request needs a timeout, and every pending request must be rejected when the
  // socket closes. Otherwise a page reload, a renderer crash or a network blip leaves a
  // promise that never settles -- the node process hangs forever with no output at all,
  // and the agent calling it hangs with it. That is the worst failure mode: no evidence.
  const send = (method, params = {}, timeoutMs = 30000) =>
    new Promise((resolve, reject) => {
      if (deadReason) return reject(new Error(deadReason));
      const msgId = ++id;
      const timer = setTimeout(() => {
        pending.delete(msgId);
        reject(new Error(`CDP request timed out after ${timeoutMs}ms: ${method} (page wedged, or tab closed?)`));
      }, timeoutMs);
      pending.set(msgId, { resolve, reject, timer });
      try { ws.send(JSON.stringify({ id: msgId, method, params })); }
      catch (e) { clearTimeout(timer); pending.delete(msgId); reject(e); }
    });
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject, timer } = pending.get(msg.id);
      clearTimeout(timer);
      pending.delete(msg.id);
      msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
    }
  });
  const killAll = (why) => {
    deadReason = why;
    for (const [, p] of pending) { clearTimeout(p.timer); p.reject(new Error(why)); }
    pending.clear();
  };
  ws.addEventListener('close', () => killAll('CDP connection closed (browser quit / tab closed / page crashed)'));
  ws.addEventListener('error', () => killAll('CDP connection error'));
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', () => reject(new Error('WebSocket connection failed')), { once: true });
  });

  const evaluate = async (expression, timeoutMs = 30000) => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, timeoutMs);
    if (r.exceptionDetails) {
      throw new Error('in-page exception: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
    }
    return r.result.value;
  };

  // Read a remote file. Note: path must be a file -- passing a directory makes the server
  // hang for 180s and then 504.
  // Always transfer via arrayBuffer + base64, never r.text(): text() silently rewrites
  // non-UTF-8 bytes as U+FFFD, so a latin-1 log or output with binary in it gets
  // irreversibly corrupted with no warning. Decoding is left to a local Buffer, so body
  // is always a faithful reconstruction of the original bytes.
  const readFile = async (path) => {
    const expr = `(async () => {
      const base = location.origin + location.pathname.replace(/\\/$/, '');
      const url = base + '/vscode-remote-resource?path=' + encodeURIComponent(${JSON.stringify(path)});
      try {
        const r = await fetch(url, {credentials: 'include', cache: 'no-store'});
        if (r.status !== 200) return JSON.stringify({status: r.status});
        const b = new Uint8Array(await r.arrayBuffer());
        let s = ''; const CH = 8192;
        for (let i = 0; i < b.length; i += CH) s += String.fromCharCode.apply(null, b.subarray(i, i + CH));
        return JSON.stringify({status: 200, b64: btoa(s), len: b.length});
      } catch (e) { return JSON.stringify({status: -1, err: String(e).slice(0, 200)}); }
    })()`;
    const r = JSON.parse(await evaluate(expr, 200000));
    if (r.status === 200) {
      r.buf = Buffer.from(r.b64, 'base64');
      r.body = r.buf.toString('utf8');
    }
    return r;
  };

  // Safety gate: every input dispatch (paste / keystroke / Ctrl+C) must pass this first.
  // Paste needs no page focus, but it is dispatched at the *selected* terminal's textarea
  // -- and if the selected tab is a long-running job, the content lands in that process's
  // stdin. So re-check before every input: the user can switch tabs at any moment.
  const assertSafeTab = async () => {
    const tab = await evaluate(`${TABNAME}()`);
    // With a single terminal there is no tab list and no label to read -- allow that case
    // (there is no other tab to mis-target).
    if (tab === '__UNKNOWN__') {
      throw new Error(
        'there are multiple terminal tabs but the selected one cannot be identified (VS Code upgrade? selection-marker DOM changed?).\n' +
        'Refusing to send input -- if the selected tab were a long-running job, the command would go into its stdin.\n' +
        `Run node ${path.join(__dirname, 'podshot.js')} to look, and check the TABNAME selector in podlib.js.`
      );
    }
    if (tab !== '__NO_TABS__' && !CFG.safeShells.test(tab)) {
      throw new Error(
        `the selected terminal tab is "${tab}", not a plain shell. Refusing to send input -- it would go into that program's stdin.\n` +
        `Run node ${path.join(__dirname, 'podtab.js')} --auto to switch to a shell tab, or click + in the IDE to open a new one.`
      );
    }
  };

  // Focus the real terminal and confirm it (only type mode needs this; paste dispatches
  // events straight at the textarea and does not depend on focus).
  // A programmatic focus() is not enough -- a real mouse click must be dispatched.
  const focusTerminal = async () => {
    await assertSafeTab();
    const t = JSON.parse(await evaluate(`(() => {
      const [s, ta] = ${PICK}();
      if (!s || !ta) return JSON.stringify({ok: false});
      ta.focus();
      const r = s.getBoundingClientRect();
      return JSON.stringify({ok: true, x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height * 0.82)});
    })()`));
    if (!t.ok) throw new Error('no visible terminal on the page. Open the terminal panel in the IDE (Ctrl+`).');
    for (const type of ['mousePressed', 'mouseReleased']) {
      await send('Input.dispatchMouseEvent', { type, x: t.x, y: t.y, button: 'left', clickCount: 1 });
    }
    await sleep(350);
    const ok = await evaluate(`(() => document.activeElement === ${PICK}()[1])()`);
    if (!ok) throw new Error("focus did not land on the real terminal (sticky scroll's fake xterm may have taken it); aborting so the command is not silently lost");
  };

  // ---- Input channel v2: synthetic paste events (default) ----
  // xterm.js listens for paste/keydown on its textarea, so a synthetic event reaches the
  // pty directly with no CDP focus and no mouse click. A single 400K-character paste was
  // measured byte-exact at a flat ~9 chars/ms; a small stress run was 10/10 stable.
  // bash's bracketed paste treats the whole payload as one line, and the synthetic Enter
  // executes it.
  //
  // The gate and the dispatch run atomically in **one page-side expression** -- split
  // across two evaluates, the user could switch tabs in between and the content would
  // land in the newly selected program's stdin (TOCTOU).
  //
  // Two addressing modes:
  //   default     -> the currently selected terminal (gate = tab name matches safeShells)
  //   POD_TERM=N  -> terminal instance N regardless of which tab is selected (gate = the
  //                  proc field of that terminal's own aria-label "Terminal N, <proc>"
  //                  matches safeShells). A background terminal receives the paste in its
  //                  own pty, which is what makes parallel agents possible.
  const GATED_DISPATCH = (bodyJs) => CFG.term === null ? `(() => {
    const tab = ${TABNAME}();
    if (tab !== '__NO_TABS__' && !${CFG.safeShells.toString()}.test(tab)) return 'unsafe:' + tab;
    const [, ta] = ${PICK}();
    if (!ta) return 'no-textarea';
    ${bodyJs}
    return 'ok';
  })()` : `(() => {
    const tas = Array.from(document.querySelectorAll('.terminal-wrapper .xterm-helper-textarea'))
      .filter(el => !el.closest('.terminal-sticky-scroll'));
    const pick = tas.map(el => {
      const m = (el.getAttribute('aria-label') || '').match(/^Terminal (\\d+), ([^\\n]*)/);
      return m ? { el, num: Number(m[1]), proc: m[2].trim() } : null;
    }).filter(Boolean).find(t => t.num === ${CFG.term});
    if (!pick) return 'no-term';
    if (!${CFG.safeShells.toString()}.test(pick.proc)) return 'unsafe:' + pick.proc;
    const ta = pick.el;
    ${bodyJs}
    return 'ok';
  })()`;

  const gateError = (r) => {
    if (r === 'no-textarea') return new Error('no visible terminal on the page. Open the terminal panel in the IDE (Ctrl+`).');
    if (r === 'no-term') return new Error(
      `terminal instance ${CFG.term} (POD_TERM) is not on the page. Was it closed?\n` +
      `Run node ${path.join(__dirname, 'podtab.js')} to list the existing terminals and their instance numbers.`);
    if (String(r).startsWith('unsafe:')) {
      const tab = String(r).slice(7);
      if (tab === '__UNKNOWN__') return new Error(
        'there are multiple terminal tabs but the selected one cannot be identified (VS Code upgrade? selection-marker DOM changed?).\n' +
        'Refusing to send input -- if the selected tab were a long-running job, the command would go into its stdin.\n' +
        `Run node ${path.join(__dirname, 'podshot.js')} to look, and check the TABNAME selector in podlib.js.`);
      return new Error(
        `${CFG.term !== null ? `terminal instance ${CFG.term} is running` : 'the selected terminal tab is'} "${tab}", not a plain shell. Refusing to send input -- it would go into that program's stdin.\n` +
        `Run node ${path.join(__dirname, 'podtab.js')} to list terminals and pick an idle shell${CFG.term !== null ? ' for POD_TERM' : ''}, or click + in the IDE to open a new one.`);
    }
    return null;
  };

  const pasteLine = async (line) => {
    if (!/^[\x20-\x7e]*$/.test(line)) throw new Error('internal error: payload contains non-ASCII or control characters');
    if (line.length > CFG.pasteMax) throw new Error(`paste too long (${line.length} > ${CFG.pasteMax}); split into chunks first`);
    const r = await evaluate(GATED_DISPATCH(`
      const dt = new DataTransfer();
      dt.setData('text/plain', ${JSON.stringify(line)});
      ta.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
      ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true}));
    `), 60000);
    const err = gateError(r);
    if (err) throw err;
    // paste -> xterm -> pty is asynchronous; allow landing time at the measured ~8 chars/ms
    await sleep(Math.min(2000, 120 + line.length / 8));
  };

  // Input channel v1: per-keystroke typing (slow; enabled by POD_INPUT=type, kept as the
  // escape hatch if an xterm upgrade ever breaks synthetic paste).
  let focused = false;
  const typeLine = async (line) => {
    if (!/^[\x20-\x7e]*$/.test(line)) throw new Error('internal error: payload contains non-ASCII or control characters');
    if (line.length > CFG.maxLine * 1.5) throw new Error(`line too long (${line.length}); split into chunks first`);
    if (!focused) { await focusTerminal(); focused = true; }
    else await assertSafeTab();
    await Promise.all([...line].map((ch) => send('Input.dispatchKeyEvent', { type: 'char', text: ch })));
    await sleep(200);
    await send('Input.dispatchKeyEvent', { type: 'char', text: '\r' });
  };

  // Single entry point: dispatch via paste or keystrokes according to CFG.input.
  // Callers only use this.
  const sendLine = (line) => (CFG.input === 'type' ? typeLine(line) : pasteLine(line));
  // Per-line ceiling, used to size chunks.
  const lineBudget = () => (CFG.input === 'type' ? CFG.maxLine : CFG.pasteMax);

  // Ctrl+C reset. It must be Ctrl+C and not Ctrl+U: once bash is sitting on a ">"
  // continuation prompt (unclosed quote), Ctrl+U only clears the line buffer while the
  // continuation state remains, so everything typed afterwards is swallowed as part of
  // the unterminated string.
  // A synthetic keydown reaches the pty without focus; gate and dispatch run atomically in
  // one expression (a Ctrl+C delivered into a long-running job would kill it outright,
  // which makes the TOCTOU window here the most dangerous one).
  const resetLine = async () => {
    const r = await evaluate(GATED_DISPATCH(`
      ta.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', code: 'KeyC', keyCode: 67, which: 67, ctrlKey: true, bubbles: true, cancelable: true}));
    `));
    const err = gateError(r);
    if (err) throw err;
    await sleep(200);
  };

  // Send one command and **confirm it actually ran**; retry when it is not confirmed.
  // This channel can drop input (measured roughly once every few dozen lines in keystroke
  // mode; paste mode is far more stable, but a page reload or a stall can still swallow
  // one). So every command carries a sentinel, is read back for confirmation, and is
  // retried on failure.
  // The cmd passed in must be **idempotent** (safe to run twice with no accumulating side
  // effect); internally everything overwrites with ">".
  const runConfirmed = async (cmdFn, sentinelPath, tag, { tries = 3, timeoutMs = 60000, pollMs = 250 } = {}) => {
    for (let attempt = 1; attempt <= tries; attempt++) {
      if (attempt > 1) await resetLine();  // the previous try may have left half a line unexecuted
      await sendLine(cmdFn());
      const got = await waitFor(readFile, sentinelPath, tag, timeoutMs, pollMs);
      if (got !== null) return got;
      if (attempt < tries) process.stderr.write(`  [input not confirmed (attempt ${attempt}/${tries}), retrying]\n`);
    }
    return null;
  };

  return { ws, send, evaluate, readFile, assertSafeTab, focusTerminal, sendLine, lineBudget, typeLine, pasteLine, resetLine, runConfirmed, close: () => ws.close() };
}

// Wait for a sentinel file to appear containing tag. Adaptive polling: start at pollMs,
// multiply by 1.5, cap at 1200ms -- a fast command confirms in ~300ms while a long job
// does not hammer readFile.
async function waitFor(readFile, path, tag, timeoutMs, pollMs = 250) {
  const deadline = Date.now() + timeoutMs;
  let wait = pollMs;
  while (Date.now() < deadline) {
    await sleep(Math.min(wait, Math.max(50, deadline - Date.now())));
    wait = Math.min(wait * 1.5, 1200);
    const d = await readFile(path);
    if (d.status === 200 && d.body && d.body.includes(tag)) return d.body;
  }
  return null;
}

// Job tag: human-readable and time-sortable (jMMDD_HHMMSS_pid). The remote job directory
// is jobs/<tag>/.
const newTag = () => {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `j${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}_${process.pid}`;
};

// Preamble for every call: sweep expired job directories and create this call's own.
// It rides on the same command line as the real work, so it costs no extra round trip.
// Expiry is judged by the mtime of the **newest file inside** the directory, not the
// directory's own mtime: a directory's mtime does not change when out.txt is appended to,
// so looking only at the directory would delete a long nohup job that is still writing
// its log after 24h.
const preamble = (jobDir) =>
  `mkdir -p ${CFG.jobRoot}; for d in ${CFG.jobRoot}/*/; do [ -e "$d" ] || continue; ` +
  `[ -z "$(find "$d" -mmin -${CFG.sweepMin} -print -quit 2>/dev/null)" ] && rm -rf "$d"; done 2>/dev/null; ` +
  `mkdir -p ${jobDir}`;

module.exports = { CFG, PICK, TABNAME, sleep, connect, waitFor, newTag, preamble, lockGuard, acquireLock, releaseLock };
