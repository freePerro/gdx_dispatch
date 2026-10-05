// GDXA-174 — the Performance page renders real rows with clock hours from the
// hours authority, and a month with nothing recorded says "Not enough data
// yet" instead of a table of zeros. Before: the view read `r.items` off a
// `{users: [...]}` payload, so the table was empty for everyone, always.
//
// Data-dependent by nature: set PERF_DATA_MONTH to a YYYY-MM that has clock
// entries on the target stack, PERF_EMPTY_MONTH to one that has none.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const DATA_MONTH = process.env.PERF_DATA_MONTH;
const EMPTY_MONTH = process.env.PERF_EMPTY_MONTH;
const SHOTS = process.env.PERF_SHOT_DIR || 'test-results';

async function primed(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await api.dispose();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: access_token, tid: TENANT, theme });
}

async function pickMonth(page, ym) {
  const input = page.locator('[data-testid="perf-month"] input, input[data-testid="perf-month"]').first();
  await input.fill(ym);
  await input.press('Enter');
  await page.locator('h2', { hasText: 'Performance Tracker' }).click();
  await page.keyboard.press('Escape');
  // Let the month panel finish fading so the screenshot shows the page.
  await expect(page.locator('.p-datepicker-panel')).toHaveCount(0);
}

for (const theme of ['light', 'dark']) {
  test(`performance page — ${theme}: data month shows rows with clock hours`, async ({ page, baseURL }) => {
    test.skip(!DATA_MONTH, 'PERF_DATA_MONTH not set');
    await primed(page, baseURL, theme);
    await page.goto('/performance');
    await expect(page.locator('h2', { hasText: 'Performance Tracker' })).toBeVisible({ timeout: 15000 });
    const reply = page.waitForResponse((r) => r.url().includes(`/api/performance/users?period=${DATA_MONTH}`));
    await pickMonth(page, DATA_MONTH);
    expect((await reply).status()).toBe(200);
    await expect(page.locator('[data-testid="performance-table"]')).toBeVisible();
    await expect(page.locator('[data-testid="perf-legend"]')).toBeVisible();
    // At least one real clock-hours figure, and the idle rows read "—", not 0.
    const hourCells = page.locator('[data-testid^="hours-"]');
    const texts = await hourCells.allInnerTexts();
    expect(texts.some((t) => /^\d+(\.\d{1,2})?$/.test(t.trim()) && Number(t) > 0)).toBe(true);
    expect(texts.some((t) => t.trim() === '—')).toBe(true);
    // A real figure is never rounded down to a nought that reads as no work.
    expect(texts.some((t) => /^0(\.0+)?$/.test(t.trim()))).toBe(false);
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    await page.screenshot({ path: `${SHOTS}/performance-data-${theme}.png`, fullPage: true });
  });

  test(`performance page — ${theme}: empty month says not enough data yet`, async ({ page, baseURL }) => {
    test.skip(!EMPTY_MONTH, 'PERF_EMPTY_MONTH not set');
    await primed(page, baseURL, theme);
    await page.goto('/performance');
    await expect(page.locator('h2', { hasText: 'Performance Tracker' })).toBeVisible({ timeout: 15000 });
    const reply = page.waitForResponse((r) => r.url().includes(`/api/performance/users?period=${EMPTY_MONTH}`));
    await pickMonth(page, EMPTY_MONTH);
    expect((await reply).status()).toBe(200);
    await expect(page.locator('[data-testid="perf-empty"]')).toBeVisible();
    await expect(page.getByText('Not enough data yet')).toBeVisible();
    await expect(page.locator('[data-testid="performance-table"]')).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/performance-empty-${theme}.png`, fullPage: true });
  });
}
