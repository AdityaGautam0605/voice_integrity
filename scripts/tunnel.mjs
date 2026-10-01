import { spawn, spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';
import { assertBackendPortFree } from './online.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const python = resolve(root, 'voice-integrity/.venv/bin/python');

export function tunnelConfig(input) {
  const domain = (input.VIF_METERED_DOMAIN || '').toLowerCase();
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.metered\.live$/.test(domain) || domain === 'your-app.metered.live') {
    throw new Error('Set VIF_METERED_DOMAIN to your appname.metered.live hostname in .env.tunnel.');
  }
  const key = input.VIF_METERED_API_KEY || '';
  if (key.length < 16 || key.length > 512 || /\s/.test(key) || key.startsWith('replace-')) {
    throw new Error('Set VIF_METERED_API_KEY to a TURN credential API key from Metered (not the app Secret Key).');
  }
  if (input.VIF_OPERATOR_CODE && !/^[\x21-\x7e]{16,128}$/.test(input.VIF_OPERATOR_CODE)) {
    throw new Error('VIF_OPERATOR_CODE must be 16–128 printable non-space characters.');
  }
  return { ...input, VIF_METERED_DOMAIN: domain, VIF_METERED_API_KEY: key,
    VIF_ONLINE: '1', VIF_TURN_PROVIDER: 'metered', VIF_FRONTEND_DIST: resolve(root, 'dist'),
    VIF_BACKEND: input.VIF_BACKEND || 'stub', VIF_DEMO_SCENARIOS: input.VIF_DEMO_SCENARIOS || '1',
    VIF_VAD: input.VIF_VAD || 'energy', VIF_OPERATOR_CODE: input.VIF_OPERATOR_CODE || randomBytes(18).toString('base64url') };
}

export function quickTunnelOrigin(log) {
  return /https:\/\/[a-z0-9-]+\.trycloudflare\.com(?=[\s/|]|$)/.exec(log)?.[0] || null;
}

