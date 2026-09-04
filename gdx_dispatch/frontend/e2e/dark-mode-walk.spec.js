/**
 * Dark-mode visual walk — screenshots every view PR #211 touched, so a human
 * can look at them.
 *
 * The companion spec (dark-mode-contrast.spec.js) ASSERTS. This one only
 * LOOKS. Both are necessary: the SMS thread card had perfect contrast and was
 * still obviously wrong, because --p-primary-color is Aura emerald and the
 * rest of the UI is blue. No ratio catches that.
 *
 * Not a visual-regression suite — there is no baseline. It renders and
 * captures; judging is the reviewer's job.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

let cachedToken = null;
async function login(baseURL) {
  if (cachedToken) return cachedToken;
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login should succeed').toBeTruthy();
  cachedToken = (await r.json()).access_token;
  await api.dispose();
  return cachedToken;
}

// route, label, optional settle selector
const ROUTES = [
  ['/dispatch', 'dispatch', '.tech-timeline-body, .dispatch-view'],
  ['/jobs', 'jobs', '.p-datatable, .jobs-view'],
  ['/budget', 'budget', '.kpi, .kpi-row'],
  ['/ai-assistant', 'ai-assistant', null],
  ['/quickbooks', 'quickbooks', null],
  ['/billing', 'billing', '.p-datatable'],
  // was ['/proposals', ...] — that page was retired in migration 061 and the
  // route now redirects here, so the walk was screenshotting /estimates under
  // the label "proposals". Repointed rather than dropped: /estimates was not
  // otherwise covered by this walk.
  ['/estimates', 'estimates', '.p-datatable'],
  ['/reports', 'reports', null],
  ['/catalog', 'catalog', null],
  ['/documents', 'documents', null],
  ['/vendor-statements', 'vendor-statements', null],
  ['/vendor-bills', 'vendor-bills', null],
  ['/admin/games', 'games', null],
  ['/estimates/new', 'estimate-new', null],
  ['/pdf-templates', 'pdf-templates', null],
  ['/mobile', 'mobile-today', null],
];

for (const [route, label, settle] of ROUTES) {
  test(`walk ${label} in dark mode`, async ({ page, baseURL }) => {
    const token = await login(baseURL);
    await page.addInitScript(
      (a) => {
        sessionStorage.setItem('gdx_access_token', a.t);
        localStorage.setItem('gdx_theme', 'dark');
      },
      { t: token, tid: TENANT },
    );

    const consoleErrors = [];
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });

    await page.goto(route, { waitUntil: 'domcontentloaded' });
    if (settle) await page.locator(settle).first().waitFor({ timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2500);

    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
    await page.screenshot({ path: `test-results/walk-${label}-dark.png`, fullPage: false });

    // Surfaced, not asserted — a noisy view is worth knowing about while looking.
    if (consoleErrors.length) console.log(`[${label}] console errors: ${consoleErrors.slice(0, 3).join(' | ')}`);
  });
}
