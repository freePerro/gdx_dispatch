/**
 * Customer statement — browser proof.
 *
 * Seeds its own customer through the real API: three invoices and three
 * payments with dates chosen so every figure on the statement is predictable
 * from the seed, then asserts the dialog shows those figures — not whatever a
 * shared fixture happens to hold. Each assertion here can fail for the defect
 * it exists to catch:
 *
 *  - the office can reach Statement from the customer record;
 *  - "last 30 days" moves the previous balance, and the preview reloads;
 *  - a custom range typed into the inputs is the range requested (a PrimeVue
 *    DatePicker once turned "2026-07-31" into July 3);
 *  - Download saves a PDF under the statement's name;
 *  - a failed send says why in words, and the attempt is recorded.
 *
 * The send goes to an address on the reserved .invalid domain, so no real
 * mailbox can ever receive it.
 */
import fs from 'node:fs';
import path from 'node:path';
import { request as pwRequest } from '@playwright/test';
import { test as base, expect } from './_fixtures.js';

const test = base.extend({
  api: async ({ baseURL }, use) => {
    const fx = JSON.parse(fs.readFileSync(path.resolve('e2e/.state/fixtures.json'), 'utf8'));
    const ctx = await pwRequest.newContext({
      baseURL,
      extraHTTPHeaders: { authorization: `Bearer ${fx.token}`, 'content-type': 'application/json' },
    });
    await use(ctx);
    await ctx.dispose();
  },
});

async function ok(res, what) {
  expect(res.ok(), `${what} must succeed, got ${res.status()}: ${(await res.text()).slice(0, 200)}`).toBeTruthy();
  return res.json();
}

