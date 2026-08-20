/**
 * Office Contacts tab — browser proof.
 *
 * customer_contacts shipped with a model, a mobile writer and a recipient
 * picker, and held ZERO rows in production because the office had no way in.
 * A unit test proves wiring; only a browser proves the office can find and
 * finish the job. See docs/design/qb-subcustomer-flattening-plan.md.
 *
 * Uses the shared `paramIds` fixture and the pre-primed `page` from
 * _fixtures.js — global-setup seeds a customer and logs in once. An earlier
 * draft read an E2E_CUSTOMER_ID env var that exists nowhere else in the repo:
 * unset, it navigated to /customers/undefined and the dark-mode and
 * phone-width tests STILL PASSED, because they only asserted on a hard-coded
 * tab literal. Every test here now asserts on data that had to round-trip
 * through the API to exist.
 */
import fs from 'node:fs';
import path from 'node:path';
import { request as pwRequest } from '@playwright/test';
import { test as base, expect } from './_fixtures.js';

// The shared `page` fixture is auth-primed; the base `request` fixture is NOT
// (playwright.config only sets x-tenant-id). An API call through it 401s, which
// would have made every seed in this file fail loudly — but only because the
// seeds assert on `r.ok()`. Build an authed context from the same fixtures file
// global-setup wrote, so no test logs in again and hits the rate limiter.
const test = base.extend({
  api: async ({ baseURL }, use) => {
    const fx = JSON.parse(fs.readFileSync(path.resolve('e2e/.state/fixtures.json'), 'utf8'));
    const ctx = await pwRequest.newContext({
      baseURL,
      extraHTTPHeaders: {
        authorization: `Bearer ${fx.token}`,
        'x-tenant-id': fx.tenant,
        'content-type': 'application/json',
      },
    });
    await use(ctx);
    await ctx.dispose();
  },
});

export { expect };

async function seedContact(api, customerId, name) {
  const r = await api.post(`/api/customers/${customerId}/contacts`, {
    data: {
      name,
      label: 'job contact',
      email: `${name.replace(/\W+/g, '.').toLowerCase()}@example.com`,
    },
  });
  expect(r.ok(), `seeding a contact must succeed, got ${r.status()}`).toBeTruthy();
  return r.json();
}

async function openContacts(page, customerId, { theme = 'light', width = 1440, height = 900 } = {}) {
  await page.setViewportSize({ width, height });
  await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);
  await page.goto(`/customers/${customerId}`);
  const tab = page.getByRole('button', { name: 'Contacts', exact: true });
  await expect(tab).toBeVisible({ timeout: 20000 });
  await tab.click();
  await expect(page.locator('[data-testid="tab-contacts-content"]')).toBeVisible();
}

function field(page, testid) {
  return page.locator(`[data-testid="${testid}"] input, input[data-testid="${testid}"]`).first();
}

