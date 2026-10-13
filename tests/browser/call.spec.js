import { test, expect } from "@playwright/test";

async function fakeMicrophone(page) {
  await page.addInitScript(() => {
    // A controllable source goes through real WebRTC encoding/decoding.
    navigator.mediaDevices.getUserMedia = async () => {
      await window.testMicContext?.close();
      const context = (window.testMicContext = new AudioContext());
      await context.resume();
      const oscillator = context.createOscillator();
      oscillator.frequency.value = 440;
      const gain = (window.testMicGain = context.createGain());
      gain.gain.value = 0;
      const output = context.createMediaStreamDestination();
      oscillator.connect(gain).connect(output);
      oscillator.start();
      return output.stream;
    };
  });
}
async function pair(operator, caller, scenario = "", again = false) {
  if (!again) await operator.goto("/call");
  await expect(
    operator.getByText("Backend ready", { exact: true }),
  ).toBeVisible();
  await operator.getByLabel("Detector mode").selectOption(scenario);
  await operator
    .getByRole("button", {
      name: again ? "Create another call" : "Create call",
      exact: true,
    })
    .click();
  const invite = operator.getByLabel("Caller invitation URL");
  await expect(invite).toBeVisible();
  await caller.goto(await invite.inputValue());
  await caller.getByRole("button", { name: "Join call", exact: true }).click();
  await expect(operator.getByText("Connected", { exact: true })).toBeVisible();
  await expect(caller.getByText("Connected", { exact: true })).toBeVisible();
  await expect(
    operator.getByText("Collecting speech", { exact: true }),
  ).toBeVisible();
}
async function talk(page, level) {
  await page.evaluate((volume) => {
    window.testMicGain.gain.value = volume;
  }, level);
}

for (const [scenario, action] of [
  ["GREEN", "PROCEED"],
  ["AMBER", "CHALLENGE"],
  ["RED", "GATE_ACTION"],
]) {
  test(`two-way call, ${scenario} policy, signed verdict and tamper rejection`, async ({
    browser,
  }) => {
    const operator = await browser.newPage();
    const caller = await browser.newPage({
      viewport: { width: 393, height: 852 },
    });
    await fakeMicrophone(operator);
    await fakeMicrophone(caller);
    const errors = [];
    operator.on("pageerror", (error) => errors.push(error.message));
    caller.on("pageerror", (error) => errors.push(error.message));
    await pair(operator, caller, scenario);
    // Operator-only speech must be audible to the caller but not analyzed.
    await talk(operator, 0.2);
    await expect
      .poll(() => caller.getByRole("meter").getAttribute("value"))
      .not.toBe("0");
    await operator.waitForTimeout(1800);
    await expect(
      operator.getByText("Collecting speech", { exact: true }),
    ).toBeVisible();
    await talk(caller, 0.2);
    await expect(
      operator.getByRole("heading", { name: action, exact: true }),
    ).toBeVisible();
    await expect
      .poll(() => operator.getByRole("meter").getAttribute("value"))
      .not.toBe("0");
    if (scenario === "AMBER")
      await expect(
        operator.getByText(/Manual verification prompt/),
      ).toBeVisible();
    await caller.getByRole("button", { name: "Mute microphone" }).click();
    await expect(
      caller.getByRole("button", { name: "Unmute microphone" }),
    ).toBeVisible();
    await caller.getByRole("button", { name: "Unmute microphone" }).click();
    if (scenario === "AMBER") {
      await operator.screenshot({
        path: "test-results/operator-live.png",
        fullPage: true,
      });
      await caller.screenshot({
        path: "test-results/caller-live.png",
        fullPage: true,
      });
    }
    await caller.getByRole("button", { name: "End call", exact: true }).click();
    await expect(operator.getByText("Ended", { exact: true })).toBeVisible();
    await expect(
      operator.getByText("Signature verified", { exact: true }),
    ).toBeVisible();
    await operator
      .getByRole("button", { name: "Test an altered verdict" })
      .click();
    await expect(operator.getByText(/Altered verdict rejected/)).toBeVisible();
    expect(errors).toEqual([]);
    await operator.close();
    await caller.close();
  });
}

test("analysis disconnect leaves peer audio connected and hang-up still works", async ({
  browser,
}) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  await fakeMicrophone(operator);
  await fakeMicrophone(caller);
  let cutAnalysis;
  await operator.routeWebSocket("**/api/v1/stream/**", (route) => {
    const server = route.connectToServer();
    cutAnalysis = () => {
      server.close();
      route.close();
    };
  });
  await pair(operator, caller, "RED");
  await talk(caller, 0.2);
  await expect(
    operator.getByRole("heading", { name: "GATE_ACTION", exact: true }),
  ).toBeVisible();
  cutAnalysis();
  await expect(
    operator.getByRole("status").filter({ hasText: /^Unavailable$/ }),
  ).toBeVisible();
  await expect(
    operator.getByRole("heading", { name: "Awaiting analysis", exact: true }),
  ).toBeVisible();
  await expect(operator.getByText("Connected", { exact: true })).toBeVisible();
  await expect(caller.getByText("Connected", { exact: true })).toBeVisible();
  await operator.getByRole("button", { name: "End call", exact: true }).click();
  await expect(caller.getByText("Ended", { exact: true })).toBeVisible();
  await operator.close();
  await caller.close();
});

test("permission denial is recoverable without a leaked active call", async ({
  page,
}) => {
  await page.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException("Denied", "NotAllowedError");
    };
  });
  await page.goto("/call");
  await page.getByRole("button", { name: "Create call", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "Microphone permission was denied",
  );
  await expect(
    page.getByRole("button", { name: "Create another call" }),
  ).toBeEnabled();
});

