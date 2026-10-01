import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';

const root = fileURLToPath(new URL('../', import.meta.url));
const domainPattern = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const expandPath = (path) => path.startsWith('~/') ? resolve(homedir(), path.slice(2)) : resolve(root, path);

export function onlineConfig(input) {
  const env = { ...input };
  let origin;
  try { origin = new URL(env.VIF_PUBLIC_ORIGIN); } catch { throw new Error('Set VIF_PUBLIC_ORIGIN to your public HTTPS origin in .env.online.'); }
  if (origin.protocol !== 'https:' || origin.username || origin.password || origin.search || origin.hash || origin.pathname !== '/' || origin.port || !domainPattern.test(origin.hostname)) {
    throw new Error('VIF_PUBLIC_ORIGIN must be an HTTPS domain without a path, credentials or custom port.');
  }
  if (origin.hostname.endsWith('.example.com')) throw new Error('Replace the example VIF_PUBLIC_ORIGIN with your deployed public domain.');
  if (!domainPattern.test(env.VIF_TURN_HOST || '') || env.VIF_TURN_HOST.endsWith('.example.com')) throw new Error('Set VIF_TURN_HOST to the TURN DNS name.');
  if (!/^[a-zA-Z0-9_-]{32,}$/.test(env.VIF_TURN_SECRET || '') || env.VIF_TURN_SECRET.startsWith('replace-')) throw new Error('VIF_TURN_SECRET must contain at least 32 random letters, digits, hyphens or underscores, matching the VPS TURN_SECRET.');
  if (!/^(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(env.VIF_SSH_HOST || '')) throw new Error('Set VIF_SSH_HOST to your dedicated SSH alias or user@host.');
  for (const [name, fallback] of [['VIF_TURN_PORT', '3478'], ['VIF_TURN_TLS_PORT', '5349'], ['VIF_SSH_PORT', '22']]) {
    const value = env[name] || fallback;
    if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535) throw new Error(`${name} must be a port from 1 to 65535.`);
    if (name !== 'VIF_SSH_PORT' || input.VIF_SSH_PORT) env[name] = value;
  }
  if (env.VIF_TURN_PORT === env.VIF_TURN_TLS_PORT) throw new Error('TURN and TURN TLS ports must differ.');
  if (env.VIF_OPERATOR_CODE && !/^[\x21-\x7e]{16,128}$/.test(env.VIF_OPERATOR_CODE)) throw new Error('An optional VIF_OPERATOR_CODE must be 16–128 printable non-space characters.');
  env.VIF_PUBLIC_ORIGIN = origin.origin;
  env.VIF_ONLINE = '1';
  env.VIF_BACKEND ||= 'stub';
  env.VIF_DEMO_SCENARIOS ||= '1';
  env.VIF_VAD ||= 'energy';
  env.VIF_OPERATOR_CODE ||= randomBytes(12).toString('base64url');
  if (env.VIF_SSH_KEY) {
    env.VIF_SSH_KEY = expandPath(env.VIF_SSH_KEY);
    if (!existsSync(env.VIF_SSH_KEY)) throw new Error('VIF_SSH_KEY does not exist.');
  }
  if (env.VIF_SSH_KNOWN_HOSTS) {
    env.VIF_SSH_KNOWN_HOSTS = expandPath(env.VIF_SSH_KNOWN_HOSTS);
    if (!existsSync(env.VIF_SSH_KNOWN_HOSTS)) throw new Error('VIF_SSH_KNOWN_HOSTS does not exist. Verify and record the VPS host key first.');
  }
  return env;
}

export function sshArguments(env) {
  const args = ['-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
    '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1', '-o', 'ControlMaster=no',
    '-o', 'ControlPath=none', '-o', 'LogLevel=ERROR', '-o', 'RequestTTY=no',
    '-R', '127.0.0.1:18000:127.0.0.1:8000'];
  // An explicit port wins; otherwise allow an SSH alias to supply its configured port.
  if (env.VIF_SSH_PORT) args.push('-p', env.VIF_SSH_PORT);
  if (env.VIF_SSH_KEY) args.push('-o', 'IdentitiesOnly=yes', '-i', env.VIF_SSH_KEY);
  if (env.VIF_SSH_KNOWN_HOSTS) args.push('-o', `UserKnownHostsFile=${env.VIF_SSH_KNOWN_HOSTS}`);
  return [...args, env.VIF_SSH_HOST];
}

