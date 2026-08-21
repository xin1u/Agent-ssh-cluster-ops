#!/usr/bin/env node
// List the web terminal's tabs, and move the selection to a **plain shell** tab.
//
// Usage:
//   node podtab.js              list every tab (with index, marking the selected one)
//   node podtab.js <index>      switch to that tab
//   node podtab.js --auto       switch to the first bash/sh/zsh tab
//
// Why this exists: pod.js's safety gate refuses to type when the selected tab is not a
// plain shell (a tab may hold a job that has been running for days). The gate is right,
// but "go click it manually" is a step that can be automated -- provided **only shells may
// be switched to**.
//
// Safety: this script dispatches mouse clicks at tab list rows only. It sends no keyboard
// events, so no program's stdin ever receives anything. And the target tab's label must
// match CFG.safeShells or it refuses outright -- so it can never click onto a tmux,
// python or TUI tab. The switch itself is fully reversible.

const { CFG, connect, sleep } = require('./podlib.js');

// Read every tab's index, label and selected state in one round trip.
// The selection marker sits on the ancestor .monaco-list-row[aria-selected="true"] while
// the entry's own class is "is-active" -- both are accepted, so there is a fallback if a
// VS Code upgrade changes one. See TABNAME in podlib.js.
// term = the terminal **instance number** (from the textarea aria-label "Terminal N"),
// which is the number POD_TERM takes: VS Code assigns it and it does not drift when other
// tabs close. Tab list indices do drift and are only used for clicking in this script.
// Wrapper index is aligned with tab index (both follow creation order); a terminal that has
// never been viewed has no xterm instantiated yet (no textarea inside its wrapper), so term
// is null -- switching to it once with this script instantiates it.
const LIST = `(() => {
  const rows = Array.from(document.querySelectorAll('.terminal-tabs-entry'));
  const wraps = Array.from(document.querySelectorAll('.terminal-wrapper'))
    .filter(w => !w.closest('.terminal-sticky-scroll'))
    .map(w => {
      const ta = w.querySelector('.xterm-helper-textarea');
      const m = ta ? (ta.getAttribute('aria-label') || '').match(/^Terminal (\\d+)/) : null;
      return m ? Number(m[1]) : null;
    });
  return JSON.stringify(rows.map((r, i) => {
    const n = r.querySelector('.label-name');
    return {
      i,
      name: ((n ? n.textContent : r.innerText) || '').trim().split('\\n')[0],
      cwd: (r.querySelector('.label-description') || {textContent: ''}).textContent.trim(),
      active: !!r.closest('[aria-selected="true"]') || /\\bis-active\\b/.test(r.className),
      term: wraps[i] ?? null,
    };
  }));
})`;

async function main() {
  const want = process.argv[2];
  const pod = await connect();
  try {
    const tabs = JSON.parse(await pod.evaluate(`${LIST}()`));
    if (!tabs.length) {
      console.log('there is a single terminal (no tab list) -- nothing to switch, just use pod.js.');
      return;
    }

    const show = () => {
      for (const t of tabs) {
        const safe = CFG.safeShells.test(t.name) ? '' : '  [not a shell, cannot switch to it]';
        const term = t.term !== null ? `  POD_TERM=${t.term}` : '  (not instantiated; switch to it once)';
        console.log(`  ${t.active ? '*' : ' '} [${t.i}] ${t.name}${t.cwd ? '  (' + t.cwd + ')' : ''}${term}${safe}`);
      }
    };

    if (want === undefined) {
      console.log('terminal tabs (* = selected):');
      show();
      const safe = tabs.filter((t) => CFG.safeShells.test(t.name));
      console.log(`\nshell tabs available: ${safe.length ? safe.map((t) => t.i).join(', ') : '(none -- click + in the IDE to open one)'}`);
      return;
    }

    let target;
    if (want === '--auto') {
      target = tabs.find((t) => t.active && CFG.safeShells.test(t.name)) ||
               tabs.find((t) => CFG.safeShells.test(t.name));
      if (!target) {
        console.error('there is no bash/sh/zsh tab. Click + in the IDE terminal panel to open one.');
        console.error('current tabs:'); show();
        process.exit(1);
      }
      if (target.active) { console.log(`already on shell tab "${target.name}", nothing to switch.`); return; }
    } else {
      const idx = Number(want);
      target = tabs.find((t) => t.i === idx);
      if (!target) {
        console.error(`there is no tab with index ${want}. Current tabs:`); show();
        process.exit(1);
      }
      // The allowlist is the whole point of this script: after switching to, say, a tmux
      // tab, every later pod.js call would type into that program's stdin -- and the gate
      // would have been fooled by the premise "the selected tab is a shell".
      if (!CFG.safeShells.test(target.name)) {
        console.error(`refusing to switch to "${target.name}" -- it is not a plain shell.\n` +
          `Once selected, pod.js's safety gate is effectively disabled and commands would be typed into that program's stdin.`);
        process.exit(1);
      }
    }

    // Click the tab row. Mouse events only, no keyboard events.
    const pt = JSON.parse(await pod.evaluate(`(() => {
      const r = document.querySelectorAll('.terminal-tabs-entry')[${target.i}];
      if (!r) return JSON.stringify({ok: false});
      r.scrollIntoView({block: 'nearest'});
      const b = r.getBoundingClientRect();
      return JSON.stringify({ok: true, x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2)});
    })()`));
    if (!pt.ok) throw new Error(`tab ${target.i} is no longer on the page (did the list just change? re-run)`);
    for (const type of ['mousePressed', 'mouseReleased']) {
      await pod.send('Input.dispatchMouseEvent', { type, x: pt.x, y: pt.y, button: 'left', clickCount: 1 });
    }
    await sleep(500);

    // Read back to confirm: a click does not always take effect (list scrolling, render
    // timing), and an unconfirmed switch is the same as no switch.
    const after = JSON.parse(await pod.evaluate(`${LIST}()`));
    const now = after.find((t) => t.active);
    if (!now || now.i !== target.i) {
      console.error(`the switch did not take effect -- "${now ? now.name : '(unknown)'}" is still selected.`);
      process.exit(1);
    }
    console.log(`switched to [${now.i}] ${now.name}${now.cwd ? '  (' + now.cwd + ')' : ''}`);
  } finally {
    pod.close();
  }
}

main().catch((e) => { console.error('error:', e.message); process.exit(1); });
