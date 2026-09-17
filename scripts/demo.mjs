import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { networkInterfaces } from 'node:os';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';

if (process.argv.includes('--tunnel')) {
  if (process.argv.includes('--online') || process.argv.includes('--http')) {
    console.error('--tunnel cannot be combined with --online or --http.');
    process.exit(1);
  }
  const { runTunnel } = await import('./tunnel.mjs');
  await runTunnel();
} else if (process.argv.includes('--online')) {
  if (process.argv.includes('--http')) {
    console.error('--online and --http cannot be combined. Online mode uses public HTTPS.');
    process.exit(1);
  }
  const { runOnline } = await import('./online.mjs');
  await runOnline();
} else {
const root = fileURLToPath(new URL('../', import.meta.url));
const python = resolve(root, 'voice-integrity/.venv/bin/python');
if (!existsSync(python)) {
  console.error('Backend environment missing. Follow README_frontend.md → Setup.');
  process.exit(1);
}
const http = process.argv.includes('--http');
if (!http && (!existsSync(resolve(root, '.certs/demo.pem')) || !existsSync(resolve(root, '.certs/demo-key.pem')))) {
  console.error('HTTPS certificate missing. Run npm run demo:cert -- <laptop-LAN-IP> first.\nFor laptop-only development: npm run demo -- --http');
  process.exit(1);
}
const env = { ...process.env, VIF_BACKEND: process.env.VIF_BACKEND || 'stub',
  VIF_DEMO_SCENARIOS: process.env.VIF_DEMO_SCENARIOS || '1', VIF_VAD: process.env.VIF_VAD || 'energy' };
if (http) env.VIF_HTTP = '1';
const backend = spawn(python, ['-m', 'vif.cli', 'serve', '--host', '127.0.0.1'], {
  cwd: resolve(root, 'voice-integrity'), stdio: 'inherit', env: { ...env, PYTHONPATH: 'src' },
});
const frontend = spawn(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js')], {
  cwd: root, stdio: 'inherit', env,
});
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  backend.kill('SIGTERM'); frontend.kill('SIGTERM');
  process.exitCode = code;
}
for (const child of [backend, frontend]) {
  child.on('error', (error) => { console.error(error.message); stop(1); });
  child.on('exit', (code) => stop(code || 0));
}
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
console.log(`\nVoice Integrity: ${env.VIF_BACKEND} backend · ${env.VIF_VAD} VAD`);
if (http) console.log('Laptop-only development: http://localhost:5173');
else for (const addresses of Object.values(networkInterfaces())) {
  for (const address of addresses || []) {
    if (address.family === 'IPv4' && !address.internal) console.log(`Open on both devices: https://${address.address}:5173`);
  }
}
console.log('Use the LAN IP covered by your certificate. Ctrl+C stops both services.\n');
}