test("three calls reuse the same operator and phone tabs without stale sessions", async ({
  browser,
}) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  await fakeMicrophone(operator);
  await fakeMicrophone(caller);
  const ids = new Set();
  for (let i = 0; i < 3; i++) {
    await pair(operator, caller, "", i > 0);
    await operator
      .getByRole("button", { name: "End call", exact: true })
      .click();
    await expect(
      operator.getByText("Signature verified", { exact: true }),
    ).toBeVisible();
    await expect(operator.getByText("Ended", { exact: true })).toBeVisible();
    await expect(caller.getByText("Ended", { exact: true })).toBeVisible();
    await expect(
      operator.getByRole("heading", { name: "Insufficient speech" }),
    ).toBeVisible();
    const record = await operator.locator(".record-id").innerText();
    ids.add(record.split("\n")[0]);
    await expect
      .poll(
        async () =>
          (await (await operator.request.get("/api/metrics")).json())
            .active_sessions,
      )
      .toBe(0);
  }
  expect(ids.size).toBe(3);
  await operator.close();
  await caller.close();
});

test("lost signaling and analysis do not interrupt peer audio or peer hang-up", async ({
  browser,
}) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  await fakeMicrophone(operator);
  await fakeMicrophone(caller);
  const cut = [];
  for (const page of [operator, caller]) {
    await page.routeWebSocket("**/api/**", (route) => {
      const server = route.connectToServer();
      cut.push(() => {
        server.close();
        route.close();
      });
    });
  }
  await pair(operator, caller, "GREEN");
  for (const disconnect of cut) disconnect();
  await expect(
    operator.getByRole("status").filter({ hasText: /^Unavailable$/ }),
  ).toBeVisible();
  await talk(operator, 0.2);
  await talk(caller, 0.2);
  for (const page of [operator, caller]) {
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await expect
      .poll(() => page.getByRole("meter").getAttribute("value"))
      .not.toBe("0");
  }
  await caller.getByRole("button", { name: "End call", exact: true }).click();
  await expect(operator.getByText("Ended", { exact: true })).toBeVisible();
  await operator.close();
  await caller.close();
});

test("the web server refuses certificate and backend private files", async ({
  request,
}) => {
  for (const path of [
    "/.certs/ca/rootCA-key.pem",
    "/voice-integrity/keys/verdict_ed25519.pem",
    "/voice-integrity/data/audit.db",
  ]) {
    const response = await request.get(path);
    expect(response.status()).toBe(403);
  }
});

test("hang-up while session creation is pending still finalizes the late session", async ({
  browser,
}) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  await fakeMicrophone(operator);
  await fakeMicrophone(caller);
  let release;
  let creating;
  const pending = new Promise((resolve) => {
    creating = resolve;
  });
  await operator.route("**/api/v1/session", async (route) => {
    const response = await route.fetch();
    creating();
    await new Promise((resolve) => {
      release = resolve;
    });
    await route.fulfill({ response });
  });
  await operator.goto("/call");
  await operator
    .getByRole("button", { name: "Create call", exact: true })
    .click();
  const invite = operator.getByLabel("Caller invitation URL");
  await expect(invite).toBeVisible();
  await caller.goto(await invite.inputValue());
  await caller.getByRole("button", { name: "Join call", exact: true }).click();
  await pending;
  await caller.getByRole("button", { name: "End call", exact: true }).click();
  await expect(operator.getByText("Ending", { exact: true })).toBeVisible();
  release();
  await expect(operator.getByText("Ended", { exact: true })).toBeVisible();
  await expect(
    operator.getByText("Signature verified", { exact: true }),
  ).toBeVisible();
  await expect
    .poll(
      async () =>
        (await (await operator.request.get("/api/metrics")).json())
          .active_sessions,
    )
    .toBe(0);
  await operator.close();
  await caller.close();
});

test("trusted reference upload enrolls and revokes within a real call", async ({
  browser,
}) => {
  const operator = await browser.newPage();
  const caller = await browser.newPage();
  await fakeMicrophone(operator);
  await fakeMicrophone(caller);
  await pair(operator, caller, "GREEN");
  const rate = 16000,
    count = rate * 4;
  const wav = Buffer.alloc(44 + count * 2);
  wav.write("RIFF");
  wav.writeUInt32LE(wav.length - 8, 4);
  wav.write("WAVEfmt ", 8);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(rate, 24);
  wav.writeUInt32LE(rate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(count * 2, 40);
  for (let i = 0; i < count; i++)
    wav.writeInt16LE(
      Math.round(Math.sin((i * 2 * Math.PI * 220) / rate) * 6000),
      44 + i * 2,
    );
  await operator
    .getByLabel("Speaker ID", { exact: true })
    .fill("trusted-reference");
  await operator
    .getByLabel("Reference audio (3–10 seconds)", { exact: true })
    .setInputFiles({ name: "trusted.wav", mimeType: "audio/wav", buffer: wav });
  await operator
    .getByRole("button", { name: "Enroll reference", exact: true })
    .click();
  await expect(
    operator.getByText(
      "Reference enrolled for this demo. Speaker comparison is simulated.",
      { exact: true },
    ),
  ).toBeVisible();
  await operator.getByRole("button", { name: "Revoke reference" }).click();
  await expect(
    operator.getByText("Reference revoked.", { exact: true }),
  ).toBeVisible();
  await operator.getByRole("button", { name: "End call", exact: true }).click();
  await expect(caller.getByText("Ended", { exact: true })).toBeVisible();
  await operator.close();
  await caller.close();
});
