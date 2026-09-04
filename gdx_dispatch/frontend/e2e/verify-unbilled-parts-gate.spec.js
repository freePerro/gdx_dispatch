/**
 * Verify refuses while recorded parts are billed to nothing — and the office
 * gets a choice, not a dead end.
 *
 * The unbilled-parts warning used to be a banner on this screen only. Being
 * client-side it could not help the accounting role (holds invoices.write but
 * NOT inventory.read, so its own fetch 403s and the empty banner reads as an
 * all-clear on a money screen), the mobile lane, or any API caller. The
 * server now refuses at verify and says what is missing.
 *
 * jsdom proves the handler; only a browser proves the dialog renders and is
 * readable in both themes.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const INVOICE_ID = process.env.E2E_GATE_INVOICE_ID;

async function login(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: access_token, tid: TENANT });
  return { api, token: access_token };
}

test('verify surfaces the missing parts and lets the office decide', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const { api, token } = await login(page, baseURL);

  await page.goto(`/billing/${INVOICE_ID}`);
  await expect(page.getByTestId('invoice-number')).toBeVisible({ timeout: 20000 });

  await page.getByTestId('verify-invoice-btn').click();

  // The refusal renders as a question, not a red toast.
  const dialog = page.getByTestId('verify-unbilled-dialog');
  await expect(dialog).toBeVisible({ timeout: 15000 });
  await expect(dialog).toHaveCSS('opacity', '1');
  const list = page.getByTestId('verify-unbilled-list');
  await expect(list).toBeVisible();
  await expect(list).toContainText('Torsion spring (gate walk)');
  // It names the money, so "is this worth chasing" is answerable on the spot.
  await expect(list).toContainText('$149.00');
  await page.screenshot({ path: 'test-results/verify-gate-light.png' });

  // Both ways out exist. A refusal with no exit is the dead end this replaces.
  await expect(page.getByTestId('verify-unbilled-edit')).toBeVisible();
  await expect(page.getByTestId('verify-anyway-btn')).toBeVisible();

  // Still unverified while the question stands.
  const beforeRes = await api.get(`/api/invoices/${INVOICE_ID}`, {
    headers: { authorization: `Bearer ${token}`},
  });
  expect((await beforeRes.json()).verified_at).toBeFalsy();

  await page.getByTestId('verify-anyway-btn').click();
  await expect(page.getByTestId('invoice-verified-tag')).toBeVisible({ timeout: 15000 });

  const afterRes = await api.get(`/api/invoices/${INVOICE_ID}`, {
    headers: { authorization: `Bearer ${token}`},
  });
  expect((await afterRes.json()).verified_at).toBeTruthy();
  await page.screenshot({ path: 'test-results/verify-gate-after-light.png' });

  await api.dispose();
});

test('the gate dialog is readable in dark mode', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));

  const second = process.env.E2E_GATE_INVOICE_ID_DARK;
  await page.goto(`/billing/${second}`);
  await expect(page.getByTestId('invoice-number')).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  await page.getByTestId('verify-invoice-btn').click();
  const dialog = page.getByTestId('verify-unbilled-dialog');
  await expect(dialog).toBeVisible({ timeout: 15000 });
  await expect(dialog).toHaveCSS('opacity', '1');
  await page.screenshot({ path: 'test-results/verify-gate-dark.png' });

  // Measured, not assumed — the list is the part that carries a colour.
  const ratio = await page.getByTestId('verify-unbilled-list').evaluate((el) => {
    const parse = (v) => (v.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => {
      const f = (c) => {
        c /= 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      };
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
  expect(ratio).not.toBeNull();
  expect(ratio).toBeGreaterThan(4.5);
});
