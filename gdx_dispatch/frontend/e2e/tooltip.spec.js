import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

// Verifies the globally-registered PrimeVue Tooltip directive actually renders
// a tooltip on hover. Headless + deterministic (Playwright auto-waits on the
// tooltip element). We assert across the structurally DISTINCT host types the
// sweep applied v-tooltip to, since rendering can differ by host:
//   1. a PrimeVue <Button> component  (directive must forward to its root)
//   2. a plain native <button>        (directive binds the DOM node directly)
//   3. a <router-link> anchor + .right modifier (native anchor + placement)
// Bare <i>/<span> hosts reduce to case 2 (native element); <Tag> reduces to
// case 1 (single-root component) — so these three cover every category.
async function login(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });
  await api.dispose();
}

function tooltipWith(page, text) {
  return page.locator('[data-pc-name="tooltip"], .p-tooltip').filter({ hasText: text });
}

test('v-tooltip renders on hover across distinct host element types', async ({ page, baseURL }) => {
  await login(page, baseURL);
  await page.goto('/dashboard');

  // Dismiss the welcome tour dialog if present (it overlays the topbar).
  const closeTour = page.getByRole('button', { name: 'Close' }).first();
  if (await closeTour.isVisible().catch(() => false)) await closeTour.click();

  // --- Case 1: PrimeVue <Button> component (topbar Notifications bell) ---
  const bell = page.locator('button[aria-label="Notifications"]');
  await expect(bell).toBeVisible();
  await bell.hover();
  await expect(tooltipWith(page, 'Notifications')).toBeVisible({ timeout: 5000 });

  // Move the pointer away so the first tooltip hides before the next assertion.
  await page.mouse.move(0, 0);

  // --- Case 2: plain native <button> (sidebar search-clear) ---
  // The clear button only appears once the filter has text.
  const filter = page.locator('[data-testid="sidebar-search"]');
  await expect(filter).toBeVisible();
  await filter.fill('cust');
  const clearBtn = page.locator('[data-testid="sidebar-search-clear"]');
  await expect(clearBtn).toBeVisible();
  await clearBtn.hover();
  await expect(tooltipWith(page, 'Clear search')).toBeVisible({ timeout: 5000 });
  await filter.fill('');
  await page.mouse.move(0, 0);

  // --- Case 3: <router-link> anchor + .right modifier (collapsed sidebar icon) ---
  // Collapse the sidebar so the icon-only nav rail (router-links w/ v-tooltip.right) renders.
  const collapse = page.locator('button[aria-label="Collapse sidebar"]');
  await expect(collapse).toBeVisible();
  await collapse.click();
  const firstIcon = page.locator('a.icon-item').first();
  await expect(firstIcon).toBeVisible();
  // The collapsed icon's tooltip text equals its aria-label (the module name).
  const label = await firstIcon.getAttribute('aria-label');
  expect(label && label.length).toBeTruthy();
  await firstIcon.hover();
  await expect(tooltipWith(page, label)).toBeVisible({ timeout: 5000 });
});
