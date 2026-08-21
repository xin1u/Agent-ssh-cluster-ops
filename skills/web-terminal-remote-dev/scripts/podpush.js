#!/usr/bin/env node
// Write a local file (or stdin) to a path on the remote host, over the web-terminal channel.
//
// Usage:
//   node podpush.js <local file> <remote absolute path>
//   cat foo.py | node podpush.js - /mnt/shared/project/foo.py
//
// How it works: base64 the content -> paste it in chunks (~150K characters each, so most
//   files take one chunk) -> each chunk is written to jobs/<tag>/c_<i> and read back for
//   verification -> concatenate in order and base64 -d into a staging file -> only after
//   the sha256 matches is it mv'd onto the destination -> then the destination itself is
//   re-read and re-hashed. Only ASCII is ever typed, so CJK, quotes and newlines are safe.
//
// Intermediate files: all under jobs/<tag>/, deleted on success and kept for diagnosis on
// failure (a later call's preamble sweeps them). A failed push never leaves a corrupt file
// at the destination.
//
// Measured (paste channel): a flat ~9 chars/ms -- 10KB file ~3s, 100KB ~16s, 1MB ~2.5min.
// For anything larger, if the remote host has egress, having it curl/git the file itself
// is faster.

const fs = require('fs');
const crypto = require('crypto');
const { CFG, connect, newTag, preamble, lockGuard } = require('./podlib.js');

const args = process.argv.slice(2).filter((a) => a !== '--stdin');
if (args.length !== 2) {
  console.error('usage: node podpush.js <local file|-> <remote absolute path>');
  process.exit(1);
}
const [src, dest] = args;
if (!dest.startsWith('/')) { console.error('the remote path must be absolute'); process.exit(1); }

const content = src === '-' ? fs.readFileSync(0) : fs.readFileSync(src);
const sha = crypto.createHash('sha256').update(content).digest('hex');
const b64 = content.toString('base64');

