/**
 * Segments page — the browser walk for #455 (PR #675).
 *
 * Unit tests cannot see any of this:
 *   - create/edit really reaching a database (create used to 422, and behind
 *     that `segments.deleted_at` was NOT NULL so the INSERT raised anyway);
 *   - `useDestructiveConfirm` — it auto-accepts under vitest because no
 *     ConfirmationService is registered, so the confirm dialog is only ever
 *     proven in a real browser;
 *   - the rule-row media query — jsdom applies no media queries;
 *   - dark mode.
 *
 * Runs against a throwaway container serving the working tree, seeded with 63
 * customers (more than the old page_size=50 cap) of which 21 have no job in
 * 180 days.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;
const EMAIL = ENV.E2E_EMAIL;
const PASSWORD = ENV.E2E_PASSWORD;

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });
  await api.dispose();
}

async function gotoSegments(page) {
  await page.goto('/segments');
  await expect(page.getByTestId('segments-open-dialog')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('tbody tr').first()).toBeVisible({ timeout: 20000 });
}

test.beforeEach(async ({ page, baseURL }) => {
  await signIn(page, baseURL);
});

test('the list renders real criteria and real customer counts', async ({ page }) => {
  await gotoSegments(page);
  const atRisk = page.locator('tr', { hasText: 'At Risk' }).first();
  await expect(atRisk).toContainText('Last job date is older than 180 days');
  // 21 of the 63 seeded customers have no job inside 180 days.
  await expect(atRisk).toContainText('21');
  await expect(atRisk).toContainText('Built-in');
});

test('a built-in offers no edit or delete control', async ({ page }) => {
  await gotoSegments(page);
  const atRisk = page.locator('tr', { hasText: 'At Risk' }).first();
  await expect(atRisk.getByTestId('segments-edit-row')).toHaveCount(0);
  await expect(atRisk.getByTestId('segments-delete-row')).toHaveCount(0);
  // clicking the row must not open the editor either
  await atRisk.click();
  await expect(page.getByTestId('segments-dialog-name')).toHaveCount(0);
});

test('Save is blocked, with a reason, for a blank and a zero rule value', async ({ page }) => {
  await gotoSegments(page);
  await page.getByTestId('segments-open-dialog').click();
  await page.getByTestId('segments-dialog-name').fill('Should not save');

  await page.getByTestId('segments-rule-value-0').fill('');
  await expect(page.getByTestId('segments-dialog-invalid')).toBeVisible();
  await expect(page.getByTestId('segments-dialog-save')).toBeDisabled();

  await page.getByTestId('segments-rule-value-0').fill('0');
  await expect(page.getByTestId('segments-dialog-invalid')).toContainText('at least 1 day');
  await expect(page.getByTestId('segments-dialog-save')).toBeDisabled();

  // ...and a real value clears it
  await page.getByTestId('segments-rule-value-0').fill('120');
  await expect(page.getByTestId('segments-dialog-invalid')).toHaveCount(0);
  await expect(page.getByTestId('segments-dialog-save')).toBeEnabled();
});

test('a segment can be CREATED end to end and survives a reload', async ({ page }) => {
  await gotoSegments(page);
  const name = `Walk Created ${Date.now()}`;

  await page.getByTestId('segments-open-dialog').click();
  await page.getByTestId('segments-dialog-name').fill(name);
  await page.getByTestId('segments-rule-value-0').fill('200');
  await page.getByTestId('segments-dialog-save').click();

  const row = page.locator('tr', { hasText: name }).first();
  await expect(row).toBeVisible({ timeout: 15000 });
  await expect(row).toContainText('Last job date is older than 200 days');
  await expect(row).toContainText('Custom');
  // 21 customers have no job in 200 days either — a real, non-zero count.
  await expect(row).toContainText('21');

  await page.reload();
  await expect(page.locator('tr', { hasText: name }).first()).toBeVisible({ timeout: 20000 });
});

test('a custom segment can be EDITED and the edit persists', async ({ page }) => {
  await gotoSegments(page);
  // Seeds its own subject: editing the fixture segment would rename it, so a
  // second run of this file would find nothing to edit.
  const before = `Walk Editable ${Date.now()}`;
  const after = `${before} renamed`;
  await page.getByTestId('segments-open-dialog').click();
  await page.getByTestId('segments-dialog-name').fill(before);
  await page.getByTestId('segments-rule-value-0').fill('90');
  await page.getByTestId('segments-dialog-save').click();
  await expect(page.locator('tr', { hasText: before }).first()).toBeVisible({ timeout: 15000 });

  await page.locator('tr', { hasText: before }).first().getByTestId('segments-edit-row').click();
  // the stored "90 days" round-trips into the editor as the number 90
  await expect(page.getByTestId('segments-rule-value-0')).toHaveValue('90');
  await page.getByTestId('segments-dialog-name').fill(after);
  await page.getByTestId('segments-rule-value-0').fill('300');
  await page.getByTestId('segments-dialog-save').click();

  await expect(page.locator('tr', { hasText: after }).first())
    .toContainText('Last job date is older than 300 days', { timeout: 15000 });

  await page.reload();
  await expect(page.locator('tr', { hasText: after }).first()).toBeVisible({ timeout: 20000 });
  // the old name is gone — this was an UPDATE, not an INSERT
  await expect(page.locator('tr', { hasText: new RegExp(`${before}$`) })).toHaveCount(0);
});

test('DELETE really asks, and Cancel does not delete', async ({ page }) => {
  await gotoSegments(page);
  const name = `Walk Deletable ${Date.now()}`;
  await page.getByTestId('segments-open-dialog').click();
  await page.getByTestId('segments-dialog-name').fill(name);
  await page.getByTestId('segments-rule-value-0').fill('150');
  await page.getByTestId('segments-dialog-save').click();
  await expect(page.locator('tr', { hasText: name }).first()).toBeVisible({ timeout: 15000 });

  // 1. the confirm dialog actually appears (vitest never sees this)
  await page.locator('tr', { hasText: name }).first().getByTestId('segments-delete-row').click();
  const dialog = page.locator('.p-confirmdialog');
  await expect(dialog).toBeVisible({ timeout: 10000 });
  await expect(dialog).toContainText(name);

  // 2. Cancel must NOT delete
  await dialog.getByRole('button', { name: /cancel/i }).click();
  await expect(dialog).toBeHidden();
  await page.reload();
  await expect(page.locator('tr', { hasText: name }).first()).toBeVisible({ timeout: 20000 });

  // 3. confirming does delete, and it stays deleted
  await page.locator('tr', { hasText: name }).first().getByTestId('segments-delete-row').click();
  await expect(dialog).toBeVisible({ timeout: 10000 });
  await dialog.getByRole('button', { name: /^delete$/i }).click();
  await expect(page.locator('tr', { hasText: name })).toHaveCount(0, { timeout: 15000 });
  await page.reload();
  await expect(page.getByTestId('segments-open-dialog')).toBeVisible({ timeout: 20000 });
  await expect(page.locator('tr', { hasText: name })).toHaveCount(0);
});

test('All customers counts the whole table, not one page of 50', async ({ page }) => {
  await gotoSegments(page);
  const allChip = page.getByTestId('segment-chip-all');
  // 63 seeded. The old ?page_size= was dropped by FastAPI, capping this at 50.
  await expect(allChip).toContainText('63');
  await expect(allChip).not.toContainText('(50)');
});

test('a segment chip really filters the customers panel', async ({ page }) => {
  await gotoSegments(page);
  const requests = [];
  page.on('request', (r) => requests.push(r.url()));

  await page.getByTestId('segment-chip-segment-at-risk').click();
  await expect(page.getByTestId('segments-customers-panel')).toBeVisible();

  await expect
    .poll(() => requests.some((u) => u.includes('/api/segments/at-risk/customers')))
    .toBe(true);
  expect(requests.some((u) => u.includes('segment_id=')), 'no dead segment_id param').toBe(false);
});

test('rule rows stack at a phone width (jsdom applies no media queries)', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await gotoSegments(page);
  await page.getByTestId('segments-open-dialog').click();
  await page.getByTestId('segments-rule-add').click();

  const row = page.getByTestId('segments-rule-0');
  await expect(row).toBeVisible();
  const cols = await row.evaluate((el) => getComputedStyle(el).gridTemplateColumns);
  // one column at <=640px, four above it
  expect(cols.split(' ').length).toBe(1);

  // and nothing overflows the phone viewport
  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

test('the rule selects are not truncated at desktop width', async ({ page }) => {
  await gotoSegments(page);
  await page.getByTestId('segments-open-dialog').click();
  await expect(page.getByTestId('segments-dialog-name')).toBeVisible();
  await page.waitForTimeout(600); // let the dialog transition settle

  // At the old 520px dialog / 1.2fr columns these rendered as
  // "Last job ..." and "is older t...". scrollWidth > clientWidth is how
  // text-overflow:ellipsis shows up to a test.
  for (const id of ['segments-rule-field-0', 'segments-rule-operator-0']) {
    const label = page.getByTestId(id).locator('.p-select-label');
    const clipped = await label.evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(clipped, `${id} is clipped by ${clipped}px`).toBeLessThanOrEqual(1);
    await expect(label).not.toContainText('\u2026');
  }
});

test('the page is legible in dark mode', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));
  await gotoSegments(page);
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.getByTestId('segments-open-dialog').click();
  await expect(page.getByTestId('segments-dialog-name')).toBeVisible();
  await page.screenshot({ path: 'e2e-artifacts/segments-dark.png', fullPage: true });

  // the rule hint must not be painted the same colour as what is behind it
  const hint = page.locator('.rule-hint').first();
  if (await hint.count()) {
    const [fg, bg] = await hint.evaluate((el) => {
      const s = getComputedStyle(el);
      let node = el;
      let bgc = 'rgba(0, 0, 0, 0)';
      while (node && bgc === 'rgba(0, 0, 0, 0)') {
        bgc = getComputedStyle(node).backgroundColor;
        node = node.parentElement;
      }
      return [s.color, bgc];
    });
    expect(fg).not.toBe(bg);
  }
});