const money = (n) => `$${Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const isoDaysAgo = (n) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};

async function seedCustomer(api) {
  const customer = await ok(
    await api.post('/api/customers', {
      data: { name: `Statement E2E ${Date.now()}`, email: 'statement-e2e@example.invalid', customer_type: 'Commercial' },
    }),
    'creating the customer',
  );
  // A: 50 days ago, paid in two parts (45 and 20 days ago). B: 25 days ago,
  // part paid 10 days ago. C: 5 days ago, unpaid. Totals come back from the API
  // (the tenant's tax rate applies), so expectations are computed, not assumed.
  const plan = [
    { day: 50, amount: 1000, pays: [[400, 45], [300, 20]] },
    { day: 25, amount: 600, pays: [[200, 10]] },
    { day: 5, amount: 250, pays: [] },
  ];
  const invoices = [];
  for (const p of plan) {
    const inv = await ok(
      await api.post('/api/invoices', { data: { customer_id: customer.id, invoice_date: isoDaysAgo(p.day), due_date: isoDaysAgo(p.day) } }),
      'creating an invoice',
    );
    await ok(await api.post(`/api/invoices/${inv.id}/lines`, { data: { description: 'Statement e2e work', quantity: 1, unit_price: p.amount } }), 'adding a line');
    let sent = await api.post(`/api/invoices/${inv.id}/mark-sent`, { data: { sent_via: 'manual' } });
    if (!sent.ok()) {
      await api.post(`/api/invoices/${inv.id}/verify`, { data: {} });
      sent = await api.post(`/api/invoices/${inv.id}/mark-sent`, { data: { sent_via: 'manual' } });
    }
    await ok(sent, 'marking the invoice sent');
    for (const [amount, ago] of p.pays) {
      await ok(await api.post(`/api/invoices/${inv.id}/payments`, { data: { amount, method: 'check', date: isoDaysAgo(ago) } }), 'recording a payment');
    }
    invoices.push({ ...(await ok(await api.get(`/api/invoices/${inv.id}`), 'reading the invoice back')), plan: p });
  }
  return { customer, invoices };
}

async function openStatement(page, customerId, { theme = 'light', width = 1440, height = 900 } = {}) {
  await page.setViewportSize({ width, height });
  await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);
  await page.goto(`/customers/${customerId}`);
  const button = page.locator('[data-testid="customer-statement-btn"]');
  await expect(button).toBeVisible({ timeout: 20000 });
  await button.click();
  await expect(page.locator('[data-testid="statement-total-unpaid"]')).toBeVisible({ timeout: 20000 });
}

test('the office can produce, narrow, download and try to email a statement', async ({ page, api }) => {
  const { customer, invoices } = await seedCustomer(api);
  const [a, b] = invoices;
  const unpaid = invoices.reduce((sum, inv) => sum + Number(inv.balance_due), 0);

  await openStatement(page, customer.id);

  // Last 90 days (default): every invoice and payment is in the period.
  await expect(page.locator('[data-testid="statement-total-unpaid"]')).toHaveText(money(unpaid));
  await expect(page.locator('[data-testid="statement-previous"]')).toHaveText('$0.00');
  await expect(page.locator('[data-testid="statement-ending"]')).toHaveText(money(unpaid));
  await expect(page.locator('[data-testid="statement-preview"]')).toHaveAttribute('src', /^blob:/);

  // Last 30 days: A (50 days ago) is before the period with its first payment.
  const reload = page.waitForResponse((r) => r.url().includes('/statement?preset=last_30'));
  await page.locator('[data-testid="statement-preset"]').click();
  await page.getByRole('option', { name: 'Last 30 days' }).click();
  expect((await reload).ok()).toBeTruthy();
  await expect(page.locator('[data-testid="statement-previous"]')).toHaveText(money(Number(a.total) - 400));
  await expect(page.locator('[data-testid="statement-ending"]')).toHaveText(money(unpaid));

  // Custom range: from A's date to B's date. Balance at the end of it is
  // A + B − the payments dated inside it (400, and 300 only if 20 days ago
  // falls inside, which it doesn't — B is 25 days ago).
  await page.locator('[data-testid="statement-preset"]').click();
  await page.getByRole('option', { name: 'Custom range' }).click();
  await page.locator('input[data-testid="statement-start"]').fill(isoDaysAgo(50));
  await page.locator('input[data-testid="statement-end"]').fill(isoDaysAgo(25));
  const custom = page.waitForResponse((r) => r.url().includes(`/statement?start=${isoDaysAgo(50)}&end=${isoDaysAgo(25)}`));
  await page.locator('[data-testid="statement-apply"]').click();
  expect((await custom).ok()).toBeTruthy();
  await expect(page.locator('[data-testid="statement-ending"]')).toHaveText(money(Number(a.total) + Number(b.total) - 400));

  // Download saves the PDF under the statement's name.
  const download = page.waitForEvent('download');
  await page.locator('[data-testid="statement-download"]').click();
  const file = await download;
  expect(file.suggestedFilename()).toBe(`statement-${isoDaysAgo(50)}-to-${isoDaysAgo(25)}.pdf`);
  const saved = await file.path();
  expect(fs.readFileSync(saved).subarray(0, 5).toString()).toBe('%PDF-');

  // Email: the test user has no mail account, so it must say so in words.
  await expect(page.locator('input[data-testid="statement-to-email"]')).toHaveValue('statement-e2e@example.invalid');
  await page.locator('[data-testid="statement-send"]').click();
  const result = page.locator('[data-testid="statement-result"]');
  await expect(result).toBeVisible({ timeout: 20000 });
  await expect(result).not.toHaveText('');
  const emailed = (await result.textContent()).includes('emailed to');
  expect(emailed, 'a send to a .invalid address must not report success').toBeFalsy();

  // …and the attempt reached the email log.
  const log = await ok(await api.get(`/api/outbound-emails?entity_type=customer&entity_id=${customer.id}&kind=statement`), 'reading the email log');
  expect(log.items.length).toBeGreaterThan(0);
  expect(log.items[0].to_email).toBe('statement-e2e@example.invalid');

  await page.screenshot({ path: 'test-results/statement-light.png' });
});

test('dark mode and a phone-width window render the statement legibly', async ({ page, api }) => {
  const { customer, invoices } = await seedCustomer(api);
  const unpaid = invoices.reduce((sum, inv) => sum + Number(inv.balance_due), 0);
  await openStatement(page, customer.id, { theme: 'dark', width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await expect(page.locator('[data-testid="statement-total-unpaid"]')).toHaveText(money(unpaid));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow, 'the statement dialog must not scroll sideways on a phone').toBeFalsy();
  await page.screenshot({ path: 'test-results/statement-dark-narrow.png' });
});
