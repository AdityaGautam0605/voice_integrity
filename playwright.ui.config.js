import { defineConfig } from "@playwright/test";

// Isolated UI checks: deterministic API/stream fixtures, no detector or credentials.
export default defineConfig({
  testDir: "./tests/browser",
  testMatch: "workspace.spec.js",
  timeout: 30000,
  workers: 1,
  use: {
    baseURL: "http://localhost:5174",
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    launchOptions: { args: ["--autoplay-policy=no-user-gesture-required"] },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "node node_modules/vite/bin/vite.js --port 5174 --host 127.0.0.1",
    url: "http://localhost:5174",
    env: { VIF_HTTP: "1" },
    reuseExistingServer: false,
  },
});
