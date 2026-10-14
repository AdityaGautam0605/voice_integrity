import { test, expect } from "@playwright/test";

function signed(id, risk = "GREEN") {
  return {
    algorithm: "Ed25519",
    key_id: "test-key",
    signature: "fixture-signature",
    payload: {
      session_id: id,
      risk,
      spoof_probability: risk === "RED" ? 0.91 : risk === "AMBER" ? 0.56 : 0.12,
      action:
        risk === "RED"
          ? "GATE_ACTION"
          : risk === "AMBER"
            ? "CHALLENGE"
            : "PROCEED",
      speaker_status: "NOT_ENROLLED",
      liveness_score: null,
      speech_seconds: 18.4,
      model_version: "stub-demo",
      timestamp: 1791280800000,
    },
  };
}

async function fixture(page, options = {}) {
  const records = options.empty
    ? []
    : Array.from(
        { length: options.count || 3 },
        (_, i) => ["GREEN", "AMBER", "RED"][i % 3],
      ).map((risk, i) => ({
        seq: i,
        entry_hash: `record-${i}`,
        ts_ms: 1791280800000 + i * 60000,
        payload: signed(`session-${i}-example`, risk),
      }));
  const api = {
    records,
    valid: true,
    signedIn: !options.auth,
    failAudit: false,
    sessions: 0,
    frames: 0,
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path.endsWith("/health"))
      body = { status: "ok", demo_mode: true, model_version: "stub-demo" };
    if (path.endsWith("/operator"))
      body = { online_mode: !!options.auth, authenticated: api.signedIn };
    if (path.endsWith("/operator/login")) {
      api.signedIn = true;
      body = { authenticated: true };
    }
    if (path.endsWith("/audit")) {
      if (api.failAudit)
        return route.fulfill({
          status: 503,
          json: { detail: "Audit service unavailable" },
        });
      const params = new URL(route.request().url()).searchParams;
      const offset = Number(params.get("offset") || 0);
      const risk = params.get("risk");
      const q = params.get("q") || "";
      const matching = [...records]
        .reverse()
        .filter(
          (entry) =>
            (!risk || entry.payload.payload.risk === risk) &&
            (
              entry.payload.payload.session_id +
              " " +
              entry.payload.payload.action
            ).includes(q),
        );
      body = {
        chain_valid: true,
        count: records.length,
        total: matching.length,
        offset,
        has_more: offset + 50 < matching.length,
        entries: matching.slice(offset, offset + 50),
      };
    }
    if (path.endsWith("/session")) {
      api.sessions++;
      body = {
        session_id: "live-session",
        sample_rate: 16000,
        window_samples: 64640,
        hop_samples: 16000,
        model_version: "stub-demo",
        demo_mode: true,
      };
    }
    if (path.endsWith("/verify"))
      body = {
        valid: options.stale ? false : api.valid,
        signature_valid: api.valid,
        fresh: !options.stale && api.valid,
        reason: api.valid ? "Signature valid" : "Signature mismatch",
      };
    if (path.endsWith("/verdict/live-session"))
      body = signed("live-session", options.risk || "GREEN");
    return route.fulfill({ json: body });
  });
  await page.routeWebSocket("**/api/v1/stream/**", (ws) => {
    api.disconnect = () => ws.close();
    const verdict = signed("live-session", options.risk || "GREEN");
    ws.onMessage((data) => {
      if (data === "end") {
        ws.send(JSON.stringify({ ...verdict, type: "verdict" }));
        ws.close();
      } else {
        api.frames++;
        ws.send(
          JSON.stringify({
            type: "score",
            ...verdict.payload,
            inference_ms: 42,
            decision: {
              action: verdict.payload.action,
              reason: "Policy fixture for UI verification",
            },
          }),
        );
      }
    });
  });
  await page.addInitScript(
    ({ denied, volume }) => {
      navigator.mediaDevices.getUserMedia = async () => {
        if (denied)
          throw new DOMException("Microphone access denied", "NotAllowedError");
        const context = new AudioContext();
        await context.resume();
        const oscillator = context.createOscillator();
        const destination = context.createMediaStreamDestination();
        const gain = context.createGain();
        gain.gain.value = volume;
        window.uiTestGain = gain;
        oscillator.connect(gain).connect(destination);
        oscillator.start();
        window.uiTestStream = destination.stream;
        return destination.stream;
      };
    },
    { denied: !!options.denied, volume: options.volume ?? 1 },
  );
  return api;
}

