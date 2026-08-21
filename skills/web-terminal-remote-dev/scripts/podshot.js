#!/usr/bin/env node
// Screenshot the remote web terminal -- the only way to see what state it is actually in.
//
// Usage:
//   node podshot.js [output png path]      default /tmp/pod-shot.png
//
// When to use it: pod.js reports "the command does not appear to have run", podpush.js
// reports an unconfirmed chunk, or any time the terminal seems stuck. A screenshot
// immediately separates the cases:
//   - cursor at a clean $ prompt      -> the input never landed (re-running usually works)
//   - line begins with ">"            -> bash is on a continuation prompt; reset it (Ctrl+C)
//   - a screen full of scrolling logs -> the previous command is still running, wait
//   - tab name is not bash/sh/zsh     -> you nearly typed into someone else's program, stop
//
// Note: xterm renders to a canvas, so its text cannot be read -- only looked at. That is
// exactly why normal output has to take the "redirect to a file + podpull" route instead of
// reading the DOM.

const fs = require('fs');
const { connect, TABNAME, CFG } = require('./podlib.js');

const outPath = process.argv[2] || '/tmp/pod-shot.png';

async function main() {
  const pod = await connect();
  try {
    // Deliberately does not call focusTerminal: a diagnostic must not change page state
    // (and must not be blocked by that function's safety check either).
    const info = await pod.evaluate(`(() => {
      const tabs = Array.from(document.querySelectorAll('.terminal-tabs-entry'))
        .map(r => (r.innerText || '').trim().split('\\n')[0]);
      const ae = document.activeElement;
      return JSON.stringify({
        url: location.href.slice(0, 120),
        tabs,
        selected: ${TABNAME}(),
        activeElement: ae ? (ae.className || ae.tagName) : '(none)',
        panelVisible: !!document.querySelector('.terminal-wrapper.active'),
      });
    })()`);
    const i = JSON.parse(info);
    const label = { __NO_TABS__: '(single terminal, no tab list)', __UNKNOWN__: 'cannot determine -- the selector may have broken' }[i.selected] || i.selected;
    console.error(`page: ${i.url}`);
    console.error(`terminal tabs: ${i.tabs.length ? i.tabs.join(' | ') : '(none)'}`);
    console.error(`selected: ${label}${CFG.safeShells.test(i.selected) ? '  [plain shell, safe to type into]' : ''}`);
    console.error(`focused element: ${i.activeElement}`);
    console.error(`terminal panel visible: ${i.panelVisible ? 'yes' : 'no -- press Ctrl+` to open the terminal panel'}`);

    const r = await pod.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(outPath, Buffer.from(r.data, 'base64'));
    console.error(`\nOK  screenshot -> ${outPath}  (${Math.round(fs.statSync(outPath).size / 1024)} KB)`);
    console.error('Open that png to see the terminal exactly as it is.');
  } finally {
    pod.close();
  }
}

main().catch((e) => { console.error('error:', e.message); process.exit(1); });
