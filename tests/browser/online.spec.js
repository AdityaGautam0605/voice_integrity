import { test, expect } from '@playwright/test';

// These focused UI tests use a local API fixture. The separate relay suite
// exercises real online authentication, coturn, signaling and peer media.
async function onlineApi(page, authenticated = false) {
  const state = { authenticated };
  await page.route('**/api/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), online_mode: true, public_origin: 'https://demo.example.test' } });
  });
  await page.route('**/api/v1/operator', (route) => route.fulfill({ json: { online_mode: true, authenticated: state.authenticated } }));
  await page.route('**/api/v1/operator/login', (route) => {
    state.authenticated = route.request().postDataJSON().code === 'fixture-access';
    return route.fulfill({ status: state.authenticated ? 200 : 401,
      json: state.authenticated ? { authenticated: true } : { detail: 'Invalid access code' } });
  });
  await page.route('**/api/v1/operator/logout', (route) => {
    state.authenticated = false;
    return route.fulfill({ json: { authenticated: false } });
  });
  return state;
}

async function microphone(page) {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      const source = context.createOscillator();
      const output = context.createMediaStreamDestination();
      source.connect(output); source.start();
      window.fixtureMicrophone = output.stream;
      return output.stream;
    };
    const Peer = window.RTCPeerConnection;
    window.RTCPeerConnection = class extends Peer {
      constructor(config) { super(config); window.fixturePeerConfig = config; }
    };
  });
}

test('online operator signs in, receives errors for bad codes, and signs out', async ({ page }) => {
  await onlineApi(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Sign in to create a call' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create call', exact: true })).toHaveCount(0);
  await page.getByLabel('Operator access code').fill('wrong-code');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Invalid access code');
  await page.getByLabel('Operator access code').fill('fixture-access');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Create call', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Force relay', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByLabel('Operator access code')).toHaveValue('');
});

test('caller invitations bypass the operator sign-in screen', async ({ page }) => {
  await onlineApi(page);
  await page.goto('/join/fixture-room#token=fixture-caller');
  await expect(page.getByRole('button', { name: 'Join call', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Operator access code')).toHaveCount(0);
  await expect(page.getByLabel('Force relay', { exact: false })).toHaveCount(0);
});

test('online invitation uses public origin and peer configuration comes from the room', async ({ page }) => {
  await onlineApi(page, true); await microphone(page);
  let creation;
  let membership;
  await page.route('**/api/v1/calls', (route) => {
    creation = route.request().postDataJSON();
    return route.fulfill({ json: { call_id: 'fixture-room', credentials: { operator: 'fixture-operator', caller: 'fixture-caller' },
      online_mode: true, public_origin: 'https://demo.example.test' } });
  });
  const servers = [{ urls: ['turn:127.0.0.1:3478'], username: 'temporary-user', credential: 'temporary-fixture' }];
  await page.route('**/api/v1/calls/fixture-room/ice', (route) => {
    membership = route.request().postDataJSON();
    return route.fulfill({ json: { iceServers: servers, iceTransportPolicy: 'relay', online_mode: true } });
  });
  await page.routeWebSocket('**/api/v1/calls/fixture-room/signal', () => {});
  await page.goto('/');
  await page.getByLabel('Force relay', { exact: false }).check();
  await page.getByRole('button', { name: 'Create call', exact: true }).click();
  await expect(page.getByLabel('Caller invitation URL')).toHaveValue('https://demo.example.test/join/fixture-room#token=fixture-caller');
  await expect(page.getByText('Waiting for peer', { exact: true })).toBeVisible();
  expect(creation).toEqual({ force_relay: true });
  expect(membership).toEqual({ role: 'operator', token: 'fixture-operator' });
  expect(await page.evaluate(() => window.fixturePeerConfig)).toEqual({ iceServers: servers, iceTransportPolicy: 'relay' });
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeDisabled();
  await expect(page.getByLabel('Connection route', { exact: true })).toHaveText('Unknown');
  await page.getByRole('button', { name: 'End call', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeEnabled();
});

test('expired operator access returns to sign-in and releases the microphone', async ({ page }) => {
  const access = await onlineApi(page, true); await microphone(page);
  await page.route('**/api/v1/calls', (route) => {
    access.authenticated = false;
    return route.fulfill({ status: 401, json: { detail: 'Operator sign-in required' } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Create call', exact: true }).click();
  await expect(page.getByLabel('Operator access code')).toBeVisible();
  expect(await page.evaluate(() => window.fixtureMicrophone.getTracks().every((track) => track.readyState === 'ended'))).toBe(true);
});

test('hanging up during ICE retrieval prevents late peer creation', async ({ page }) => {
  await onlineApi(page, true); await microphone(page);
  await page.route('**/api/v1/calls', (route) => route.fulfill({ json: {
    call_id: 'pending-room', credentials: { operator: 'fixture-operator', caller: 'fixture-caller' }, online_mode: true,
  } }));
  let release;
  let requested;
  const arrived = new Promise((resolve) => { requested = resolve; });
  await page.route('**/api/v1/calls/pending-room/ice', async (route) => {
    requested();
    await new Promise((resolve) => { release = resolve; });
    await route.fulfill({ json: { iceServers: [], iceTransportPolicy: 'all', online_mode: true } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Create call', exact: true }).click();
  await arrived;
  await page.getByRole('button', { name: 'End call', exact: true }).click();
  await expect(page.getByText('Ended', { exact: true })).toBeVisible();
  const delivered = page.waitForResponse('**/api/v1/calls/pending-room/ice');
  release(); await delivered;
  expect(await page.evaluate(() => window.fixturePeerConfig)).toBeUndefined();
  expect(await page.evaluate(() => window.fixtureMicrophone.getTracks().every((track) => track.readyState === 'ended'))).toBe(true);
});
