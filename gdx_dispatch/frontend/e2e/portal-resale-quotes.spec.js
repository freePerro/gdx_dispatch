// Contractor resale quotes (PR B): a contractor signs in to the portal,
// agrees to the reseller terms, sets up their brand and logo, resells one of
// their estimates at their own markup, downloads the branded PDF, and deletes
// the quote. Seeds its own contractor customer, a sent estimate and a portal
// session through the staff API, so it runs on any fresh test server. Never
// point it at a server holding real data: it writes.
import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from '@playwright/test';

const FIXTURES_PATH = path.resolve('e2e/.state/fixtures.json');
// 1x1 PNG.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
);

test.skip(process.env.E2E_READ_ONLY === '1', 'writes: seeds a customer, an estimate and a portal user');

async function seed(request) {
  const { token } = JSON.parse(fs.readFileSync(FIXTURES_PATH, 'utf8'));
  const staff = { authorization: `Bearer ${token}`, 'content-type': 'application/json' };
  const stamp = Date.now();
  const email = `resale-e2e-${stamp}@example.com`;

  const cust = await request.post('/api/customers', {
    headers: staff,
    data: { name: `Resale E2E ${stamp}`, email, phone: '5550000002', pricing_class: 'contractor' },
  });
  expect(cust.ok(), await cust.text()).toBeTruthy();
  const customerId = (await cust.json()).id;

  const est = await request.post('/api/estimates', {
    headers: staff,
    data: {
      customer_id: customerId,
      label: 'Two doors',
      tax_rate: 0,
      line_items: [
        { description: '16x7 insulated door', quantity: 2, unit_price: 1000 },
        { description: 'Install', quantity: 1, unit_price: 400 },
      ],
    },
  });
  expect(est.ok(), await est.text()).toBeTruthy();
  const estimate = await est.json();
  const sent = await request.post(`/api/estimates/${estimate.id}/mark-sent`, { headers: staff, data: {} });
  expect(sent.ok(), await sent.text()).toBeTruthy();

  const invite = await request.post('/api/portal/invite', { headers: staff, data: { customer_id: customerId, email } });
  expect(invite.ok(), await invite.text()).toBeTruthy();
  const magic = new URL((await invite.json()).magic_link).searchParams.get('token');
  const verify = await request.get(`/portal/verify?token=${encodeURIComponent(magic)}`, {
    headers: { 'x-e2e-test': 'true' },
  });
  expect(verify.ok(), await verify.text()).toBeTruthy();
  return { portalJwt: (await verify.json()).access_token, estimateNumber: estimate.estimate_number };
}

test('a contractor brands, resells an estimate, downloads the PDF and deletes the quote', async ({ page, request }) => {
  const { portalJwt, estimateNumber } = await seed(request);
  await page.addInitScript((jwt) => sessionStorage.setItem('gdx_portal_jwt', jwt), portalJwt);
  await page.goto('/customer-portal');

  // My Quotes before agreeing points at My Branding.
  await page.getByTestId('my-quotes-tab-btn').click();
  await expect(page.getByTestId('my-quotes-locked')).toBeVisible();
  await page.getByTestId('my-quotes-goto-branding').click();

  // Agree, then brand.
  await expect(page.getByTestId('branding-locked-msg')).toBeVisible();
  await page.getByTestId('branding-accept-btn').click();
  await expect(page.getByTestId('branding-disclaimer-accepted')).toBeVisible();
  await page.getByTestId('branding-company-name').fill('Acme Door Co');
  await page.getByTestId('branding-phone').fill('555-0199');
  await page.getByTestId('branding-markup').locator('input').fill('25');
  await page.getByTestId('branding-save-btn').click();
  await expect(page.getByTestId('branding-saved')).toBeVisible();
  await page.getByTestId('branding-logo-input').setInputFiles({ name: 'logo.png', mimeType: 'image/png', buffer: PNG });
  await expect(page.getByTestId('branding-logo-img')).toBeVisible();

  // Resell the estimate: the markup starts at the default.
  await page.getByRole('tab', { name: 'Estimates' }).click();
  // The card shows the label; this customer has only the one estimate.
  await page.getByTestId('estimate-card').filter({ hasText: 'Two doors' }).click();
  await page.getByTestId('estimate-resell-btn').click();
  await expect(page.getByTestId('resell-dialog')).toContainText(`Resell ${estimateNumber}`);
  await expect(page.getByTestId('resell-markup').locator('input')).toHaveValue('25 %');
  await page.getByTestId('resell-customer-name').fill('Pat Example');
  await page.getByTestId('resell-submit-btn').click();
  const done = page.getByTestId('resell-done');
  await expect(done).toContainText('Q-0001');
  // 2 x 1000 + 400 = 2400 to us; at 25% their price is 3000.
  await expect(done).toContainText('$3,000.00');
  await expect(done).toContainText('$2,400.00');

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('resell-download-btn').click(),
  ]);
  expect(download.suggestedFilename()).toBe('quote-Q-0001.pdf');
  const pdf = fs.readFileSync(await download.path());
  expect(pdf.subarray(0, 5).toString()).toBe('%PDF-');

  // The quote is on My Quotes, and deleting it asks first.
  await page.getByTestId('resell-goto-quotes').click();
  const card = page.locator('[data-testid^="my-quote-"]').filter({ hasText: 'Q-0001' }).first();
  await expect(card).toContainText('Pat Example');
  await expect(card).toContainText('$3,000.00');
  // One confirm, not one per mounted tab: "Keep it" must leave nothing behind.
  await card.getByRole('button', { name: 'Delete' }).click();
  const confirm = page.getByRole('alertdialog');
  await expect(confirm).toHaveCount(1);
  await expect(confirm).toContainText('Delete Q-0001?');
  await confirm.getByRole('button', { name: 'Keep it' }).click();
  await expect(confirm).toHaveCount(0);
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: 'Delete' }).click();
  await expect(confirm).toHaveCount(1);
  await confirm.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(page.getByTestId('my-quotes-empty')).toBeVisible();
});
