/**
 * Bank Feeds — the SimpleFIN "Re-link in Settings" button reaches a page that
 * can actually finish the re-link.
 *
 * The button used to push '/settings/integrations', which is not in the route
 * table (only '/settings' and the deeper '/settings/integrations/outlook'), so
 * the one control that repairs a stale bank feed landed on NotFoundView. A unit
 * test cannot prove the repair, because two things only a browser settles:
 *
 *  1. the destination honours the query it is handed. SettingsView picks its
 *     tab from `window.location.search`, read once during setup() — so whether
 *     a CLIENT-SIDE router.push has already updated window.location by then is
 *     a vue-router ordering question, not something jsdom answers. If it
 *     hasn't, the user silently lands on Branding and the flow is still dead.
 *     (This is the #657 class: a view ignoring the query it was given.)
 *  2. the setup-token field is actually on screen in the unhealthy state. The
 *     reconnect banner says "paste a fresh setup token below"; the input used
 *     to render only in the disconnected branch, so the promised field was
 *     missing in exactly the state this button exists for.
 *
 * Requires a SimpleFIN institution whose connection is NOT healthy — that is
 * the only state in which the button renders.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

// ONE login for the whole file. Logging in per test raced the login rate
// limiter and intermittently left the SPA unauthenticated, which shows up as
// the settings view simply never rendering — a flake that says nothing about
// the code under test. Note the limiter is per-container and shared, so
// running this back-to-back with other specs that each log in can still trip
// it; that is the repo's known cross-file flake class. Re-run in isolation
// before blaming this file.
let sharedToken = null;
let sharedApi = null;

test.beforeAll(async ({ playwright }) => {
  void playwright;
  sharedApi = await pwRequest.newContext({ baseURL: process.env.E2E_BASE_URL });
  const r = await sharedApi.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  sharedToken = (await r.json()).access_token;
});

test.afterAll(async () => {
  await sharedApi?.dispose();
});

async function signIn(page) {
  await page.addInitScript((a) => {
    sessionStorage.setItem('gdx_access_token', a.t);
  }, { t: sharedToken, tid: TENANT });
  return { api: sharedApi, token: sharedToken };
}

test('the re-link button lands on Settings → Integrations, ready to re-link', async ({ page }) => {
  const { api, token } = await signIn(page);

  // Precondition, stated out loud: the button only renders for a SimpleFIN
  // institution that is not healthy. Skipping is visibly a skip; silently
  // passing against a healthy feed would be a green that proves nothing.
  const statusRes = await api.get('/api/bank-feeds/status', {
    headers: { authorization: `Bearer ${token}` },
  });
  expect(statusRes.ok()).toBeTruthy();
  const status = await statusRes.json();
  const unhealthy = (status.institutions || []).filter(
    (i) => i.provider === 'simplefin' && (!i.connected || i.auth_state !== 'healthy'),
  );
  test.skip(
    unhealthy.length === 0,
    'needs a SimpleFIN institution with auth_state != healthy; set one with: '
    + "UPDATE banno_connections SET auth_state='needs_reconnect'",
  );

  await page.goto('/bank-feeds');
  const relink = page.locator('[data-testid^="bank-simplefin-relink-"]').first();
  await expect(relink).toBeVisible({ timeout: 20_000 });

  await relink.click();

  // 1. It goes somewhere real, carrying the tab query.
  await expect(page).toHaveURL(/\/settings\?tab=integrations$/);

  // 2. NotFoundView is not what rendered. Assert on a positive marker of the
  //    settings screen rather than the absence of a 404 string.
  await expect(page.locator('[data-testid="settings-tabs"]')).toBeVisible({ timeout: 20_000 });

  // 3. The INTEGRATIONS tab is the selected one — not Branding, the default.
  //    This is the assertion that fails if window.location.search was not yet
  //    updated when SettingsView's setup() ran.
  const activeTab = page.locator('[data-testid="settings-tabs"] [role="tab"][aria-selected="true"]');
  await expect(activeTab).toHaveText(/Integrations/i);

  // 4. The SimpleFIN card is on screen and offers the setup-token field, so the
  //    re-link can actually be completed from here. The token field is the
  //    universal assertion — it renders for every non-healthy state.
  await expect(page.locator('[data-testid="simplefin-card"]')).toBeVisible();
  await expect(page.locator('[data-testid="sfin-token-input"]')).toBeVisible();

  //    The reconnect banner explains a connection that still exists, so it is
  //    asserted only when there is one. Asserting it unconditionally would go
  //    red against a soft-disconnected feed (auth_state 'disconnected' reports
  //    connected:false), which is an ordinary state, not a regression.
  if (unhealthy.some((i) => i.connected)) {
    await expect(page.locator('[data-testid="sfin-reconnect-banner"]')).toBeVisible();
  }

  // 5. The submit control is present and enabled once a token is typed —
  //    proving the form is live, not decoration. No token is submitted: a real
  //    setup token is single-use and would burn a live bridge claim.
  const connectBtn = page.locator('[data-testid="sfin-connect-btn"]');
  await expect(connectBtn).toBeVisible();
  await expect(connectBtn).toBeDisabled();
  await page.locator('[data-testid="sfin-token-input"] textarea, textarea[data-testid="sfin-token-input"]')
    .first().fill('not-a-real-token');
  await expect(connectBtn).toBeEnabled();

});

test('without the tab query /settings opens Branding', async ({ page }) => {
  // Pins what the tab assertions above are discriminating against. If this ever
  // also reported "Integrations", those assertions would be vacuous and the
  // dead-link regression could come back green. (An *unknown* tab is a
  // different case: SettingsView validates against SETTINGS_TABS and falls back
  // to this same default, because PrimeVue would otherwise select no panel at
  // all and render a blank body.)
  await signIn(page);
  await page.goto('/settings');
  await expect(page.locator('[data-testid="settings-tabs"]')).toBeVisible({ timeout: 20_000 });
  const activeTab = page.locator('[data-testid="settings-tabs"] [role="tab"][aria-selected="true"]');
  await expect(activeTab).toHaveText(/Branding/i);
  // PrimeVue keeps every TabPanel mounted, so the card exists in the DOM on any
  // tab — visibility, not presence, is what tells the tabs apart. That is why
  // the assertions above check toBeVisible().
  await expect(page.locator('[data-testid="simplefin-card"]')).toBeHidden();
});

test('an unknown ?tab= falls back to the default instead of a blank body', async ({ page }) => {
  // A stale bookmark or an old emailed link carries a tab that no longer
  // exists. PrimeVue Tabs selects on an exact value match, so without the
  // SETTINGS_TABS allowlist this renders every panel hidden — a blank page
  // with no error and no way forward.
  await signIn(page);
  await page.goto('/settings?tab=no_such_tab_exists');
  await expect(page.locator('[data-testid="settings-tabs"]')).toBeVisible({ timeout: 20_000 });
  const activeTab = page.locator('[data-testid="settings-tabs"] [role="tab"][aria-selected="true"]');
  await expect(activeTab).toHaveCount(1);
  await expect(activeTab).toHaveText(/Branding/i);
});

test('deep link straight to /settings?tab=integrations opens Integrations', async ({ page }) => {
  // The full-page-load path, for contrast with the client-side push above.
  // Both must work: one is the button, the other is a bookmark or an email link.
  await signIn(page);
  await page.goto('/settings?tab=integrations');
  await expect(page.locator('[data-testid="settings-tabs"]')).toBeVisible({ timeout: 20_000 });
  const activeTab = page.locator('[data-testid="settings-tabs"] [role="tab"][aria-selected="true"]');
  await expect(activeTab).toHaveText(/Integrations/i);
  await expect(page.locator('[data-testid="simplefin-card"]')).toBeVisible();
});
