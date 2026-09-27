/**
 * GDXA-91 — the invoice edit-mode save refuses BEFORE it writes.
 *
 * saveEdit() is a multi-request diff loop: per-line PATCH/POST, then per-line
 * DELETE, then the invoice-level PATCH (tax rate, dates, notes,
 * hide_line_prices) LAST. The quantity refusal used to live INSIDE that loop,
 * so a refusal the client could compute up front landed after earlier lines
 * were already committed — line A's edit stuck, the operator's header edits
 * silently went nowhere, and because the refusal returned without throwing the
 * catch's resync never ran, leaving the table rendering the pre-save snapshot.
 *
 * This spec is the browser-level proof: a GOOD line ordered before the BAD one,
 * plus a header edit, and the assertion that the SERVER is untouched.
 *
 * SEED CONTRACT — E2E_GDXA91_INVOICE_ID must name a DRAFT invoice with
 * exactly two described lines, each with quantity > 0. Nothing depends on
 * their text: the spec reads the line descriptions back from the API and
 * asserts the toast names the one whose quantity it cleared. Index 0 is the
 * "good" line (its quantity is edited and must NOT be committed); index 1 is
 * the one whose quantity is cleared.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const INVOICE_ID = process.env.E2E_GDXA91_INVOICE_ID;

test('edit-mode save refuses with zero writes when a good line precedes a bad one', async ({ page, baseURL }) => {
  test.skip(!INVOICE_ID, 'set E2E_GDXA91_INVOICE_ID to a two-line draft invoice');

  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  const authed = { authorization: `Bearer ${access_token}`, 'x-tenant-id': TENANT, 'x-e2e-test': 'true' };

  // The server's state BEFORE the attempted save.
  const before = await (await api.get(`/api/invoices/${INVOICE_ID}`, { headers: authed })).json();
  const beforeLines = (before.lines || before.line_items || []).map((l) => ({
    id: l.id, description: l.description, quantity: Number(l.quantity), unit_price: Number(l.unit_price),
  }));
  expect(beforeLines.length, 'seed contract: exactly two described draft lines').toBe(2);
  // Read the name out of the data rather than hardcoding it, so a different
  // seeder does not produce a red test for a reason unrelated to this fix.
  const clearedLineName = beforeLines[1].description;
  expect(clearedLineName, 'seed contract: line 1 must have a description').toBeTruthy();

  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });

  // Record every write this page issues. Zero is the whole point.
  const writes = [];
  page.on('request', (req) => {
    if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(req.method()) && req.url().includes('/api/invoices')) {
      writes.push(`${req.method()} ${new URL(req.url()).pathname}`);
    }
  });

  // The invoice-detail route is /billing/:id (router/index.js) — /invoices/:id 404s.
  await page.goto(`/billing/${INVOICE_ID}`);
  await expect(page.locator('[data-testid="invoice-edit-btn"]')).toBeVisible({ timeout: 20000 });
  await page.click('[data-testid="invoice-edit-btn"]');
  await expect(page.locator('[data-testid="line-items-editor"]')).toBeVisible({ timeout: 15000 });

  // Line 0 = a legitimate edit the pre-fix loop would have COMMITTED.
  const qty0 = page.locator('[data-testid="line-qty-0"] input').first();
  await qty0.fill('');
  await qty0.type('3');
  await qty0.blur();

  // Line 1 = the cleared quantity that triggers the refusal.
  const qty1 = page.locator('[data-testid="line-qty-1"] input').first();
  await qty1.fill('');
  await qty1.dispatchEvent('input');

  // A header edit too: it is sequenced LAST, so pre-fix it never fired at all.
  const rate = page.locator('[data-testid="invoice-edit-tax-rate"] input').first();
  if (await rate.count()) { await rate.fill(''); await rate.type('0'); }

  await page.click('[data-testid="invoice-edit-save"]');

  // The refusal is visible to the operator, and it names the offending line.
  const toast = page.locator('.p-toast-message-warn, .p-toast-message[data-pc-severity="warn"]').first();
  await expect(toast).toBeVisible({ timeout: 10000 });
  await expect(toast).toContainText('Fix line items first');
  await expect(toast).toContainText(clearedLineName);
  await page.waitForTimeout(1500); // let any stray request land before we count

  // ZERO writes — no /lines/ write, no invoice-level PATCH.
  expect(writes, `page issued writes: ${JSON.stringify(writes)}`).toEqual([]);

  // And the server really is untouched: line 0 is still quantity 1, not 3.
  const after = await (await api.get(`/api/invoices/${INVOICE_ID}`, { headers: authed })).json();
  const afterLines = (after.lines || after.line_items || []).map((l) => ({
    id: l.id, description: l.description, quantity: Number(l.quantity), unit_price: Number(l.unit_price),
  }));
  expect(afterLines).toEqual(beforeLines);
  expect(Number(after.total)).toBe(Number(before.total));

  await api.dispose();
});

test('the refusal toast is readable in dark mode', async ({ page, baseURL }) => {
  test.skip(!INVOICE_ID, 'set E2E_GDXA91_INVOICE_ID to a two-line draft invoice');
  await page.setViewportSize({ width: 1440, height: 1100 });

  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', 'dark');
  }, { t: access_token, tid: TENANT });

  await page.goto(`/billing/${INVOICE_ID}`);
  await expect(page.locator('[data-testid="invoice-edit-btn"]')).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  await page.click('[data-testid="invoice-edit-btn"]');
  await expect(page.locator('[data-testid="line-items-editor"]')).toBeVisible({ timeout: 15000 });
  const qty1 = page.locator('[data-testid="line-qty-1"] input').first();
  await qty1.fill('');
  await qty1.dispatchEvent('input');
  await page.click('[data-testid="invoice-edit-save"]');

  // This toast is the ONLY thing telling the operator why the save did not
  // happen. If it is murky in dark mode the refusal is silent in practice.
  const detail = page.locator('.p-toast-message .p-toast-detail').first();
  await expect(detail).toBeVisible({ timeout: 10000 });
  // Let the PrimeVue enter animation settle first: measuring contrast during
  // the fade measures the animation, not the theme.
  await expect(page.locator('.p-toast-message').first()).toHaveCSS('opacity', '1');
  await page.screenshot({ path: 'test-results/invoice-edit-refusal-dark.png' });

  // Contrast is MEASURED, not asserted-truthy (the can't-fail shape this repo
  // keeps catching). Same WCAG computation as invoice-void-ui.spec.js.
  const ratio = await detail.evaluate((el) => {
    const parse = (v) => (v.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => {
      const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    let node = el, bg = null;
    while (node && !bg) {
      const c = getComputedStyle(node).backgroundColor;
      if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') bg = parse(c);
      node = node.parentElement;
    }
    const fg = parse(getComputedStyle(el).color);
    if (!bg) return null;
    const [a, b2] = [lum(fg), lum(bg)].sort((x, y) => y - x);
    return (a + 0.05) / (b2 + 0.05);
  });
  expect(ratio, 'could not resolve a background to measure against').not.toBeNull();
  expect(ratio).toBeGreaterThan(4.5);
  await api.dispose();
});
