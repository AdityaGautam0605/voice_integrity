import { defineConfig } from '@playwright/test';
import process from 'node:process';

// A separate suite: this starts an actual authenticated coturn server.
export default defineConfig({
  testDir: './tests/relay',
  testMatch: '**/*.spec.js',
  timeout: 90000,
  workers: 1,
  expect: { timeout: 20000 },
  use: {
    baseURL: 'https://localhost:5173',
    headless: true,
    ignoreHTTPSErrors: true,
    actionTimeout: 15000,
    launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `"${process.execPath}" tests/relay/server.mjs`,
    url: 'https://localhost:5173/api/health',
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 45000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 6000 },
  },
});
