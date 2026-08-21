/**
 * Browser walk for the invoice-line-editor stack (p1-p5), 2026-08-20.
 *
 * Every PR in that stack shipped saying "NOT verified: no browser walk". This
 * is that walk. It runs against a THROWAWAY container serving the working
 * tree's freshly-built dist -- not the baked image -- with the 071/072 columns
 * applied by hand, because create_all does not alter existing tables.
 *
 * What it proves that jsdom cannot: the Category cell actually renders a value
 * (the original complaint was a blank select), the catalog-source pill is
 * visible, Add Labor exists and opens, and the discount reaches the totals.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const SECRET = process.env.E2E_PASSWORD;

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: SECRET },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });
  await api.dispose();
}

test.describe('invoice line editor — p1..p5 on /billing/new', () => {
  test.beforeEach(async ({ page, baseURL }) => {
    await signIn(page, baseURL);
  });

  test('p1: the category cell renders a value, and the line says which catalog it came from', async ({ page }) => {
    await page.goto('/billing/new');
    await expect(page.locator('[data-testid="line-add-catalog-btn"]')).toBeVisible({ timeout: 20000 });

    await page.locator('[data-testid="line-add-catalog-btn"]').click();
    await expect(page.locator('[data-testid="catalog-picker"]')).toBeVisible({ timeout: 15000 });

    // The picker opens on the FIRST catalog tab (a big read-only CHI one on
    // this tenant), so switch to the seeded catalog before looking for its row.
    await page.getByRole('tab', { name: /Hardware/ }).first().click();

    // The seeded item's free-form category is `3" Struts` -- matches none of
    // the six options, which is exactly the row that used to render blank.
    const row = page.locator('[data-testid="catalog-picker-table"] tr', { hasText: '3in Strut' }).first();
    await expect(row).toBeVisible({ timeout: 15000 });
    await row.locator('td').first().click();
    await page.locator('[data-testid="catalog-picker-add"]').click();

    // THE original complaint: this select was empty.
    const cat = page.locator('[data-testid="line-cat-0"]');
    await expect(cat).toBeVisible({ timeout: 15000 });
    const shown = await cat.textContent();
    expect(shown, 'category cell rendered blank -- the p1 bug').toBeTruthy();

    // p1's other half: provenance on the line.
    await expect(page.locator('[data-testid="line-source-0"]')).toBeVisible();
    await expect(page.locator('[data-testid="line-source-0"]')).toContainText('Hardware');
  });

  test('p2: Add Labor is reachable and lists the matrix', async ({ page }) => {
    await page.goto('/billing/new');
    const btn = page.locator('[data-testid="line-add-labor-btn"]');
    await expect(btn, 'Add Labor did not exist at all before p2').toBeVisible({ timeout: 20000 });

    await btn.click();
    await expect(page.locator('[data-testid="labor-picker"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="labor-lane-matrix"]')).toBeVisible();
    await expect(page.locator('[data-testid="labor-table"]')).toContainText('16x7 Sectional Install', { timeout: 15000 });
  });

  test('p4: a discount reaches the totals the operator sees', async ({ page }) => {
    await page.goto('/billing/new');
    const disc = page.locator('[data-testid="invoice-discount"] input').first();
    await expect(disc).toBeVisible({ timeout: 20000 });

    // Give the invoice a line so a discount is meaningful.
    await page.locator('[data-testid="line-desc-0"]').fill('Door');
    const price = page.locator('[data-testid="line-price-0"] input').first();
    await price.fill('1000');
    await price.blur();

    await disc.fill('150');
    await disc.blur();

    await expect(page.locator('[data-testid="invoice-discount-amount"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('[data-testid="invoice-discount-amount"]')).toContainText('150');
  });

  test('p5 + theming: the editor renders in dark mode', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));
    await page.goto('/billing/new');
    await expect(page.locator('[data-testid="line-items-editor"]')).toBeVisible({ timeout: 20000 });
    const theme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    expect(theme, 'dark mode did not apply').toBe('dark');
    await page.screenshot({ path: 'test-results/invoice-editor-dark.png', fullPage: false });
  });
});

test.describe('F14 — the 11-column grid at a real laptop width', () => {
  // Flagged in the plan as unverifiable in jsdom (no media queries, no layout).
  // The dark-mode screenshot showed the header truncating after "INCL. INSTALL",
  // so this measures it rather than eyeballing.
  test('the line row is reachable, not clipped, at 1366px', async ({ page, baseURL }) => {
    await signIn(page, baseURL);
    await page.setViewportSize({ width: 1366, height: 900 });
    await page.goto('/billing/new');
    const editor = page.locator('[data-testid="line-items-editor"]');
    await expect(editor).toBeVisible({ timeout: 20000 });

    const m = await editor.evaluate((el) => {
      const row = el.querySelector('.line-item-row') || el.querySelector('.line-item-header');
      const style = row ? getComputedStyle(row) : null;
      return {
        editorClientW: el.clientWidth,
        editorScrollW: el.scrollWidth,
        overflowX: getComputedStyle(el).overflowX,
        rowScrollW: row ? row.scrollWidth : null,
        rowClientW: row ? row.clientWidth : null,
        tracks: style ? style.gridTemplateColumns : null,
        bodyScrollW: document.body.scrollWidth,
        bodyClientW: document.body.clientWidth,
      };
    });
    console.log('F14 measurements:', JSON.stringify(m, null, 2));

    // The page itself must never scroll horizontally — that is the rule this
    // repo's layout guidance states outright.
    expect(m.bodyScrollW, 'the PAGE scrolls horizontally').toBeLessThanOrEqual(m.bodyClientW + 1);

    await page.screenshot({ path: 'test-results/invoice-editor-1366.png', fullPage: false });
  });
});

test.describe('F14 — no horizontal page scroll at any realistic width', () => {
  // The rule this repo states outright: wide content scrolls inside its own
  // container, the page body never scrolls sideways. Pin it across the widths
  // the office actually uses, not just the one I happened to fix.
  for (const [label, width] of [['13in laptop', 1280], ['common laptop', 1366], ['1080p', 1920]]) {
    test(`${label} (${width}px): page does not scroll sideways`, async ({ page, baseURL }) => {
      await signIn(page, baseURL);
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/billing/new');
      await expect(page.locator('[data-testid="line-items-editor"]')).toBeVisible({ timeout: 20000 });

      const m = await page.locator('[data-testid="line-items-editor"]').evaluate((el) => ({
        editorClientW: el.clientWidth,
        editorScrollW: el.scrollWidth,
        bodyScrollW: document.body.scrollWidth,
        bodyClientW: document.body.clientWidth,
      }));
      expect(m.bodyScrollW, 'the PAGE scrolls horizontally').toBeLessThanOrEqual(m.bodyClientW + 1);
      if (width >= 1366) {
        // At 1366+ the row must FIT, not merely scroll -- the Total column
        // being off-screen is what made this worth fixing.
        expect(m.editorScrollW, `row overflows at ${width}px`).toBeLessThanOrEqual(m.editorClientW + 1);
      }
    });
  }
});
