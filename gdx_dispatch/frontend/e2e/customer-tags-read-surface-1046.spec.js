/**
 * GDXA-1046 — customer tags have a read surface. On the Customers list the
 * office can filter by a tag and see each row's tags; on Segments a rule can
 * select by tag, and the segment's customer table shows the tags it matched.
 *
 * Self-seeding: creates three customers and two tags through the API, walks
 * both pages in the real SPA, and soft-deletes everything it made.
 * Run at a phone width with E2E_VIEWPORT=mobile, dark with E2E_THEME=dark.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';
const THEME = process.env.E2E_THEME || 'light';
const MOBILE = process.env.E2E_VIEWPORT === 'mobile';

test.use({ viewport: MOBILE ? { width: 390, height: 844 } : { width: 1440, height: 900 } });

test('filter customers by tag, see their tags, and build a segment by tag', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token: token } = await login.json();
  const headers = { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT, 'content-type': 'application/json' };

  const stamp = Date.now();
  const post = async (url, data) => {
    const res = await api.post(url, { headers, data });
    expect(res.ok(), `${url} → ${res.status()}`).toBeTruthy();
    return res.json();
  };
  const [alpha, bravo, charlie] = [
    await post('/api/customers', { name: `GDXA-1046 ${stamp} Alpha` }),
    await post('/api/customers', { name: `GDXA-1046 ${stamp} Bravo` }),
    await post('/api/customers', { name: `GDXA-1046 ${stamp} Charlie` }),
  ];
  const vip = await post('/api/tags', { name: `VIP ${stamp}`, color: '#b91c1c' });
  const net30 = await post('/api/tags', { name: `Net 30 ${stamp}`, color: '#fde68a' });
  for (const c of [alpha, bravo]) await post(`/api/customers/${c.id}/tags`, { tag_id: vip.id });
  for (const c of [alpha, charlie]) await post(`/api/customers/${c.id}/tags`, { tag_id: net30.id });
  let segmentId = null;

  try {
    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    }, { t: token, tid: TENANT, theme: THEME });

    // Pick from a PrimeVue Select, then wait for its overlay to leave, so the
    // next Select's options are the only ones on the page.
    const choose = async (testid, name) => {
      await page.locator(`[data-testid="${testid}"]`).click();
      await page.locator('.p-select-overlay').getByRole('option', { name, exact: true }).click();
      await expect(page.locator('.p-select-overlay')).toHaveCount(0);
    };

    // ── Customers: the tag filter narrows the list, the column shows tags ──
    // At phone width /customers redirects to the mobile card list
    // (useViewMode MOBILE_COMPANION_PATHS), which has no tag surface yet.
    // That list is a deferred instance of this class; the desktop table is
    // what this change serves.
    if (MOBILE) {
      await page.goto('/customers');
      await expect(page).toHaveURL(/\/mobile\/customers$/);
    } else {
      await page.goto('/customers');
      await page.locator('[data-testid="customers-search"]').fill(`GDXA-1046 ${stamp}`);
      await expect(page.locator(`[data-testid="customer-link-${charlie.id}"]`)).toBeVisible({ timeout: 15000 });

      const listed = page.waitForResponse((r) => r.url().includes(`tag_id=${vip.id}`));
      await choose('customers-tag-filter', vip.name);
      expect((await listed).status()).toBe(200);

      await expect(page.locator(`[data-testid="customer-link-${alpha.id}"]`)).toBeVisible();
      await expect(page.locator(`[data-testid="customer-link-${bravo.id}"]`)).toBeVisible();
      await expect(page.locator(`[data-testid="customer-link-${charlie.id}"]`)).toHaveCount(0);
      await expect(page.locator(`[data-testid="customer-tags-${alpha.id}"] [data-testid="customer-tag-chip"]`))
        .toHaveText([net30.name, vip.name]);
      await expect(page.locator(`[data-testid="customer-tags-${bravo.id}"] [data-testid="customer-tag-chip"]`))
        .toHaveText([vip.name]);
      await page.screenshot({ path: `${SHOTS}/1046-customers-desktop-${THEME}.png`, fullPage: false });
    }

    // ── Segments: a tag rule, built in the dialog, selects the tagged ──
    await page.goto('/segments');
    await page.locator('[data-testid="segments-open-dialog"]').click();
    await page.locator('[data-testid="segments-dialog-name"]').fill(`Lacks VIP ${stamp}`);
    await choose('segments-rule-field-0', 'Tag');
    await choose('segments-rule-operator-0', 'does not have');
    await choose('segments-rule-tag-0', vip.name);
    // Combine with Net 30 so the audience is exactly this test's Charlie.
    await page.locator('[data-testid="segments-rule-add"]').click();
    await choose('segments-rule-field-1', 'Tag');
    await choose('segments-rule-operator-1', 'has');
    await choose('segments-rule-tag-1', net30.name);
    await expect(page.locator('[data-testid="segments-dialog-invalid"]')).toHaveCount(0);

    const created = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith('/api/segments'));
    await page.locator('[data-testid="segments-dialog-save"]').click();
    const createdRes = await created;
    expect(createdRes.status()).toBe(201);
    segmentId = (await createdRes.json()).id;

    await page.locator(`[data-testid="segment-chip-segment-${segmentId}"]`).click();
    const table = page.locator('[data-testid="segments-customers-table"]');
    await expect(table.getByText(charlie.name)).toBeVisible({ timeout: 15000 });
    await expect(table.getByText(alpha.name)).toHaveCount(0);
    await expect(table.getByText(bravo.name)).toHaveCount(0);
    await expect(table.locator('[data-testid="customer-tag-chip"]')).toHaveText([net30.name]);
    await page.screenshot({ path: `${SHOTS}/1046-segments-${MOBILE ? 'mobile' : 'desktop'}-${THEME}.png`, fullPage: false });
  } finally {
    if (segmentId) await api.delete(`/api/segments/${segmentId}`, { headers });
    for (const t of [vip, net30]) await api.delete(`/api/tags/${t.id}`, { headers });
    for (const c of [alpha, bravo, charlie]) await api.delete(`/api/customers/${c.id}`, { headers });
    await api.dispose();
  }
});
