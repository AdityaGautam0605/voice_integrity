import { test, expect } from '@playwright/test';

async function prepare(page) {
  await page.addInitScript(() => {
    const NativePeer = window.RTCPeerConnection;
    window.testPeers = [];
    window.RTCPeerConnection = class extends NativePeer {
      constructor(config) { super(config); window.testPeers.push(this); }
    };
    // This signal crosses real Opus/WebRTC/coturn; only microphone hardware is replaced.
    navigator.mediaDevices.getUserMedia = async () => {
      const context = new AudioContext();
      await context.resume();
      const oscillator = context.createOscillator();
      const gain = window.testMicGain = context.createGain();
      oscillator.frequency.value = 440;
      gain.gain.value = 0;
      const destination = context.createMediaStreamDestination();
      oscillator.connect(gain).connect(destination);
      oscillator.start();
      return destination.stream;
    };
  });
}

async function create(operator, caller) {
  await operator.goto('/');
  await operator.getByLabel('Operator access code').fill('relay-test-operator-access-code');
  await operator.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(operator.getByText('Backend ready', { exact: true })).toBeVisible();
  await operator.getByLabel('Detector mode').selectOption('AMBER');
  await operator.getByLabel('Force relay', { exact: true }).check();
  await operator.getByRole('button', { name: 'Create call', exact: true }).click();
  const invite = operator.getByLabel('Caller invitation URL');
  await expect(invite).toBeVisible();
  await caller.goto(await invite.inputValue());
  await caller.getByRole('button', { name: 'Join call', exact: true }).click();
}

async function selectedPair(page) {
  return page.evaluate(async () => {
    const peer = window.testPeers.at(-1);
    if (!peer || peer.connectionState !== 'connected') return null;
    const report = await peer.getStats();
    const values = [...report.values()];
    const transport = values.find((item) => item.type === 'transport' && item.selectedCandidatePairId);
    const pair = transport ? report.get(transport.selectedCandidatePairId)
      : values.find((item) => item.type === 'candidate-pair' && item.nominated && item.state === 'succeeded');
    if (!pair) return null;
    return {
      local: report.get(pair.localCandidateId)?.candidateType,
      remote: report.get(pair.remoteCandidateId)?.candidateType,
      packets: values.filter((item) => item.type === 'inbound-rtp' && item.kind === 'audio')
        .reduce((count, item) => count + item.packetsReceived, 0),
    };
  });
}

test('real TURN relay carries both audio directions through analysis and a signed audit verdict', async ({ browser }) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage({ viewport: { width: 393, height: 852 } });
  try {
    await prepare(operator); await prepare(caller);
    await create(operator, caller);
    for (const page of [operator, caller]) {
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      await expect.poll(async () => {
        const pair = await selectedPair(page);
        return pair && [pair.local, pair.remote];
      }).toEqual(['relay', 'relay']);
      await expect(page.getByText('Relayed', { exact: true })).toBeVisible();
    }
    // Give the energy VAD its quiet calibration interval, then prove caller-only analysis.
    await operator.evaluate(() => { window.testMicGain.gain.value = .2; });
    await expect.poll(() => caller.getByRole('meter').getAttribute('value')).not.toBe('0');
    await operator.waitForTimeout(1800);
    await expect(operator.getByText('Collecting speech', { exact: true })).toBeVisible();
    await caller.evaluate(() => { window.testMicGain.gain.value = .2; });
    await expect(operator.getByRole('heading', { name: 'CHALLENGE', exact: true })).toBeVisible();
    for (const page of [operator, caller]) {
      expect((await selectedPair(page)).packets).toBeGreaterThan(0);
    }
    await caller.getByRole('button', { name: 'End call', exact: true }).click();
    await expect(operator.getByText('Signature verified', { exact: true })).toBeVisible();
    const audit = await (await operator.request.get('/api/v1/audit')).json();
    expect(audit.chain_valid).toBe(true);
    expect(audit.count).toBeGreaterThan(0);
    await operator.getByRole('button', { name: 'Test an altered verdict' }).click();
    await expect(operator.getByText(/Altered verdict rejected/)).toBeVisible();
  } finally {
    await operator.close(); await caller.close();
  }
});

test('relay audio and peer hang-up survive loss of signaling and analysis', async ({ browser }) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  try {
    const disconnects = [];
    for (const page of [operator, caller]) {
      await prepare(page);
      await page.routeWebSocket('**/api/**', (route) => {
        const server = route.connectToServer();
        disconnects.push(() => { server.close(); route.close(); });
      });
    }
    await create(operator, caller);
    await expect(operator.getByText('Collecting speech', { exact: true })).toBeVisible();
    for (const page of [operator, caller]) {
      await expect.poll(async () => (await selectedPair(page))?.local).toBe('relay');
    }
    disconnects.forEach((disconnect) => disconnect());
    await expect(operator.getByText('Unavailable', { exact: true })).toBeVisible();
    await expect(operator.getByText('NO CURRENT RESULT', { exact: true })).toBeVisible();
    for (const page of [operator, caller]) {
      await page.evaluate(() => { window.testMicGain.gain.value = .2; });
    }
    for (const page of [operator, caller]) {
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      await expect.poll(() => page.getByRole('meter').getAttribute('value')).not.toBe('0');
    }
    await caller.getByRole('button', { name: 'End call', exact: true }).click();
    await expect(operator.getByText('Ended', { exact: true })).toBeVisible();
  } finally {
    await operator.close(); await caller.close();
  }
});

test('invalid TURN credentials cannot connect a forced-relay call', async ({ browser }) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  try {
    for (const page of [operator, caller]) {
      await prepare(page);
      await page.route('**/api/v1/calls/*/ice', async (route) => {
        const response = await route.fetch();
        const config = await response.json();
        config.iceServers = config.iceServers.map((server) => server.credential
          ? { ...server, credential: 'deliberately-invalid-credential' } : server);
        await route.fulfill({ response, json: config });
      });
    }
    await create(operator, caller);
    await expect(operator.getByRole('alert')).toContainText(/connect|relay|TURN/i, { timeout: 65000 });
    for (const page of [operator, caller]) {
      expect(await selectedPair(page)).toBe(null);
      await expect(page.getByText('Connected', { exact: true })).toHaveCount(0);
    }
  } finally {
    await operator.close(); await caller.close();
  }
});
