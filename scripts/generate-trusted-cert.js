#!/usr/bin/env node
// Manual re-run of the trusted-certificate setup server.js already does
// automatically on first start (see ../certSetup.js) — mainly useful after
// your LAN IP changes (e.g. a different Wi-Fi network), since the cert's
// list of covered hostnames only gets refreshed when this forces it.

const { getOrCreateCertificate, mkcertAvailable, getLanIps } = require('../certSetup');

async function main() {
  if (!mkcertAvailable()) {
    console.error('mkcert is not installed or not on PATH.\n');
    console.error('Install it, then re-run `npm run setup:https`:');
    console.error('  Windows (winget): winget install -e --id FiloSottile.mkcert');
    console.error('  Windows (choco):  choco install mkcert');
    console.error('  macOS (brew):     brew install mkcert');
    console.error('  Linux:            https://github.com/FiloSottile/mkcert#installation');
    process.exit(1);
  }

  console.log(`Issuing a trusted certificate for: localhost, 127.0.0.1, ::1, ai-gos.local, ${getLanIps().join(', ')}`);
  await getOrCreateCertificate({ forceRegenerate: true });
  console.log('\nDone. Restart the backend (`npm run dev`) and reload the page —');
  console.log('the "Not secure" warning should be gone on this PC.');
  const lanIps = getLanIps();
  if (lanIps.length) {
    console.log(`If your phone still warns when visiting https://${lanIps[0]}:5000,`);
    console.log('run `mkcert -install` on the phone too, or just tap through once.');
  }
}

main();
