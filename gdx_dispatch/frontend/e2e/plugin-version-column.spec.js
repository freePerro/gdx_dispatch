import { expect, request as pwRequest, test } from '@playwright/test';

/**
 * The Plugins admin screen shows which version of each plugin is actually
 * running. The catalog gained `version`/`distribution` (published by
 * plugin-host from live discovery); this proves the column renders them to a
 * real owner in a real browser, in both themes — a unit test cannot see a
 * column that never made it into the DOM.
 */

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
  }, { t: access_token, tid: TENANT });
  await api.dispose();
}

test('the Running now table shows a Version column with the live version', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  await page.goto('/admin/plugins');

  await expect(page.getByText('Running now')).toBeVisible({ timeout: 20000 });

  // The column header exists...
  const table = page.locator('table').last();
  await expect(table.getByRole('columnheader', { name: 'Version' })).toBeVisible();

  // ...and the VERSION CELL specifically carries the version the API reports.
  // Asserting on the whole row would pass on a version-shaped string in any
  // other column, and never checks it is the right number.
  const headers = await table.locator('thead th').allInnerTexts();
  const versionIdx = headers.findIndex((h) => h.trim() === 'Version');
  expect(versionIdx).toBeGreaterThan(-1);

  const firstRow = table.locator('tbody tr').first();
  const expected = await page.evaluate(async () => {
    const res = await fetch('/api/plugins', {
      headers: {
        authorization: 'Bearer ' + sessionStorage.getItem('gdx_access_token'),
        'x-tenant-id': sessionStorage.getItem('gdx_tenant_slug'),
      },
    });
    const rows = await res.json();
    return rows.length ? rows[0].version : null;
  });
  expect(expected).toMatch(/\d+\.\d+/);
  await expect(firstRow.locator('td').nth(versionIdx)).toHaveText(expected, { timeout: 20000 });

  await firstRow.scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/plugin-version-light.png', fullPage: true });
});

test('the Version column is readable in dark mode', async ({ page, baseURL }) => {
  await signIn(page, baseURL);
  await page.addInitScript(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto('/admin/plugins');

  await expect(page.getByText('Running now')).toBeVisible({ timeout: 20000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');

  const table = page.locator('table').last();
  await expect(table.getByRole('columnheader', { name: 'Version' })).toBeVisible();
  await expect(table.locator('tbody tr').first()).toContainText(/\d+\.\d+/, { timeout: 20000 });

  // Contrast guard for the muted "unknown" fallback. Rendered on purpose rather
  // than skipped when absent: every local plugin reports a version, so the
  // fallback would never be exercised and the check would quietly no-op — which
  // is how an undefined theme token stays invisible.
  const muted = await page.evaluate(() => {
    const cell = document.querySelector('table tbody td');
    if (!cell) return null;
    const probe = document.createElement('span');
    probe.className = 'muted';
    probe.textContent = 'unknown';
    cell.appendChild(probe);
    const c = getComputedStyle(probe).color;
    // The surface actually behind the cell, not document.body (usually
    // transparent, which would make any comparison pass).
    let el = cell, bg = 'rgba(0, 0, 0, 0)';
    while (el && bg === 'rgba(0, 0, 0, 0)') { bg = getComputedStyle(el).backgroundColor; el = el.parentElement; }
    probe.remove();
    return { color: c, bg, resolved: c !== '' };
  });
  expect(muted).not.toBeNull();
  expect(muted.resolved).toBe(true);
  expect(muted.color).not.toBe(muted.bg);

  await table.locator('tbody tr').first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/plugin-version-dark.png', fullPage: true });
});
