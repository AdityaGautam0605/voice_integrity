import { spawnSync } from 'node:child_process';
import { mkdirSync, copyFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isIP } from 'node:net';
const ip = process.argv[2];
if (!ip || !isIP(ip)) {
  console.error('Usage: npm run demo:cert -- <laptop-LAN-IP>');
  process.exit(1);
}
mkdirSync('.certs/ca', { recursive: true });
const env = { ...process.env, CAROOT: resolve('.certs/ca') };
function run(args) {
  const result = spawnSync('mkcert', args, { env, stdio: 'inherit' });
  if (result.error || result.status !== 0) {
    console.error(result.error?.message || 'Certificate setup failed. Install mkcert and retry.');
    process.exit(1);
  }
}
run(['-cert-file', '.certs/demo.pem', '-key-file', '.certs/demo-key.pem', ip, 'localhost', '127.0.0.1', '::1']);
copyFileSync('.certs/ca/rootCA.pem', '.certs/phone-rootCA.crt');
if (!process.argv.includes('--no-trust')) run(['-install']);
else console.log('Certificate generated; laptop trust installation was skipped.');
console.log(`\nLaptop certificate ready for https://${ip}:5173\nTransfer .certs/phone-rootCA.crt to Android and install it as a CA certificate.\nNever transfer rootCA-key.pem. Then run npm run demo.\n`);