async function main() {
  const pod = await connect();
  try {
    // Reset first: if the terminal is stuck on a continuation prompt, the first step would
    // otherwise burn a full timeout before the retry path resets it.
    await pod.resetLine();
    const tag = newTag();
    const dir = `${CFG.jobRoot}/${tag}`;
    const prog = `${dir}/prog`;
    // Write to a staging file and only mv onto the destination after verification, so the
    // destination is always either the old content or the correct new content, never a
    // corrupt half-written file. (The first version wrote the destination directly, and a
    // checksum failure meant the destination was already ruined.)
    const stage = `${dir}/stage`;

    // The destination path goes through base64 as well: inside a double-quoted shell
    // string, JSON.stringify does not stop bash from expanding $(...) or backticks
    // (injection), and a path containing CJK would be rejected by the input channel's
    // ASCII check. Decoding base64 into a variable solves both. The variable name carries
    // the tag suffix so it cannot collide with a variable already set in the user's shell.
    const destB64 = Buffer.from(dest, 'utf8').toString('base64');
    const destVar = `D_${tag}`;
    const destRef = `"$${destVar}"`;
    const destDecl = `${destVar}=$(printf %s ${destB64} | base64 -d)`;

    // Per-chunk budget, leaving room for the command template. In paste mode a chunk is
    // ~150K characters; on the keystroke fallback it is ~9K.
    const CHUNK = Math.max(1000, pod.lineBudget() - 200);
    const chunks = [];
    for (let i = 0; i < b64.length; i += CHUNK) chunks.push(b64.slice(i, i + CHUNK));
    console.error(`pushing ${content.length} bytes -> ${dest}  (b64 ${b64.length} chars, ${chunks.length} chunk(s))`);

    // Every step goes through runConfirmed: paste it, read the sentinel to confirm, retry
    // when unconfirmed. The input channel can drop content (see podlib.js), so no step may
    // assume it landed on the first try.
    const step = async (label, cmd, opts) => {
      const r = await pod.runConfirmed(() => cmd, prog, `${tag}:${label}:`, opts);
      if (r === null) {
        throw new Error(`step "${label}" was still unconfirmed after several retries.\n` +
          `Diagnose with: node ${__dirname}/podshot.js  (is the terminal stuck?)\n` +
          `               node ${__dirname}/pod.js 'df -h ${CFG.tmpDir}'  (a full disk looks identical)`);
      }
      return r;
    };
    const sentinel = (label) => `echo ${tag}:${label}:$? > ${prog}`;

    await step('prep', `${preamble(dir)}; ${destDecl}; mkdir -p "$(dirname ${destRef})"; ${sentinel('prep')}`, { timeoutMs: 30000 });

    // Write chunk by chunk. Each chunk goes to **its own file** (c_<i>) with ">" rather
    // than appending to one file with ">>": input loss is real, and under append semantics
    // a retry writes duplicate data and makes things worse. One file per chunk is
    // idempotent. Read back the length after writing -- a truncated write still exits 0,
    // so the sentinel alone cannot detect it.
    for (let i = 0; i < chunks.length; i++) {
      let ok = false;
      for (let attempt = 1; attempt <= 3 && !ok; attempt++) {
        await step(`c${i}`, `printf %s ${chunks[i]} > ${dir}/c_${i}; ${sentinel(`c${i}`)}`,
          { timeoutMs: Math.max(60000, chunks[i].length / 4) });
        const got = await pod.readFile(`${dir}/c_${i}`);
        if (got.status === 200 && got.buf.length === chunks[i].length && got.body === chunks[i]) ok = true;
        else process.stderr.write(`  [chunk ${i + 1} mismatch (${got.status === 200 ? got.buf.length : 'HTTP ' + got.status}` +
          ` vs ${chunks[i].length}), rewrite attempt ${attempt}]\n`);
      }
      if (!ok) throw new Error(`chunk ${i + 1}/${chunks.length} was still incomplete after 3 attempts. Check remote disk space.`);
      if (chunks.length > 1) process.stderr.write(`\r  chunk ${i + 1}/${chunks.length}`);
    }
    if (chunks.length > 1) process.stderr.write('\n');

    // Concatenate in numeric order, decode, verify. A shell glob cannot be used: c_10
    // sorts before c_2 lexically.
    // Paste chunks are large, so a normal file is only a few chunks -- the filename list is
    // inlined directly, and only a very large chunk count needs batching.
    const listFile = `${dir}/list`;
    const NAMES_PER_LINE = 800;  // 800 "c_<i>" names is about 6KB, far below the paste budget
    for (let i = 0; i < chunks.length; i += NAMES_PER_LINE) {
      const names = chunks.slice(i, i + NAMES_PER_LINE).map((_, j) => `c_${i + j}`).join('\\n');
      const redir = i === 0 ? '>' : '>>';   // first batch overwrites (keeping retries idempotent), later ones append
      await step(`l${i}`, `printf '${names}\\n' ${redir} ${listFile}; ${sentinel(`l${i}`)}`);
    }

    // Note the cd must be wrapped in a subshell: these commands run in **the terminal's own
    // shell**, so a bare cd would really change the user's working directory -- and since
    // the tab label includes the cwd, that makes the safety gate see "bash" concatenated
    // with the cwd and reject a valid shell (observed). It is also just rude to the user's
    // terminal.
    await step('decode',
      `(cd ${dir} && xargs -a ${listFile} cat | base64 -d > ${stage}) 2>/dev/null; ` +
      `printf '%s %s ' "$(sha256sum ${stage} | cut -d" " -f1)" "$(wc -c < ${stage})" > ${prog}; ` +
      `echo ${tag}:decode:0 >> ${prog}`, { timeoutMs: 120000 });

    const res = await pod.readFile(prog);
    const [gotSha, gotLen] = res.body.trim().split(/\s+/);
    if (gotSha !== sha) {
      throw new Error(`verification failed: sha256 mismatch -- the destination ${dest} was NOT modified.\n` +
        `  local:  ${sha} (${content.length} bytes)\n  remote: ${gotSha} (${gotLen} bytes)\n` +
        `  Intermediates kept at ${dir} for diagnosis (they will be swept automatically). Please retry.`);
    }

    // Verified, so mv onto the destination (an atomic replace within one filesystem;
    // across filesystems mv is copy+rm, which the final re-check covers).
    // destDecl is re-declared in this step rather than relying on the variable prep left in
    // the terminal's shell (it would be gone if a human intervened in between).
    await step('mv', `${destDecl}; mv -f ${stage} ${destRef}; ${sentinel('mv')}`, { timeoutMs: 60000 });
    const mvRes = await pod.readFile(prog);
    if (!/:mv:0\b/.test(mvRes.body)) {
      throw new Error(`content verified but the mv onto ${dest} failed (permissions? destination is a directory?).\n` +
        `The content is still at ${stage} on the remote host and can be moved by hand.`);
    }

    // Finally, independently confirm the destination file itself -- that is the only fact
    // that actually matters, and it is not inferred from an exit code.
    const finalCheck = await pod.readFile(dest);
    if (finalCheck.status !== 200 || crypto.createHash('sha256').update(finalCheck.buf).digest('hex') !== sha) {
      throw new Error(`post-write re-check failed: ${dest} does not match the local content (HTTP ${finalCheck.status}). Please retry.`);
    }

    console.error(`OK  ${dest}  ${gotLen} bytes  sha256 ${gotSha.slice(0, 16)}...  landed and re-verified`);
    // Success: delete the job directory now.
    await pod.sendLine(`rm -rf ${dir}`).catch(() => {});
  } finally {
    pod.close();
  }
}

lockGuard();
main().catch((e) => { console.error('error:', e.message); process.exit(1); });
