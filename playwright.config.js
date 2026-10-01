import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const testData = join(tmpdir(), `vif-browser-${Date.now()}`);
const https = process.env.VIF_TEST_HTTPS === '1';
const origin = `${https ? 'https' : 'http'}://localhost:5173`;
export default defineConfig({
  testDir: './tests/browser', timeout: 60000, workers: 1,
  expect: { timeout: 15000 },
  use: {
    baseURL: origin, headless: true, actionTimeout: 15000, ignoreHTTPSErrors: https,
    launchOptions: { args: ['--autoplay-policy=no-user-gesture-required'] },
    screenshot: 'only-on-failure', trace: 'retain-on-failure',
  },
  webServer: {
    command: `node scripts/demo.mjs${https ? '' : ' --http'}`, url: `${origin}/api/health`, ignoreHTTPSErrors: https,
    reuseExistingServer: false, timeout: 30000,
    env: { VIF_BACKEND: 'stub', VIF_DEMO_SCENARIOS: '1', VIF_VAD: 'energy',
      VIF_KEYS_DIR: join(testData, 'keys'), VIF_DATA_DIR: join(testData, 'data') },
  },
});
