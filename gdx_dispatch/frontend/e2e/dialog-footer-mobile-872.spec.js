// GDXA-872: on a 390x844 phone the Save/Cancel row of CustomerFormDialog was
// the last item of the scrolling dialog content and started below the dialog's
// edge. The actions now sit in the PrimeVue #footer slot. This walks the dialog
// at phone size, light and dark, with and without the --keyboard-inset that
// lib/keyboardInset.js publishes on iOS, and proves Save and Enter still
// submit the form. API writes are fulfilled by page.route, so nothing is
// created in the database the run points at.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';
const VIEWPORT = { width: 390, height: 844 };

test.use({ viewport: VIEWPORT, hasTouch: true });

async function signIn(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript(
    (a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    },
    { t: access_token, tid: TENANT, theme },
  );
  return { api, headers: { authorization: `Bearer ${access_token}`, 'x-tenant-id': TENANT } };
}

// The button is fully on screen, inside the dialog, and above the inset.
async function expectReachable(page, selector, inset = 0) {
  const btn = page.locator(selector);
  await expect(btn).toBeVisible();
  const b = await btn.boundingBox();
  const d = await page.locator('.p-dialog').last().boundingBox();
  expect(b.y).toBeGreaterThanOrEqual(0);
  expect(b.y + b.height).toBeLessThanOrEqual(VIEWPORT.height - inset);
  expect(b.y + b.height).toBeLessThanOrEqual(d.y + d.height);
}

