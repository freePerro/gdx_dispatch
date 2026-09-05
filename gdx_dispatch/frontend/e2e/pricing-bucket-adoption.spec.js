import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const SECRET = process.env.E2E_PASSWORD;

const BASE_BUCKETS = ['doors', 'openers', 'parts', 'labor', 'other'];

/**
 * q4 — a seeded pricing tier reaches the invoice line editor.
 *
 * p5 refused to adopt any bucket name the synonym table already knew, so an
 * admin-seeded `accessories` tier was honoured by the server and ignored by the
 * client. Since `add_invoice_line` stores the client's unit_price verbatim, the
 * client's tier choice is the one that reaches the customer.
 *
 * This spec seeds nothing itself — the tier is seeded on the container before
 * the run — it only proves the client SEES it. It skips when the tenant has
 * seeded no extra tier, so it is safe in a default suite run.
 */
test('a seeded tier becomes a real category option', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: SECRET },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token: token } = await r.json();

  // What the SERVER believes is valid.
  const cats = await (await api.get('/api/catalogs/pricing-categories', {
    headers: { authorization: `Bearer ${token}`},
  })).json();
  const seeded = cats.filter((c) => !BASE_BUCKETS.includes(c));
  test.skip(seeded.length === 0, 'tenant has seeded no extra pricing tier');

  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: token, tid: TENANT });

  await page.goto('/billing/new');
  await expect(page.locator('[data-testid="line-items-editor"]')).toBeVisible({ timeout: 30000 });

  const select = page.locator('[data-testid="line-cat-0"]');
  await expect(select).toBeVisible({ timeout: 15000 });
  // Scroll it up before opening, or the overlay renders clipped by the viewport
  // and the screenshot proves nothing a human can read.
  await select.scrollIntoViewIfNeeded();
  await select.click();

  const panel = page.locator('.p-select-overlay, .p-dropdown-panel').last();
  await expect(panel).toBeVisible({ timeout: 10000 });
  const options = panel.locator('.p-select-option, .p-dropdown-item');
  // Wait for the fade-in to settle so the capture is legible, not mid-animation.
  await expect(options.first()).toBeVisible({ timeout: 10000 });
  const offered = (await options.allTextContents()).map((t) => t.trim().toLowerCase());

  // THE assertion. Under p5 a seeded synonym-table name never reached this list.
  for (const c of seeded) {
    expect(offered, `server offers "${c}" but the dropdown does not`).toContain(c);
  }
  // And no non-labor base bucket went missing while widening.
  for (const c of cats.filter((x) => x !== 'labor')) {
    expect(offered, `server offers "${c}" but the dropdown does not`).toContain(c);
  }

  // A seeded type is a real category, so it must sort ahead of the "Other"
  // catch-all rather than below it.
  for (const c of seeded) {
    expect(offered.indexOf(c)).toBeLessThan(offered.indexOf('other'));
  }

  // Evidence that a human can read. The overlay's list scrolls internally —
  // seven options in the DOM, five in view — so both a full-page shot and a
  // bare panel shot cut off exactly the option this test is about. Scroll the
  // seeded one into frame first, then capture the panel.
  const seededOption = panel.locator('.p-select-option, .p-dropdown-item')
    .filter({ hasText: new RegExp(`^\\s*${seeded[0]}\\s*$`, 'i') })
    .first();
  await seededOption.scrollIntoViewIfNeeded();
  await expect(seededOption).toBeVisible();
  await panel.screenshot({ path: 'test-results/q4-seeded-accessories.png' });
  await api.dispose();
});
