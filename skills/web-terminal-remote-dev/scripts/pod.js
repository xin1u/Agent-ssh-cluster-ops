#!/usr/bin/env node
// Run a shell command on the remote host and bring back its full text output, over the
// web-terminal channel.
//
// Usage:
//   node pod.js '<shell command>' [timeout seconds]     default timeout 60s
//
// Mechanism: base64 the command -> dispatch a synthetic paste event into the web terminal
//   -> redirect output into this call's own job directory jobs/<tag>/out.txt -> fetch it
//   back through the IDE's own /vscode-remote-resource endpoint. The terminal is canvas
//   rendered, so its text cannot be read from the DOM; the file round trip is mandatory.
//
// Intermediate files: everything lives under <tmpDir>/jobs/<tag>/ (out.txt + done.txt).
//   Success deletes the whole directory at once; a failure or timeout leaves it in place
//   for diagnosis and the next call's preamble sweeps it after POD_SWEEP_MIN (24h by
//   default). The steady state leaves no remote litter.
//
// Important limits:
//   - each call runs in its own bash, so **cd / export do not persist across calls**.
//     Chain the commands into a single call with && or ; when state must be kept.
//   - the terminal is an exclusive resource, so **do not call this in parallel** against
//     the same terminal. The script takes a lock and fails fast rather than interleaving.
//   - do not run interactive or full-screen TUI programs (vim/top/nvitop) through this --
//     their output cannot be read back.

const { CFG, connect, waitFor, newTag, preamble, lockGuard } = require('./podlib.js');

const CMD = process.argv[2];
if (!CMD) {
  console.error("usage: node pod.js '<shell command>' [timeout seconds]");
  process.exit(1);
}
const TIMEOUT_MS = Number(process.argv[3] || 60) * 1000;

async function main() {
  const pod = await connect();
  try {
    // Reset first: the terminal may be sitting on a continuation prompt, or a previous
    // timed-out call's command may still hold the foreground.
    // (Cost: that foreground command gets Ctrl+C'd -- long jobs should be detached with
    // nohup and & anyway.)
    await pod.resetLine();
    // One dedicated job directory per call. Uniqueness matters: with a fixed filename, a
    // command that failed to run silently reads the previous call's stale output and looks
    // like a success. The timeout case is worse -- the previous command is still writing
    // to that file in the background. The tag carries a timestamp, so ls shows at a glance
    // which call left what.
    const tag = newTag();
    const dir = `${CFG.jobRoot}/${tag}`;
    const out = `${dir}/out.txt`;
    const done = `${dir}/done.txt`;

    const b64 = Buffer.from(CMD, 'utf8').toString('base64');
    // The command runs in a separate bash behind a pipe rather than in the terminal's own
    // shell: otherwise an exit or set -e in the command kills the terminal itself. (This
    // was hit for real -- once the tab closed, the panel auto-switched to a neighbouring
    // tab and subsequent keystrokes went into a running job's stdin.)
    // preamble sweeps expired job directories on the way past, costing no extra round trip.
    const line = `${preamble(dir)}; printf %s ${b64} | base64 -d | bash > ${out} 2>&1; echo ${tag}:$? > ${done}`;

    if (line.length > pod.lineBudget()) {
      throw new Error(`command too long (${line.length} characters including the template > ${pod.lineBudget()}).\n` +
        `Instead: push a script with podpush.js first, then run pod.js 'bash /tmp/that-script'.`);
    }

    await pod.sendLine(line);

    const marker = await waitFor(pod.readFile, done, tag, TIMEOUT_MS);
    const o = await pod.readFile(out);

    if (marker === null) {
      // Distinguish the two failures: still running vs input never landed.
      if (o.status === 200) {
        process.stdout.write(o.body);
        console.error(`\n[not finished -- timed out after ${TIMEOUT_MS / 1000}s; the output above is what exists so far]\n` +
          `The command is still running remotely. Output file: ${out}\n` +
          `Poll it later with: node ${__dirname}/podpull.js ${out}`);
      } else {
        console.error(`the command does not appear to have run -- the output file does not exist (HTTP ${o.status}).\n` +
          `Usual causes: input was dropped / the terminal is stuck on a continuation prompt / the page just reloaded.\n` +
          `Re-running usually fixes it; if not, run node ${__dirname}/podshot.js to see the terminal's actual state.`);
      }
      process.exit(1);
    }

    if (o.status !== 200) {
      console.error(`the command finished but its output cannot be read (HTTP ${o.status}) -- was the job directory swept?`);
      process.exit(1);
    }
    process.stdout.write(o.body);

    const code = (marker.match(/:(\d+)/) || [])[1];
    // Success path: delete the job directory immediately so nothing is left remotely
    // (best effort -- a failure here does not affect the result, since a later call's
    // preamble sweeps leftovers).
    await pod.sendLine(`rm -rf ${dir}`).catch(() => {});
    if (code !== '0') { console.error(`[exit code ${code}]`); process.exitCode = Number(code) || 1; }
  } finally {
    pod.close();
  }
}

lockGuard();
main().catch((e) => { console.error('error:', e.message); process.exit(1); });
