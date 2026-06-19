import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 2,
  reporter: [['list'], ['json', { outputFile: 'e2e-results.json' }]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  globalSetup: './e2e/global-setup.js',
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:8001',
    channel: 'chrome',
    headless: true,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    extraHTTPHeaders: {
      'x-tenant-id': process.env.E2E_TENANT_SLUG || 'lab-tenant',
    },
  },
});
