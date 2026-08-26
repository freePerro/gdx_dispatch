// Prod walk only. The default config's globalSetup logs in with a password
// and seeds fixtures — neither is wanted against production, where the
// session is primed directly and nothing may be created.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: 'https://gdx.teamgaragedoor.com',
    channel: 'chrome',
    headless: true,
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
  },
});
