#!/usr/bin/env node
// Read a file from the remote host back to the local machine, over the web IDE's file
// endpoint.
//
// Usage:
//   node podpull.js <remote absolute path>              # to stdout
//   node podpull.js <remote absolute path> <local path> # to a file
//
// This uses the IDE's own /vscode-remote-resource endpoint, so it does not touch the
// terminal and types nothing -- which makes it fast: measured 1MB in ~1s and 8MB in ~0.1s
// once cached. Text and binary take the same path (always transferred as an arrayBuffer,
// byte faithful), so no extra flag is needed.
//
// Notes:
//   - files only: **passing a directory makes the server hang for 180s and then 504**.
//     List a directory with pod.js 'ls' instead.
//   - not limited to the workspace; any absolute path is readable (/tmp, /root and
//     /etc/hostname were all verified).
//   - Range requests are not supported (a Range header is ignored and the whole file comes
//     back), so there is no resume.
//   - **it takes no terminal lock**, so a long job's log can safely be polled repeatedly
//     while that job runs.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { connect } = require('./podlib.js');

const argv = process.argv.slice(2);
// --bin is accepted for backwards compatibility (reads are always byte faithful now, so
// the flag has no effect).
const rest = argv.filter((a) => a !== '--bin');
if (rest.length < 1 || rest.length > 2) {
  console.error('usage: node podpull.js <remote absolute path> [local path]');
  process.exit(1);
}
const [remote, local] = rest;
if (!remote.startsWith('/')) { console.error('the remote path must be absolute'); process.exit(1); }

async function main() {
  const pod = await connect();
  try {
    const r = await pod.readFile(remote);
    if (r.status === 404) throw new Error(`remote file does not exist: ${remote}`);
    if (r.status === 504) throw new Error(`timed out with 504 -- ${remote} may be a directory (this endpoint reads files only)`);
    if (r.status !== 200) throw new Error(`read failed with HTTP ${r.status}${r.err ? ': ' + r.err : ''}`);

    if (local) {
      fs.mkdirSync(path.dirname(path.resolve(local)), { recursive: true });
      fs.writeFileSync(local, r.buf);
      console.error(`OK  ${remote} -> ${local}  ${r.buf.length} bytes  sha256 ${crypto.createHash('sha256').update(r.buf).digest('hex').slice(0, 16)}...`);
    } else {
      process.stdout.write(r.buf);
    }
  } finally {
    pod.close();
  }
}

main().catch((e) => { console.error('error:', e.message); process.exit(1); });