export async function runTunnel() {
  const file = resolve(root, '.env.tunnel');
  let env;
  let binary;
  try {
    if (!existsSync(file)) {
      writeFileSync(file, readFileSync(resolve(root, 'deploy/tunnel/laptop.env.example')), { flag: 'wx', mode: 0o600 });
      throw new Error('Created .env.tunnel. Add your Metered app domain and TURN credential API key, then run again.');
    }
    if (statSync(file).mode & 0o077) throw new Error('Run chmod 600 .env.tunnel to restrict access to its credential.');
    env = tunnelConfig({ ...parseEnv(readFileSync(file, 'utf8')), ...process.env });
    if (!existsSync(python)) throw new Error('Backend environment missing. Follow README_frontend.md → Setup.');
    binary = env.VIF_CLOUDFLARED || ['/opt/homebrew/bin/cloudflared', '/usr/local/bin/cloudflared']
      .find((path) => existsSync(path)) || 'cloudflared';
    if (spawnSync(binary, ['--version'], { stdio: 'ignore' }).status !== 0) {
      throw new Error('Install cloudflared first (macOS: brew install cloudflared).');
    }
    await assertBackendPortFree();
  } catch (error) {
    console.error(`Tunnel setup: ${error.message}\nSee docs/TUNNEL_DEMO.md.`);
    process.exitCode = 1;
    return;
  }

  // Check the narrow provider key before publishing a URL. The key is only
  // passed through the Python environment, never a CLI argument or browser file.
  const check = spawnSync(python, ['-c', [
    'from vif.serve.online import OnlineConfig',
    'from vif.serve.metered import fetch_servers',
    'from fastapi import HTTPException',
    'import sys',
    'config = OnlineConfig.from_env()',
    'try:',
    '    fetch_servers(config.metered_domain, config.metered_api_key)',
    'except HTTPException as error:',
    '    print(error.detail, file=sys.stderr)',
    '    sys.exit(1)',
  ].join('\n')], { cwd: resolve(root, 'voice-integrity'), stdio: 'inherit',
    env: { ...env, PYTHONPATH: 'src', VIF_PUBLIC_ORIGIN: 'https://pending.trycloudflare.com' }, timeout: 15000 });
  if (check.status !== 0) { console.error('TURN preflight failed; the public tunnel was not started.'); process.exitCode = 1; return; }
  // cloudflared and the build do not need application credentials.
  const publicEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('VIF_')));
  const build = spawnSync(process.execPath, [resolve(root, 'node_modules/vite/bin/vite.js'), 'build'], {
    cwd: root, env: publicEnv, stdio: 'inherit', timeout: 60000,
  });
  if (build.status !== 0) { console.error('Frontend build failed.'); process.exitCode = 1; return; }

  // An explicit empty config avoids interference from an existing named tunnel.
  const directory = mkdtempSync(join(tmpdir(), 'vif-quick-tunnel-'));
  const configFile = join(directory, 'config.yml');
  writeFileSync(configFile, '{}\n', { mode: 0o600 });
  const children = [];
  let stopping;
  let rejectUrl;
  let startupTimer;
  function stop(code = 0) {
    if (stopping) return stopping;
    process.exitCode = code;
    clearTimeout(startupTimer);
    rejectUrl?.(new Error('Tunnel startup stopped.'));
    stopping = Promise.all(children.map((child) => new Promise((done) => {
      if (!child.pid || child.exitCode !== null || child.signalCode !== null) return done();
      const killTimer = setTimeout(() => child.kill('SIGKILL'), 5000);
      child.once('exit', () => { clearTimeout(killTimer); done(); });
      child.kill('SIGTERM');
    }))).then(() => rmSync(directory, { recursive: true, force: true }));
    return stopping;
  }
  function launch(label, executable, args, options) {
    const child = spawn(executable, args, options);
    children.push(child);
    child.once('error', () => { console.error(`${label} could not start.`); void stop(1); });
    child.once('exit', () => {
      if (!stopping) {
        console.error(`${label} stopped. Run ./scripts/demo.sh --tunnel again to get a new URL.`);
        void stop(1);
      }
    });
    return child;
  }
  process.once('SIGINT', () => { void stop(); });
  process.once('SIGTERM', () => { void stop(); });

  try {
    const urlReady = new Promise((resolveUrl, reject) => {
      rejectUrl = reject;
      startupTimer = setTimeout(() => reject(new Error('Cloudflare did not provide a URL within 60 seconds. Check internet access.')), 60000);
      const tunnel = launch('Cloudflare tunnel', binary, ['tunnel', '--config', configFile,
        '--no-autoupdate', '--url', 'http://127.0.0.1:8000'], {
        cwd: root, env: publicEnv, stdio: ['ignore', 'pipe', 'pipe'],
      });
      let log = '';
      function output(chunk) {
        process.stderr.write(chunk);
        log = (log + chunk.toString()).slice(-16384);
        const origin = quickTunnelOrigin(log);
        if (origin) { clearTimeout(startupTimer); resolveUrl(origin); }
      }
      tunnel.stdout.on('data', output);
      tunnel.stderr.on('data', output);
    });
    env.VIF_PUBLIC_ORIGIN = await urlReady;
    if (stopping) return;
    launch('Backend', python, ['-m', 'vif.cli', 'serve', '--host', '127.0.0.1', '--port', '8000'], {
      cwd: resolve(root, 'voice-integrity'), env: { ...env, PYTHONPATH: 'src' }, stdio: 'inherit',
    });
    const deadline = Date.now() + 60000;
    let ready = false;
    while (!stopping && Date.now() < deadline) {
      try {
        const response = await fetch('http://127.0.0.1:8000/api/health', { signal: AbortSignal.timeout(1000) });
        const health = response.ok && await response.json();
        if (health.online_mode && health.public_origin === env.VIF_PUBLIC_ORIGIN) { ready = true; break; }
      } catch { /* Wait for backend bootstrap. */ }
      await delay(500);
    }
    if (!ready) throw new Error('The backend did not start. Check the messages above.');
    console.log(`\nDemo URL: ${env.VIF_PUBLIC_ORIGIN}\nOperator access code: ${env.VIF_OPERATOR_CODE}`);
    console.log('Open the URL on your laptop, sign in, and create a caller invitation.');
    console.log('Keep the laptop awake and this terminal open. A restart changes the URL. Ctrl+C stops the demo.');
    console.log('A newly created Metered credential may need two minutes before relay calls work.');
  } catch (error) {
    if (!stopping) { console.error(error.message); await stop(1); }
  }
}
