/**
 * Office actions land in the audit trail, naming who did them (#700).
 *
 * A handler that committed its change and THEN wrote the audit row lost the
 * row: `get_db()` closes the session without committing. On prod that left 8
 * holding areas and 49 job assignments with no trail, and nothing errored.
 *
 * This walks the office path a dispatcher really takes — Dispatch → Add Area →
 * delete it; a job → add a tech → remove them — then opens Activity and checks
 * every one of those actions is listed under the admin who did it. The feed
 * API is read too, for the exact user id the table only shows as a name.
 *
 * Each run makes its own area (timestamped name) and removes its own
 * assignment, so the file is re-runnable against the same database.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;
const CLOSED = new Set(['completed', 'closed', 'cancelled', 'canceled', 'invoiced', 'paid']);

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: ENV.E2E_EMAIL, password: ENV.E2E_PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token: token } = await r.json();
  const headers = { 'x-tenant-id': TENANT, authorization: `Bearer ${token}` };
  await page.addInitScript((t) => sessionStorage.setItem('gdx_access_token', t), token);
  const me = await (await api.get('/api/users/me', { headers })).json();
  return { api, headers, me };
}

async function feed(api, headers, entityType) {
  const r = await api.get(`/api/activity/recent?entity_type=${entityType}&limit=100`, { headers });
  expect(r.ok(), `activity feed for ${entityType}`).toBeTruthy();
  return (await r.json()).items;
}

test('holding-area and job-assignment changes appear in Activity under the admin', async ({ page, baseURL }) => {
  const { api, headers, me } = await signIn(page, baseURL);
  const started = new Date(Date.now() - 5000).toISOString();
  const areaName = `Walk 700 ${Date.now()}`;

  // ── Dispatch: add a holding area, then delete it ──────────────────────────
  // "Add Area" renders only once a parking area exists; seed one on a bare DB.
  const existing = await (await api.get('/api/holding-areas', { headers })).json();
  if (!existing.some((a) => !/ready to schedule/i.test(a.name))) {
    const seeded = await api.post('/api/holding-areas', { headers, data: { name: 'Needs Parts' } });
    expect(seeded.ok(), 'seed a parking area').toBeTruthy();
  }
  await page.goto('/dispatch');
  await page.getByTestId('add-holding-area').click();
  await page.getByTestId('new-area-name').fill(areaName);
  await page.getByTestId('add-holding-area-submit').click();
  const card = page.locator('.holding-area-col', { hasText: areaName });
  await expect(card).toBeVisible({ timeout: 15000 });
  await card.getByRole('button', { name: 'Delete' }).click();
  await expect(card).toHaveCount(0, { timeout: 15000 });

  // ── A job: add another tech, then remove them ─────────────────────────────
  const listed = await (await api.get('/api/jobs?limit=50', { headers })).json();
  const job = (Array.isArray(listed) ? listed : listed.items || []).find(
    (j) => !CLOSED.has(String(j.status || '').toLowerCase()),
  );
  expect(job, 'an open job to assign on').toBeTruthy();
  await page.goto(`/jobs/${job.id}`);
  const removeButtons = page.locator('[data-testid^="assignment-remove-"]');
  await expect(page.getByTestId('assignment-add-select')).toBeVisible({ timeout: 15000 });
  const before = await removeButtons.count();
  await page.getByTestId('assignment-add-select').click();
  await page.getByRole('option').first().click();
  await page.getByTestId('assignment-add-btn').click();
  await expect(removeButtons).toHaveCount(before + 1, { timeout: 15000 });
  await removeButtons.last().click();
  await expect(removeButtons).toHaveCount(before, { timeout: 15000 });

  // ── The feed: every action, this run, attributed to the admin ─────────────
  const mine = (items) => items.filter((i) => i.created_at >= started);
  const areas = mine(await feed(api, headers, 'holding_area'));
  const assigns = mine(await feed(api, headers, 'job_assignment'));
  expect(areas.map((i) => i.action).sort()).toEqual(['create', 'delete']);
  expect(assigns.map((i) => i.action).sort()).toEqual(['assign', 'unassign']);
  for (const row of [...areas, ...assigns]) {
    expect(row.user_id, `${row.entity_type} ${row.action} names the admin`).toBe(String(me.id));
  }

  // ── And the Activity page shows them, under a person, not "system" ────────
  await page.goto('/activity');
  const table = page.locator('.p-datatable');
  await expect(table).toBeVisible({ timeout: 15000 });
  for (const entity of ['holding_area', 'job_assignment']) {
    const rows = table.locator('tbody tr', { has: page.locator('.p-tag', { hasText: entity }) });
    await expect(rows.first(), `${entity} rows listed`).toBeVisible({ timeout: 15000 });
    const firstRow = await rows.first().innerText();
    expect(firstRow.toLowerCase(), `${entity} row names a user`).not.toContain('system');
  }
  await page.screenshot({ path: 'test-results/audit-700-activity-light.png', fullPage: true });
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.reload();
  await expect(table).toBeVisible({ timeout: 15000 });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await page.screenshot({ path: 'test-results/audit-700-activity-dark.png', fullPage: true });

  await api.dispose();
});