test('the office can find, add, edit and promote a contact — and removal asks first',
  async ({ page, api, paramIds }) => {
    await openContacts(page, paramIds.customer);
    await expect(page.locator('[data-testid="add-contact-btn"]')).toBeVisible();

    // ── add ────────────────────────────────────────────────────────────────
    const name = `Pat Walk ${Date.now()}`;
    await page.locator('[data-testid="add-contact-btn"]').click();
    await expect(page.locator('[data-testid="contact-dialog"]')).toBeVisible();
    await field(page, 'contact-name-input').fill(name);
    await field(page, 'contact-label-input').fill('job contact');
    await field(page, 'contact-email-input').fill('pat.walk@example.com');
    await page.locator('[data-testid="save-contact-btn"]').click();
    await expect(page.locator('[data-testid="tab-contacts-content"]')).toContainText(name, { timeout: 15000 });

    const card = page.locator('[data-testid^="contact-"]').filter({ hasText: name }).first();
    const contactId = (await card.getAttribute('data-testid')).replace('contact-', '');

    // it reached the database, not just the DOM
    const listed = await (await api.get(`/api/customers/${paramIds.customer}/contacts`)).json();
    expect(listed.map((c) => c.name)).toContain(name);

    // ── edit: a real PATCH, not delete-and-re-add ──────────────────────────
    await page.locator(`[data-testid="edit-contact-${contactId}"]`).click();
    await expect(page.locator('[data-testid="contact-dialog"]')).toBeVisible();
    await field(page, 'contact-label-input').fill('property manager');
    await page.locator('[data-testid="save-contact-btn"]').click();
    await expect(card).toContainText('property manager', { timeout: 15000 });
    // the id survived — proof it was an update, not a replace
    expect((await card.getAttribute('data-testid')).replace('contact-', '')).toBe(contactId);

    // ── promote: the control this whole recipient feature exists for ───────
    const promote = page.waitForResponse(
      (r) => r.url().includes(`/contacts/${contactId}/make-primary`) && r.request().method() === 'POST');
    await page.locator(`[data-testid="make-primary-${contactId}"]`).click();
    expect((await promote).ok()).toBeTruthy();
    await expect(page.locator(`[data-testid="contact-primary-tag-${contactId}"]`)).toBeVisible({ timeout: 15000 });

    await page.screenshot({ path: 'test-results/contacts-light.png' });

    // ── removal ASKS. useDestructiveConfirm auto-accepts silently (#215), so
    //    this proves a real dialog stands between the click and the delete ──
    await page.locator(`[data-testid="delete-contact-${contactId}"]`).click();
    await expect(page.locator('[data-testid="remove-contact-dialog"]')).toBeVisible();
    await expect(page.locator('[data-testid="remove-primary-warning"]')).toBeVisible();
    // nothing was deleted by opening the dialog — checked at the source
    const stillThere = await (await api.get(`/api/customers/${paramIds.customer}/contacts`)).json();
    expect(stillThere.map((c) => c.name)).toContain(name);

    await page.locator('[data-testid="confirm-remove-contact-btn"]').click();
    await expect(page.locator('[data-testid="tab-contacts-content"]')).not.toContainText(name, { timeout: 15000 });

    // soft-delete, and the role went with the person
    const after = await (await api.get(`/api/customers/${paramIds.customer}/contacts`)).json();
    expect(after.map((c) => c.name)).not.toContain(name);
    expect(after.filter((c) => c.is_primary)).toHaveLength(0);
  });

test('dark mode renders a real contact legibly', async ({ page, api, paramIds }) => {
  const seeded = await seedContact(api, paramIds.customer, `Dark Mode ${Date.now()}`);
  await openContacts(page, paramIds.customer, { theme: 'dark' });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  // asserts on seeded data — fails if the panel renders empty
  const card = page.locator(`[data-testid="contact-${seeded.id}"]`);
  await expect(card).toBeVisible();
  await expect(card).toContainText(seeded.name);

  await page.locator('[data-testid="add-contact-btn"]').click();
  const dlg = page.locator('[data-testid="contact-dialog"]');
  await expect(dlg).toBeVisible();
  // PrimeVue dialogs fade+scale in. Screenshotting on first visibility catches
  // the animation mid-flight and reads as a transparent panel — wait for the
  // settled state before judging the theme.
  await page.waitForTimeout(600);
  await page.screenshot({ path: 'test-results/contacts-dark.png' });
  // Prove opacity rather than eyeball it: a see-through dialog puts dark-mode
  // text on top of the page content behind it.
  const settled = await dlg.evaluate((el) => {
    const cs = getComputedStyle(el);
    return { opacity: cs.opacity, bg: cs.backgroundColor };
  });
  expect(Number(settled.opacity)).toBe(1);
  expect(settled.bg).not.toBe('rgba(0, 0, 0, 0)');

  await api.delete(`/api/customers/${paramIds.customer}/contacts/${seeded.id}`);
});

test('a real contact is usable at phone width', async ({ page, api, paramIds }) => {
  const seeded = await seedContact(api, paramIds.customer, `Phone Width ${Date.now()}`);
  await openContacts(page, paramIds.customer, { theme: 'dark', width: 390, height: 844 });

  const card = page.locator(`[data-testid="contact-${seeded.id}"]`);
  await expect(card).toBeVisible();
  // the row's own actions must be reachable, not clipped off the card
  await expect(page.locator(`[data-testid="edit-contact-${seeded.id}"]`)).toBeVisible();

  // jsdom applies no media queries — this is the check only a browser can make
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: 'test-results/contacts-mobile-dark.png' });

  await api.delete(`/api/customers/${paramIds.customer}/contacts/${seeded.id}`);
});