export async function assertBackendPortFree() {
  await new Promise((resolvePromise, reject) => {
    const server = createServer();
    server.once('error', () => reject(new Error('Laptop port 8000 is already in use. Stop the existing demo before starting online mode.')));
    server.listen(8000, '127.0.0.1', () => server.close(resolvePromise));
  });
}

export async function runOnline() {
  let env;
  try {
    const file = resolve(root, '.env.online');
    const fileEnv = existsSync(file) ? parseEnv(readFileSync(file, 'utf8')) : {};
    env = onlineConfig({ ...fileEnv, ...process.env });
    if (!existsSync(resolve(root, 'voice-integrity/.venv/bin/python'))) throw new Error('Backend environment missing. Follow README_frontend.md → Setup.');
    if (spawnSync('ssh', ['-V'], { stdio: 'ignore' }).status !== 0) throw new Error('OpenSSH client is required.');
    await assertBackendPortFree();
  } catch (error) {
    console.error(`Online setup: ${error.message}\nSee docs/ONLINE_DEMO.md.`);
    process.exitCode = 1;
    return;
  }
  let stopping = false;
  let tunnel;
  let retry;
  let retrySeconds = 1;
  let backendReady = false;
  const backend = spawn(resolve(root, 'voice-integrity/.venv/bin/python'), ['-m', 'vif.cli', 'serve', '--host', '127.0.0.1', '--port', '8000'], {
    cwd: resolve(root, 'voice-integrity'), stdio: 'inherit', env: { ...env, PYTHONPATH: 'src' },
  });
  function stop(code = 0) {
    if (stopping) return;
    stopping = true;
    clearTimeout(retry);
    backend.kill('SIGTERM');
    tunnel?.kill('SIGTERM');
    process.exitCode = code;
  }
  backend.once('error', (error) => { console.error(`Backend failed: ${error.message}`); stop(1); });
  backend.once('exit', (code) => {
    if (!stopping) { console.error('Backend stopped; closing the online tunnel.'); stop(code || 1); }
  });
  process.once('SIGINT', () => stop());
  process.once('SIGTERM', () => stop());
  const startupDeadline = Date.now() + 60000;
  while (Date.now() < startupDeadline && !stopping) {
    try {
      const response = await fetch('http://127.0.0.1:8000/health', { signal: AbortSignal.timeout(1000) });
      if (response.ok) { backendReady = true; break; }
    } catch { /* Backend is still starting. */ }
    await delay(500);
  }
  if (!backendReady) {
    if (!stopping) console.error('Backend did not become healthy within 60 seconds.');
    stop(1);
    return;
  }
  console.log(`\nOnline demo: ${env.VIF_PUBLIC_ORIGIN}`);
  console.log(`Operator access code: ${env.VIF_OPERATOR_CODE}`);
  console.log('Keep this terminal open and the laptop awake. The public site must be deployed on the VPS.');
  console.log('Opening the SSH tunnel; backend logs and connection failures appear below. Ctrl+C stops the backend and tunnel.\n');
  function connectTunnel() {
    if (stopping) return;
    const started = Date.now();
    tunnel = spawn('ssh', sshArguments(env), { stdio: ['ignore', 'ignore', 'inherit'] });
    tunnel.once('error', (error) => { console.error(`SSH could not start: ${error.message}`); stop(1); });
    tunnel.once('exit', () => {
      if (stopping) return;
      if (Date.now() - started > 60000) retrySeconds = 1;
      console.error(`SSH tunnel disconnected. Retrying in ${retrySeconds}s; check the SSH host key, access and VPS port 18000.`);
      retry = setTimeout(connectTunnel, retrySeconds * 1000);
      retrySeconds = Math.min(retrySeconds * 2, 30);
    });
  }
  connectTunnel();
}
