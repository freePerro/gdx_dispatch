/**
 * Dark-mode contrast — verified in a real browser, not computed.
 *
 * The 2026-07-27 sweep (PR #211) measured every fix against the stylesheet
 * PrimeVue *emits*. That is one step removed from what a browser actually
 * paints: it never proved the resolver matches `getComputedStyle`. This spec
 * closes that gap — it renders the two views Doug reported, reads the real
 * computed colours off the real elements in both themes, and asserts WCAG AA.
 *
 * It also screenshots both modes so the result can be looked at, not just
 * asserted — a green number on an ugly panel is still a bad panel.
 *
 * Theme is `localStorage.gdx_theme`, applied as `data-theme` on <html> by
 * src/stores/theme.js.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

const AA_NORMAL_TEXT = 4.5;

/** Parse `rgb(r, g, b)` / `rgba(r, g, b, a)` into [r,g,b,a]. */
function parseRgb(value) {
  const nums = String(value).match(/[\d.]+/g);
  if (!nums || nums.length < 3) return null;
  return [Number(nums[0]), Number(nums[1]), Number(nums[2]), nums.length > 3 ? Number(nums[3]) : 1];
}

function luminance([r, g, b]) {
  const [R, G, B] = [r, g, b]
    .map((c) => c / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * R + 0.7152 * G + 0.0722 * B;
}

function contrast(fg, bg) {
  const a = luminance(fg);
  const b = luminance(bg);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * The colour actually behind an element: walk up until a non-transparent
 * background is found, compositing any alpha along the way. This is the
 * cascade the static gate cannot model — the reason a "Clear" button hid
 * behind a `dark-safe:` opt-out.
 */
const EFFECTIVE_COLORS = (selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const stack = [];
  let node = el;
  while (node && node !== document.documentElement.parentElement) {
    stack.push(getComputedStyle(node).backgroundColor);
    node = node.parentElement;
  }
  return { color: getComputedStyle(el).color, backgrounds: stack };
};

function resolveBackground(backgrounds) {
  // Composite front-to-back until fully opaque.
  let acc = null;
  for (const raw of backgrounds) {
    const c = parseRgb(raw);
    if (!c || c[3] === 0) continue;
    if (!acc) {
      acc = c.slice();
    } else {
      const a = acc[3];
      acc = [0, 1, 2].map((i) => acc[i] * a + c[i] * (1 - a)).concat([a + c[3] * (1 - a)]);
    }
    if (acc[3] >= 0.999) break;
  }
  return acc ? acc.slice(0, 3) : [255, 255, 255];
}

// One token for the whole file. GDX_E2E_BYPASS is 0 on the local stack, so the
// rate limiter is live and a login-per-test 429s after the second one.
let cachedToken = null;

async function login(baseURL) {
  if (cachedToken) return cachedToken;
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login should succeed').toBeTruthy();
  const { access_token } = await r.json();
  await api.dispose();
  cachedToken = access_token;
  return access_token;
}

async function prime(page, token, theme) {
  await page.addInitScript(
    (a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    },
    { t: token, tid: TENANT, theme },
  );
}

/** Read an element's rendered contrast ratio. */
async function ratioOf(page, selector) {
  const measured = await page.evaluate(EFFECTIVE_COLORS, selector);
  expect(measured, `${selector} should exist`).not.toBeNull();
  const fg = parseRgb(measured.color);
  const bg = resolveBackground(measured.backgrounds);
  return { ratio: contrast(fg.slice(0, 3), bg), fg, bg };
}

for (const theme of ['dark', 'light']) {
  test(`landing-lead popup message is readable in ${theme} mode`, async ({ page, baseURL }) => {
    const token = await login(baseURL);
    await prime(page, token, theme);
    await page.goto('/leads');

    await expect(page.locator('.landing-table')).toBeVisible({ timeout: 20000 });
    await page.locator('.landing-table tbody tr').first().click();

    const body = page.locator('[data-testid="landing-message"]');
    await expect(body).toBeVisible({ timeout: 10000 });

    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);

    const { ratio, fg, bg } = await ratioOf(page, '[data-testid="landing-message"]');
    console.log(`landing message ${theme}: fg=${fg} bg=${bg} ratio=${ratio.toFixed(2)}`);
    await page.screenshot({ path: `test-results/landing-popup-${theme}.png` });

    expect(ratio, `landing-lead message body in ${theme} mode`).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
  });

  test(`SMS bubbles are readable in ${theme} mode`, async ({ page, baseURL }) => {
    const token = await login(baseURL);
    await prime(page, token, theme);
    await page.goto('/phone-com/messages');

    await expect(page.locator('[data-test="pc-thread-row"]').first()).toBeVisible({ timeout: 20000 });
    await page.locator('[data-test="pc-thread-row"]').first().click();

    await expect(page.locator('.msg-out .msg-body').first()).toBeVisible({ timeout: 10000 });

    const out = await ratioOf(page, '.msg-out .msg-body');
    const inb = await ratioOf(page, '.msg-in .msg-body');
    const meta = await ratioOf(page, '.msg-out .msg-meta');
    console.log(
      `sms ${theme}: outbound=${out.ratio.toFixed(2)} (bg=${out.bg}) ` +
      `inbound=${inb.ratio.toFixed(2)} meta=${meta.ratio.toFixed(2)}`,
    );
    await page.screenshot({ path: `test-results/sms-thread-${theme}.png` });

    // The reported bug: outbound bubbles were 1.05:1 in dark.
    expect(out.ratio, `outbound SMS bubble in ${theme} mode`).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    expect(inb.ratio, `inbound SMS bubble in ${theme} mode`).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    // The timestamp is small print; hold it to the 3:1 large/incidental line.
    expect(meta.ratio, `SMS timestamp in ${theme} mode`).toBeGreaterThanOrEqual(3);
  });
}
