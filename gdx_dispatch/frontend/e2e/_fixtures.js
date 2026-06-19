// Fixtures load the token + param IDs that global-setup wrote to disk.
// No per-test login — that hits the auth rate limiter.
import fs from 'node:fs';
import path from 'node:path';
import { test as base, expect } from '@playwright/test';

const FIXTURES_PATH = path.resolve('e2e/.state/fixtures.json');

function loadFixtures() {
  if (!fs.existsSync(FIXTURES_PATH)) {
    throw new Error(`fixtures missing — globalSetup did not run: ${FIXTURES_PATH}`);
  }
  return JSON.parse(fs.readFileSync(FIXTURES_PATH, 'utf8'));
}

export const test = base.extend({
  paramIds: async ({}, use) => {
    await use(loadFixtures().ids);
  },
  // Override `page` to prime sessionStorage with the SPA's auth keys
  // (gdx_access_token / gdx_tenant_slug) before any script runs, and to stub
  // /api/settings/modules so the SPA's module-gate composable doesn't hammer
  // the rate limiter across every navigation (useTenantModules calls it
  // onMounted for every view).
  page: async ({ page }, use) => {
    const fx = loadFixtures();
    await page.addInitScript(
      ({ token, tenant }) => {
        sessionStorage.setItem('gdx_access_token', token);
        sessionStorage.setItem('gdx_tenant_slug', tenant);
      },
      { token: fx.token, tenant: fx.tenant },
    );
    // Stub /api/settings/modules — return every module enabled so the harness
    // exercises every route regardless of the lab tenant's module grants.
    // The /api/settings/modules endpoint's real behavior is covered by
    // backend unit tests; here we only care about SPA render.
    await page.route('**/api/settings/modules', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(
          [
            'jobs', 'customers', 'estimates', 'invoices', 'dispatch', 'timeclock',
            'inventory', 'quickbooks', 'customer_portal', 'equipment_tracking',
            'campaigns', 'gps_dispatch', 'ai_estimates', 'ai_dispatch',
            'ai_communication', 'stripe_connect', 'loyalty', 'warranties',
            'automations', 'documents', 'communications', 'reports_advanced',
            'mobile', 'segments', 'google_maps', 'chrome_extension',
          ].map((key) => ({ key, enabled: true, tier: 'business', locked: false })),
        ),
      });
    });
    await use(page);
  },
});

export { expect };
