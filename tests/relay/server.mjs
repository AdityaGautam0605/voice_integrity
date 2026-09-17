// TEST ONLY: allow-loopback-peers is deliberately restricted to this local
// harness. Never copy this configuration into the public VPS deployment.
import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createConnection } from 'node:net';

const root = fileURLToPath(new URL('../../', import.meta.url));
const python = resolve(root, 'voice-integrity/.venv/bin/python');
const turnserver = process.env.VIF_TEST_TURNSERVER
  || ['/opt/homebrew/opt/coturn/bin/turnserver', '/usr/bin/turnserver', '/usr/local/bin/turnserver']
    .find((path) => existsSync(path)) || 'turnserver';
const turnCheck = spawnSync(turnserver, ['--version'], { encoding: 'utf8' });
if (turnCheck.error || turnCheck.status !== 0 || !existsSync(python)) {
  console.error('Relay tests require coturn (brew install coturn / apt install coturn), the demo Python venv, Node 22+, and Playwright Chromium. Set VIF_TEST_TURNSERVER if coturn is outside PATH.');
  process.exit(1);
}

// Exercise the built-frontend server used by --tunnel with real local TURN.
if (spawnSync(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), 'build'],
  { cwd: root, stdio: 'inherit' }).status !== 0) process.exit(1);

const directory = mkdtempSync(join(tmpdir(), 'vif-relay-test-'));
const secret = randomBytes(32).toString('hex');
const turnPort = 53470;
const cert = join(directory, 'localhost.pem');
const key = join(directory, 'localhost-key.pem');
const tls = spawnSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
  '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
  '-keyout', key, '-out', cert], { encoding: 'utf8' });
if (tls.error || tls.status !== 0) {
  console.error('Cannot create isolated test HTTPS certificate. Install OpenSSL with req -addext support.', tls.stderr);
  rmSync(directory, { recursive: true, force: true });
  process.exit(1);
}

const conf = join(directory, 'turnserver.conf');
writeFileSync(conf, [
  '# Local integration test only. This file is never deployed.',
  'listening-ip=127.0.0.1', 'relay-ip=127.0.0.1', `listening-port=${turnPort}`,
  'min-port=53480', 'max-port=53500', 'realm=vif-local-test',
  'use-auth-secret', `static-auth-secret=${secret}`, 'fingerprint',
  'no-cli', 'allow-loopback-peers', 'no-multicast-peers', 'no-tls', 'no-dtls',
  'no-rfc5780', 'no-tcp-relay', 'relay-threads=1',
  'total-quota=16', 'user-quota=4', 'simple-log', 'log-file=stdout',
  `userdb=${join(directory, 'turn.sqlite')}`,
  `pidfile=${join(directory, 'coturn.pid')}`,
].join('\n') + '\n', { mode: 0o600 });

const children = [];
let stopping;
function start(label, binary, args, options = {}) {
  const child = spawn(binary, args, { cwd: root, stdio: 'inherit', ...options });
  children.push(child);
  child.once('error', (error) => { console.error(`${label}: ${error.message}`); stop(1); });
  child.once('exit', (code, signal) => {
    if (!stopping) {
      console.error(`${label} stopped unexpectedly (${code ?? signal}). Stop other demos using ports 5173/53470.`);
      stop(1);
    }
  });
  return child;
}
function stop(code = 0) {
  if (stopping) return stopping;
  process.exitCode = code;
  stopping = Promise.all(children.map((child) => new Promise((done) => {
    if (child.exitCode !== null || child.signalCode !== null || !child.pid) return done();
    const timer = setTimeout(() => child.kill('SIGKILL'), 4000);
    child.once('exit', () => { clearTimeout(timer); done(); });
    child.kill('SIGTERM');
  }))).then(() => rmSync(directory, { recursive: true, force: true }));
  return stopping;
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => stop());

function waitForPort(port) {
  const deadline = Date.now() + 10000;
  return new Promise((done, fail) => {
    const attempt = () => {
      if (stopping) return fail(new Error('Relay harness stopped during startup.'));
      const socket = createConnection({ host: '127.0.0.1', port });
      socket.once('connect', () => { socket.destroy(); done(); });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() >= deadline) fail(new Error(`coturn did not listen on ${port}.`));
        else setTimeout(attempt, 100);
      });
    };
    attempt();
  });
}

start('coturn', turnserver, ['-c', conf]);
try {
  await waitForPort(turnPort);
  const env = {
    ...process.env, VIF_ONLINE: '1', VIF_PUBLIC_ORIGIN: 'https://localhost:5173', VIF_TURN_PROVIDER: 'coturn',
    VIF_OPERATOR_CODE: 'relay-test-operator-access-code',
    VIF_TURN_HOST: '127.0.0.1', VIF_TURN_SECRET: secret, VIF_TURN_PORT: String(turnPort),
    VIF_TURN_TLS_PORT: '53479', VIF_BACKEND: 'stub', VIF_DEMO_SCENARIOS: '1', VIF_VAD: 'energy',
    VIF_KEYS_DIR: join(directory, 'keys'), VIF_DATA_DIR: join(directory, 'data'),
    VIF_TLS_CERT: cert, VIF_TLS_KEY: key, VIF_HTTP: '0',
  };
  // Explicitly clear unrelated local credentials for this isolated online server.
  delete env.VIF_API_TOKEN;
  start('demo site', python, ['-c', [
    'import os, uvicorn',
    'from vif.serve.server import create_app',
    'from vif.serve.site import DemoSite',
    'uvicorn.run(DemoSite(create_app(), os.environ["VIF_FRONTEND_DIST"]), host="127.0.0.1", port=5173,',
    '            ssl_certfile=os.environ["VIF_TLS_CERT"], ssl_keyfile=os.environ["VIF_TLS_KEY"], access_log=False)',
  ].join('\n')], {
    cwd: resolve(root, 'voice-integrity'), env: { ...env, PYTHONPATH: 'src', VIF_FRONTEND_DIST: resolve(root, 'dist') },
  });
} catch (error) {
  console.error(error.message);
  await stop(1);
}