async function noOverflow(page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
}

test("dashboard, audit filtering, record selection and explicit signature checks", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const api = await fixture(page);
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Recent sessions" }),
  ).toBeVisible();
  await expect(page.locator(".session-card")).toHaveCount(4);
  await page.screenshot({
    path: "test-results/ui-dashboard.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  await expect(page.locator("tbody tr")).toHaveCount(3);
  await page.getByLabel("Filter by risk").selectOption("AMBER");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByLabel("Search sessions", { exact: true }).fill("missing");
  await expect(
    page.getByRole("heading", { name: "No matching sessions" }),
  ).toBeVisible();
  await page.getByLabel("Search sessions", { exact: true }).fill("");
  await page.getByLabel("Filter by risk").selectOption("ALL");
  await page
    .getByRole("button", { name: "View session session-0-example" })
    .click();
  await expect(page.locator(".record-details")).toContainText(
    "session-0-example",
  );
  await page.screenshot({ path: "test-results/ui-audit.png", fullPage: true });
  await page.getByRole("button", { name: "Open verdict", exact: true }).click();
  await expect(
    page.getByText("Not yet verified", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Verify signature" }).click();
  await expect(page.getByText("Verified", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/ui-verdict.png",
    fullPage: true,
  });
  api.valid = false;
  await page.getByRole("button", { name: "Verify signature" }).click();
  await expect(
    page.getByText("Verification failed", { exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});

test("live microphone, streamed scores, gate alert and finalized verdict", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (err) => errors.push(err.message));
  const api = await fixture(page, { risk: "RED" });
  await page.goto("/");
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.screenshot({
    path: "test-results/ui-new-session.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Start session", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "High synthetic-speech risk" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue monitoring" }).click();
  await expect(page.locator(".signals-panel:visible")).toContainText("91%");
  await expect.poll(() => api.frames).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Start manual check" }).click();
  await expect(
    page.getByRole("button", { name: "Manual check in progress" }),
  ).toBeDisabled();
  await page.screenshot({ path: "test-results/ui-live.png", fullPage: true });
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Session verdict" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() =>
      window.uiTestStream
        .getTracks()
        .every((track) => track.readyState === "ended"),
    ),
  ).toBe(true);
  await expect(
    page.getByText("Not yet verified", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "New session", exact: true }),
  ).toBeEnabled();
  expect(errors).toEqual([]);
});

test("empty states, denied microphone, modal keyboard dismissal and mobile layouts", async ({
  page,
}) => {
  const api = await fixture(page, { empty: true, denied: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Your sessions will live here" }),
  ).toBeVisible();
  await noOverflow(page);
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Start session", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Microphone access denied",
  );
  expect(api.sessions).toBe(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  for (const title of ["Live Call", "Verdict", "Audit", "Dashboard"]) {
    await page.getByRole("button", { name: title, exact: true }).click();
    await noOverflow(page);
  }
  await page.screenshot({
    path: "test-results/ui-mobile-empty.png",
    fullPage: true,
  });
});

test("operator sign-in and audit error recovery", async ({ page }) => {
  const api = await fixture(page, { auth: true });
  await page.goto("/");
  await page.getByLabel("Operator code").fill("test-operator");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Recent sessions" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  api.failAudit = true;
  await page.getByRole("button", { name: "Refresh records" }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Audit service unavailable",
  );
  api.failAudit = false;
  await page.getByRole("button", { name: "Refresh records" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("populated pages remain usable at phone and tablet widths", async ({
  page,
}) => {
  await fixture(page);
  await page.goto("/");
  await expect(page.locator(".session-card")).toHaveCount(4);
  for (const width of [390, 768, 1024]) {
    await page.setViewportSize({ width, height: 900 });
    for (const title of ["Dashboard", "Live Call", "Audit"]) {
      await page.getByRole("button", { name: title, exact: true }).click();
      await noOverflow(page);
    }
    await page
      .getByRole("button", { name: "Open verdict", exact: true })
      .click();
    await noOverflow(page);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.screenshot({ path: "test-results/ui-mobile.png", fullPage: true });
});

test("waveform follows actual input level and clears after silence and stop", async ({
  page,
}) => {
  await fixture(page, { volume: 0 });
  await page.goto("/");
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Start session", exact: true })
    .click();
  await expect(
    page.getByLabel("Live microphone waveform", { exact: true }),
  ).toBeVisible();
  const newest = () =>
    page
      .locator(".waveform:visible i")
      .last()
      .evaluate((bar) => parseFloat(bar.style.height));
  const peak = () =>
    page
      .locator(".waveform:visible i")
      .evaluateAll((bars) =>
        Math.max(...bars.map((bar) => parseFloat(bar.style.height))),
      );
  await expect.poll(peak).toBe(3);
  await page.evaluate(() => {
    window.uiTestGain.gain.value = 0.01;
  });
  await expect.poll(newest).toBeGreaterThan(20);
  await expect.poll(newest).toBeLessThan(50);
  await page.evaluate(() => {
    window.uiTestGain.gain.value = 0.3;
  });
  await expect.poll(newest).toBeGreaterThan(70);
  await page.evaluate(() => {
    window.uiTestGain.gain.value = 0;
  });
  await expect.poll(peak, { timeout: 6000 }).toBe(3);
  await page.getByRole("button", { name: "End session", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Session verdict" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await expect.poll(peak).toBe(3);
});

test("responsive layout adapts to narrow, landscape and ultrawide screens", async ({
  page,
}) => {
  test.setTimeout(60000);
  await fixture(page);
  await page.goto("/");
  await expect(page.locator(".session-card")).toHaveCount(4);
  for (const [width, height] of [
    [320, 568],
    [360, 800],
    [600, 900],
    [768, 1024],
    [844, 390],
    [1024, 768],
    [1366, 768],
    [1920, 1080],
    [2560, 1080],
  ]) {
    await page.setViewportSize({ width, height });
    for (const title of ["Dashboard", "Live Call", "Audit"]) {
      await page.getByRole("button", { name: title, exact: true }).click();
      await noOverflow(page);
      for (const tab of await page.locator(".nav-tab").all()) {
        const box = await tab.boundingBox();
        expect(box.x).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width).toBeLessThanOrEqual(width + 1);
        expect(box.height).toBeGreaterThanOrEqual(44);
      }
      if (title === "Live Call") await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
      if (title === "Live Call" && width <= 800) {
        const analysis = await page.locator(".analysis-panel:visible").boundingBox();
        const sources = await page.locator(".sources-panel:visible").boundingBox();
        expect(analysis.y).toBeLessThan(sources.y);
      }
    }
    await page
      .getByRole("button", { name: "Open verdict", exact: true })
      .click();
    await noOverflow(page);
    await page.getByRole("button", { name: "Dashboard", exact: true }).click();
    if (width >= 1920) {
      const box = await page.locator(".summary-grid").boundingBox();
      expect(box.width).toBeGreaterThan(width * 0.85);
    }
    await page
      .getByRole("button", { name: "New session", exact: true })
      .click();
    const modal = await page.getByRole("dialog").boundingBox();
    expect(modal.width).toBeLessThan(width);
    expect(modal.height).toBeLessThan(height);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    if (width === 320 || width === 1920)
      await page.screenshot({
        path: `test-results/responsive-${width}.png`,
        fullPage: true,
      });
  }
});

test("audit paging searches older records and historical signatures remain distinct from freshness", async ({
  page,
}) => {
  await fixture(page, { count: 65, stale: true });
  await page.goto("/");
  await page.getByRole("button", { name: "Audit", exact: true }).click();
  await expect(page.locator("tbody tr")).toHaveCount(50);
  await page.getByRole("button", { name: "Next records" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(15);
  await page
    .getByLabel("Search sessions", { exact: true })
    .fill("session-64-example");
  await expect(page.locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "Open verdict", exact: true }).click();
  await page.getByRole("button", { name: "Verify signature" }).click();
  await expect(page.getByText("Verified", { exact: true })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("not fresh enough");
});

test("lost local analysis releases the microphone and removes stale scores", async ({
  page,
}) => {
  const api = await fixture(page);
  await page.route("**/api/v1/verdict/live-session", (route) =>
    route.fulfill({ status: 404, json: { detail: "No final verdict" } }),
  );
  await page.goto("/");
  await page.getByRole("button", { name: "Live Call", exact: true }).click();
  await page.getByRole("button", { name: "Test my microphone", exact: true }).click();
  await page.getByRole("button", { name: "Dashboard", exact: true }).click();
  await page.getByRole("button", { name: "New session", exact: true }).click();
  await page
    .getByRole("button", { name: "Start session", exact: true })
    .click();
  await expect(page.locator(".signals-panel:visible")).toContainText("12%");
  api.disconnect();
  await expect
    .poll(() =>
      page.evaluate(() =>
        window.uiTestStream
          .getTracks()
          .every((track) => track.readyState === "ended"),
      ),
    )
    .toBe(true);
  await expect(page.locator(".signals-panel:visible")).not.toContainText("12%");
  await expect(
    page.getByText("No active session", { exact: true }),
  ).toBeVisible();
});

test("unified call defaults to another device and keeps microphone testing secondary", async ({ page }) => {
  await fixture(page);
  await page.goto('/call');
  await expect(page.getByRole('navigation', { name: 'Main navigation' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Call another device', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Create call', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Two-device call', exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Test my microphone', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Create call', exact: true })).toBeHidden();
  await page.getByRole('button', { name: 'Call another device', exact: true }).click();
  await page.setViewportSize({ width: 360, height: 800 });
  await noOverflow(page);
});
test("both live modes share panels and fit phone tablet and desktop screens", async ({ page }) => {
  await fixture(page);
  await page.goto('/call');
  for (const width of [360, 900, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const mode of ['Call another device', 'Test my microphone']) {
      await page.getByRole('button', { name: mode, exact: true }).click();
      const workspace = page.locator('.live-workspace:visible');
      for (const title of ['Setup', 'Audio & controls', 'Analysis']) {
        await expect(workspace.getByRole('heading', { name: title, exact: true })).toBeVisible();
      }
      await noOverflow(page);
      const source = await workspace.locator('.sources-panel').boundingBox();
      const audio = await workspace.locator('.analysis-panel').boundingBox();
      const signals = await workspace.locator('.signals-panel').boundingBox();
      if (width === 1440) {
        expect(source.x).toBeLessThan(audio.x);
        expect(audio.x).toBeLessThan(signals.x);
      }
      if (width === 360) expect(audio.y).toBeLessThan(source.y);
      if (width === 1440) await page.screenshot({path: `test-results/shared-${mode === 'Call another device' ? 'call' : 'microphone'}.png`, fullPage: true});
    }
  }
});
test("theme toggle persists and sign-in matches both themes on mobile and desktop", async ({ page }) => {
  await page.emulateMedia({colorScheme: 'light'});
  await fixture(page, {auth: true});
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Welcome to your workspace'})).toBeVisible();
  await page.getByRole('button', {name: 'Switch to dark mode'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await page.screenshot({path: 'test-results/sign-in-dark.png', fullPage: true});
  await page.setViewportSize({width: 360, height: 800});
  await noOverflow(page);
  await page.getByRole('button', {name: 'Switch to light mode'}).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme','light');
  await page.getByLabel('Operator code', {exact:true}).fill('fixture-access');
  await page.getByRole('button', {name:'Sign in',exact:true}).click();
  await page.getByRole('button', {name:'Live Call',exact:true}).click();
  await page.getByRole('button', {name:'Switch to dark mode'}).click();
  await noOverflow(page);
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:'test-results/workspace-dark.png',fullPage:true});
  await page.getByRole('button', {name:'Test my microphone',exact:true}).click();
  await noOverflow(page);
});
test("startup retries failed operator checks after a slow backend without overlapping health polls", async ({page}) => {
  await fixture(page, {auth:true});
  let operators = 0;
  const pending = new Set();
  let maximum = 0;
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/health') {
      pending.add(request);
      maximum = Math.max(maximum, pending.size);
    }
  });
  page.on('requestfinished', request => pending.delete(request));
  page.on('requestfailed', request => pending.delete(request));
  await page.route('**/api/health', async route => {
    await new Promise(resolve => setTimeout(resolve, 6000));

    await route.fulfill({json:{status:'ok',online_mode:true}});
  });
  await page.route('**/api/v1/operator', async route => {
    operators++;
    if (operators === 1) return route.abort('failed');
    await route.fulfill({json:{online_mode:true, authenticated:false}});
  });
  await page.goto('/');
  await expect(page.getByText('The backend is taking longer to respond. Retrying automatically…')).toBeVisible({timeout:12000});
  await expect(page.getByRole('heading',{name:'Welcome to your workspace'})).toBeVisible({timeout:20000});
  expect(operators).toBe(2);
  expect(maximum).toBe(1);
});