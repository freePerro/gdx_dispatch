/**
 * The office can finally void an invoice.
 *
 * `POST /api/invoices/{id}/void` shipped complete and audited with GL S5 and
 * had ZERO UI callers until 2026-08-23 — no .vue file called it, and prod had
 * never written a single `invoice_voided` audit row. The office simply could
 * not void.
 *
 * This walks the real rendered screen: the button exists, the typed
 * confirmation actually gates the action, voiding works, and the parts the
 * invoice claimed come back onto the unbilled checklist. Light and dark.
 *
 * jsdom applies no media queries and would not prove the dialog renders —
 * only a browser does.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const INVOICE_ID = process.env.E2E_VOID_INVOICE_ID;
const PART_ID = process.env.E2E_VOID_PART_ID;
const JOB_ID = process.env.E2E_VOID_JOB_ID;
// Its own draft, so the dark pass opens a REAL dialog instead of
// photographing whatever state the light test happened to leave behind.
const DARK_INVOICE_ID = process.env.E2E_VOID_INVOICE_ID_DARK;

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

test('the office can void an invoice, and the typed confirmation gates it', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const { api, token } = await login(page, baseURL);
  const authed = { authorization: `Bearer ${token}`};

  // --- the part starts CLAIMED -------------------------------------------
  // Assert the CLAIM directly, on the unfiltered list. An earlier version of
  // this test only checked "absent from the unbilled list", which is trivially
  // true for any part the list's status filter excludes — it passed while the
  // seed had silently produced a `needed` part the banner never shows. A setup
  // assertion that cannot fail is not a setup assertion.
  const claim = async () => {
    const res = await api.get(`/api/jobs/${JOB_ID}/parts-needed`, { headers: authed });
    const rows = await res.json();
    const list = Array.isArray(rows) ? rows : (rows.items ?? []);
    return list.find((p) => String(p.id) === PART_ID);
  };
  const unbilledIds = async () => {
    const res = await api.get(
      `/api/jobs/${JOB_ID}/parts-needed?status=ordered,received,used&unbilled=true`,
      { headers: authed },
    );
    const rows = await res.json();
    const list = Array.isArray(rows) ? rows : (rows.items ?? []);
    return list.map((p) => String(p.id));
  };

  const seeded = await claim();
  expect(seeded, 'setup: the seeded part must exist on the job').toBeTruthy();
  expect(seeded.status, 'setup: a `needed` part never reaches the unbilled banner').toBe('received');
  expect(seeded.billed_invoice_id, 'setup: the invoice must have claimed the part').toBeTruthy();
  expect(await unbilledIds(),
    'setup: a claimed part is off the unbilled checklist').not.toContain(PART_ID);

  await page.goto(`/billing/${INVOICE_ID}`);
  await expect(page.getByTestId('invoice-number')).toBeVisible({ timeout: 20000 });
  const invoiceNumber = (await page.getByTestId('invoice-number').innerText()).trim();

  // --- the button exists at all (this is the whole point) -----------------
  const voidBtn = page.getByTestId('void-invoice-btn');
  await expect(voidBtn, 'the Void button must be on the invoice screen').toBeVisible();
  await page.screenshot({ path: 'test-results/void-actions-light.png' });

  await voidBtn.click();
  const dialog = page.getByTestId('void-invoice-dialog');
  await expect(dialog).toBeVisible();

  // The dialog says what happens, not just "are you sure".
  await expect(page.getByTestId('void-consequences')).toContainText('unbilled checklist');
  await expect(dialog).toContainText('permanent');
  // Let the modal's enter transition finish — a screenshot taken mid-fade is
  // evidence of nothing, and this is the artifact a human reviews.
  await expect(dialog).toHaveCSS('opacity', '1');

  // --- the typed confirmation is REAL, not decorative ---------------------
  const confirmBtn = page.getByTestId('void-confirm-btn');
  await expect(confirmBtn, 'confirm must be disabled before typing').toBeDisabled();

  await page.getByTestId('void-confirm-input').fill('not-the-number');
  await expect(confirmBtn, 'a wrong value must not enable confirm').toBeDisabled();

  await page.screenshot({ path: 'test-results/void-dialog-light.png' });

  await page.getByTestId('void-confirm-input').fill(invoiceNumber);
  await expect(confirmBtn, 'the exact invoice number must enable confirm').toBeEnabled();

  await confirmBtn.click();

  // --- it actually voided -------------------------------------------------
  await expect(page.getByTestId('invoice-status')).toContainText(/void/i, { timeout: 20000 });
  await expect(page.getByTestId('void-invoice-btn'),
    'a voided invoice must not still offer Void').toHaveCount(0);
  await expect(page.getByTestId('invoice-void-tag')).toBeVisible();
  await page.screenshot({ path: 'test-results/void-after-light.png' });

  // --- THE POINT: the part is back on the unbilled checklist --------------
  const released = await claim();
  expect(released.billed_invoice_id,
    'voiding must release the claim').toBeFalsy();
  expect(await unbilledIds(),
    'voiding must put the part back on the unbilled checklist').toContain(PART_ID);

  await api.dispose();
});

test('the void dialog is readable in dark mode', async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  await login(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));

  await page.goto(`/billing/${DARK_INVOICE_ID}`);
  await expect(page.getByTestId('invoice-number')).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  await page.getByTestId('void-invoice-btn').click();
  await expect(page.getByTestId('void-invoice-dialog')).toBeVisible();
  // The consequence panel carries a colour and a border — the two things that
  // go white-on-white when a fixed hex sneaks in instead of a theme token.
  await expect(page.getByTestId('void-invoice-dialog')).toHaveCSS('opacity', '1');
  await expect(page.getByTestId('void-consequences')).toBeVisible();
  await page.screenshot({ path: 'test-results/void-dialog-dark.png' });

  // Contrast has to be MEASURED. An earlier version asserted
  // `expect(color).toBeTruthy()`, which getComputedStyle can never fail —
  // exactly the can't-fail assertion this repo keeps catching. Compute the
  // real WCAG contrast ratio against the effective background instead.
  const ratio = await page.getByTestId('void-consequences').evaluate((el) => {
    const parse = (v) => (v.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
    const lum = ([r, g, b]) => {
      const f = (c) => {
        c /= 255;
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    // Walk up for the first non-transparent background — the panel's own may
    // legitimately be a translucent token over the dialog surface.
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
  // 4.5:1 is WCAG AA for body text. This panel is the part that explains an
  // irreversible action, so it is exactly the text that must not be murky.
  expect(ratio).toBeGreaterThan(4.5);
});