// At phone width /customers is the mobile list, which has its own create page;
// the shared dialog reaches a phone through "Edit customer" on an invoice.
for (const theme of ['light', 'dark']) {
  test(`customer dialog footer reachable on a phone (${theme})`, async ({ page, baseURL }) => {
    const { api, headers } = await signIn(page, baseURL, theme);
    const list = await (await api.get('/api/invoices?per_page=50', { headers })).json();
    const items = Array.isArray(list) ? list : list.items || list.data || [];
    const inv = items.find((i) => i.customer_id);
    await api.dispose();
    test.skip(!inv, 'no invoice with a customer in this database');

    let patched = null;
    await page.route('**/api/customers/*', async (route) => {
      if (route.request().method() !== 'PATCH') return route.continue();
      patched = route.request().postDataJSON();
      await route.fulfill({ json: { id: inv.customer_id, ...patched } });
    });
    await page.goto(`/billing/${inv.id}`);
    await expect.poll(() => page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    await page.locator('[data-testid="invoice-edit-customer-btn"]').click();
    await expect(page.locator('[data-testid="customer-form-dialog"]')).toBeVisible();

    // Measured on main (2026-10-13): this edit dialog fits 390x844 without a
    // keyboard, so the first-paint checks pass there too; the keyboard-inset
    // step below is what fails on main. Logged for the record, not asserted.
    const overflow = await page.locator('.p-dialog-content').last().evaluate((el) => [el.scrollHeight, el.clientHeight, el.scrollTop]);
    console.log(`[${theme}] content scrollHeight/clientHeight/scrollTop`, overflow);
    expect(overflow[2]).toBe(0);

    await expectReachable(page, '[data-testid="customer-submit-btn"]');
    await expectReachable(page, '[data-testid="customer-cancel-btn"]');
    await page.screenshot({ path: `${SHOTS}/872-customer-${theme}.png` });

    // iOS keyboard: lib/keyboardInset.js publishes the covered height.
    await page.locator('[data-testid="customer-name-input"]').focus();
    await page.evaluate(() => document.documentElement.style.setProperty('--keyboard-inset', '300px'));
    await expectReachable(page, '[data-testid="customer-submit-btn"]', 300);
    await page.screenshot({ path: `${SHOTS}/872-customer-${theme}-kb.png` });

    // A refusal is visible from the pinned Save, not left below the fold.
    await page.locator('[data-testid="customer-name-input"]').fill('');
    await page.locator('[data-testid="customer-submit-btn"]').click();
    const err = page.locator('[data-testid="customer-form-error"]');
    await expect(err).toBeInViewport();
    const eb = await err.boundingBox();
    expect(eb.y + eb.height).toBeLessThanOrEqual(VIEWPORT.height - 300);
    await page.evaluate(() => document.documentElement.style.removeProperty('--keyboard-inset'));

    // That empty name was refused by submitForm: nothing was sent.
    await expect(err).toHaveText('Name is required.');
    expect(patched).toBeNull();

    // Enter in a field submits the form through submitForm.
    await page.locator('[data-testid="customer-name-input"]').fill('GDXA-872 Enter');
    await page.locator('[data-testid="customer-name-input"]').press('Enter');
    await expect.poll(() => patched?.name).toBe('GDXA-872 Enter');
    await expect(page.locator('[data-testid="customer-form-dialog"]')).toBeHidden();

    // The footer Save button submits the same form.
    patched = null;
    await page.locator('[data-testid="invoice-edit-customer-btn"]').click();
    await page.locator('[data-testid="customer-name-input"]').fill('GDXA-872 Save');
    await page.locator('[data-testid="customer-submit-btn"]').click();
    await expect.poll(() => patched?.name).toBe('GDXA-872 Save');
  });
}

test.describe('desktop customers list', () => {
  test.use({ viewport: { width: 1280, height: 800 }, hasTouch: false });

  test('create and delete dialogs keep their actions in the footer', async ({ page, baseURL }) => {
    const { api } = await signIn(page, baseURL, 'light');
    await api.dispose();
    let posted = null;
    await page.route('**/api/customers', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      posted = route.request().postDataJSON();
      await route.fulfill({ json: { id: '00000000-0000-0000-0000-000000000872', name: posted.name } });
    });
    await page.goto('/customers');
    await page.locator('[data-testid="new-customer-btn"]').click();
    await expect(page.locator('.p-dialog-footer [data-testid="customer-submit-btn"]')).toBeVisible();
    await page.locator('[data-testid="customer-name-input"]').fill('GDXA-872 Desktop');
    await page.locator('[data-testid="customer-submit-btn"]').click();
    await expect.poll(() => posted?.name).toBe('GDXA-872 Desktop');

    // Open the delete confirmation and back out: nothing is deleted.
    await page.locator('[data-testid^="customer-delete-"]').first().click();
    const del = page.locator('[data-testid="customer-delete-dialog"]');
    await expect(del.locator('.p-dialog-footer [data-testid="customer-confirm-delete-btn"]')).toBeVisible();
    await del.locator('.p-dialog-footer').getByRole('button', { name: 'Cancel' }).click();
    await expect(del).toBeHidden();
  });
});

test('lead intake footer reachable on a phone', async ({ page, baseURL }) => {
  const { api } = await signIn(page, baseURL, 'dark');
  await api.dispose();
  let posted = null;
  await page.route('**/api/leads/intake', async (route) => {
    posted = route.request().postDataJSON();
    await route.fulfill({ json: { lead: { id: '00000000-0000-0000-0000-000000000872', name: posted.name }, matched_customer: null, possible_duplicate: null } });
  });
  await page.goto('/customers');
  await page.locator('[data-testid="quick-capture-fab"]').click();
  const choice = page.locator('[data-testid="choice-lead-intake"]');
  if (await choice.isVisible().catch(() => false)) await choice.click();
  await expect(page.locator('[data-test="intake-save-btn"]')).toBeVisible();
  await expectReachable(page, '[data-test="intake-save-btn"]');
  await page.screenshot({ path: `${SHOTS}/872-lead-intake-dark.png` });
  await page.locator('[data-test="intake-name-input"] input, input[data-test="intake-name-input"]').first().fill('GDXA-872 Lead');
  await page.locator('[data-test="intake-save-btn"]').click();
  await expect.poll(() => posted?.name).toBe('GDXA-872 Lead');
  await expect(page.locator('[data-test="intake-result-pane"]')).toBeVisible();
  await expect(page.locator('[data-test="intake-save-btn"]')).toHaveCount(0);
});
