// GDXA-1762 — the folder rail's unread badge fills with --p-primary-color.
// White text on it read 1.92:1 on dark emerald; it now takes
// --text-on-p-primary. The badge sits in two places that resolve the fill
// differently: a system folder row is a <button> (light mode darkens the fill
// to --link-color there) and a custom folder is a Tree node (plain emerald).
// The folder list is stubbed so the rail renders without a connected mailbox.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

const FOLDERS = [
  { graph_folder_id: 'f-inbox', display_name: 'Inbox', well_known_name: 'inbox', is_system: true, pinned: false, parent_folder_id: null, unread_count: 7, total_count: 20 },
  { graph_folder_id: 'f-custom', display_name: 'Vendors', well_known_name: null, is_system: false, pinned: false, parent_folder_id: null, unread_count: 3, total_count: 9 },
];

async function primeSession(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await api.dispose();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.t);
    sessionStorage.setItem('gdx_tenant_slug', a.tid);
    localStorage.setItem('gdx_theme', a.theme);
  }, { t: access_token, tid: TENANT, theme });
  await page.route('**/api/outlook/folders', (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({ json: FOLDERS })
      : route.continue(),
  );
}

function parseRgb(s) {
  const m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(s);
  if (!m) throw new Error(`not an rgb colour: ${s}`);
  return m.slice(1, 4).map(Number);
}

function luminance([r, g, b]) {
  const lin = (c) => { const v = c / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

for (const theme of ['light', 'dark']) {
  test(`unread badges read at AA contrast, ${theme}`, async ({ page, baseURL }) => {
    await primeSession(page, baseURL, theme);
    await page.goto('/inbox');
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);

    const badges = page.locator('[data-test="folder-rail"] .unread-badge');
    await expect(badges).toHaveCount(2, { timeout: 20000 });

    for (const [i, label] of [[0, 'system folder (button)'], [1, 'custom folder (tree)']]) {
      const { color, background } = await badges.nth(i).evaluate((el) => {
        const cs = getComputedStyle(el);
        return { color: cs.color, background: cs.backgroundColor };
      });
      const ratio = contrast(parseRgb(color), parseRgb(background));
      console.log(`${theme} ${label}: ${color} on ${background} = ${ratio.toFixed(2)}:1`);
      expect(ratio, `${label} in ${theme}`).toBeGreaterThanOrEqual(4.5);
    }
    await page.locator('[data-test="folder-rail"]').screenshot({ path: `${SHOTS}/inbox-unread-badge-${theme}.png` });
  });
}
