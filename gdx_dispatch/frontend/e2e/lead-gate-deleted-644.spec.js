/**
 * The lead-tech completion toggles are gone from the admin form (#644).
 *
 * Two catalog keys spelled one idea in opposite orders:
 * `tech_mobile.completion_lead_tech_only` was read by exactly one gate, on the
 * deprecated POST /api/mobile/jobs/{id}/complete that no shipped client calls;
 * `tech_mobile.lead_tech_only_completion` was never read by anything at all.
 * Either way the office saw a switch labelled "restrict completion to the lead
 * tech" that could not do what it said. Both were deleted rather than repaired.
 *
 * This walks what the office actually sees: the tech-mobile settings form, in
 * light and dark, confirming neither toggle is offered any more and that the
 * rest of the Phase 1.4 tab still saves. The running regression guard is the
 * pytest absence assertion in tests/test_feature_defaults.py — CI does not run
 * Playwright — so this file is the reproducible manual walk, not the net.
 *
 * Re-runnable: it flips one bool and flips it back.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;
const ROUTE = '/admin/feature-settings/tech-mobile';
const GONE = ['tech_mobile.completion_lead_tech_only', 'tech_mobile.lead_tech_only_completion'];
// Probe a setting something actually READS (mobile.py resolves this one), so
// the write-path check is not itself demonstrating a switch nothing acts on.
const PROBE = 'tech_mobile.techs_see_all_jobs';
const OTHER = 'tech_mobile.multi_tech_jobs';

async function signIn(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: ENV.E2E_EMAIL, password: ENV.E2E_PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token: token } = await r.json();
  await page.addInitScript(
    (a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    },
    { t: token, tid: TENANT },
  );
  return { api, headers: { 'x-tenant-id': TENANT, authorization: `Bearer ${token}` } };
}

test('the office is offered no lead-tech completion toggle, in either theme', async ({
  page,
  baseURL,
}) => {
  // `/api/session-policy` 500s against the shared LOCAL dev database, whose
  // schema has drifted from the ORM — it answers 401 on prod, and nothing in
  // this change goes near it.
  //
  // Its console line is the generic "Failed to load resource ... 500", which
  // carries no URL, so a text filter for it necessarily swallows EVERY 500 on
  // the page. The responses are therefore the real gate: any 5xx that is not
  // session-policy fails the walk regardless of what the console says.
  const IGNORED_URL = /\/api\/session-policy/;
  const errors = [];
  const badResponses = [];
  page.on('response', (r) => {
    if (r.status() >= 500 && !IGNORED_URL.test(r.url())) {
      badResponses.push(`${r.status()} ${r.url()}`);
    }
  });
  page.on('console', (m) => {
    const t = m.text();
    if (m.type() !== 'error') return;
    if (IGNORED_URL.test(t) || /Failed to load resource.*500/.test(t)) return;
    errors.push(t);
  });
  page.on('pageerror', (e) => errors.push(String(e)));

  const { api, headers } = await signIn(page, baseURL);

  // The API is the source the form renders from — check it first so a UI
  // assertion can never pass on a stale bundle.
  const cat = await api.get('/api/admin/feature-settings/tech-mobile', { headers });
  expect(cat.ok(), 'catalog GET').toBeTruthy();
  const keys = (await cat.json()).catalog.map((e) => e.key);
  for (const k of GONE) expect(keys, `${k} still served`).not.toContain(k);
  expect(keys.filter((k) => k.toLowerCase().includes('lead'))).toEqual([]);

  await page.goto(ROUTE);
  await expect(page.locator('.setting-row').first()).toBeVisible({ timeout: 15000 });

  // Phase 1.4 tab: the two survivors are there, neither dead toggle is.
  await page.getByRole('tab', { name: /1\.4/ }).click();
  const panel = page.locator('.settings-grid').filter({ hasText: OTHER });
  await expect(panel.locator('.setting-row')).toHaveCount(2);
  await expect(page.getByText(PROBE, { exact: true })).toBeVisible();
  for (const k of GONE) {
    await expect(page.getByText(k, { exact: true })).toHaveCount(0);
  }
  // The label, not just the key — the form shows both.
  await expect(page.getByText(/restrict .*completion to lead tech/i)).toHaveCount(0);

  // The rest of the tab still writes. Flip the probe, confirm it persisted
  // server-side, then put it back — in a finally, so a failure between the two
  // does not strand an override in whatever database this ran against.
  const read = async () =>
    (await (await api.get('/api/admin/feature-settings/tech-mobile', { headers })).json())
      .resolved[PROBE];
  const before = await read();
  try {
    const row = page.locator('.setting-row').filter({ hasText: PROBE });
    await row.locator('.p-toggleswitch').click();
    await expect(row.getByText('overridden')).toBeVisible({ timeout: 10000 });
    expect(await read(), 'toggle did not persist').toBe(!before);
  } finally {
    const del = await api.delete(
      `/api/admin/feature-settings/tech-mobile/${encodeURIComponent(PROBE)}`,
      { headers },
    );
    expect(del.ok(), 'revert').toBeTruthy();
  }

  // Dark mode: Doug runs the app dark, and this form is an office surface.
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.goto(ROUTE);
  await page.getByRole('tab', { name: /1\.4/ }).click();
  await expect(page.locator('.setting-row').filter({ hasText: PROBE })).toBeVisible({
    timeout: 15000,
  });
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  for (const k of GONE) await expect(page.getByText(k, { exact: true })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/lead-gate-644-dark.png', fullPage: true });

  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
  await page.goto(ROUTE);
  await page.getByRole('tab', { name: /1\.4/ }).click();
  await expect(page.locator('.setting-row').filter({ hasText: PROBE })).toBeVisible({
    timeout: 15000,
  });
  await page.screenshot({ path: 'test-results/lead-gate-644-light.png', fullPage: true });

  expect(badResponses, `5xx responses: ${badResponses.join(' | ')}`).toEqual([]);
  expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  await api.dispose();
});
