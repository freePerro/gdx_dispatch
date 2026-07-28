/**
 * Dark-mode probe — measures the rules a page load never shows.
 *
 * Most of PR #211's fixes are CONDITIONAL states: error banners, drag-over
 * highlights, bulk-select bars, summary tiles on detail pages. A screenshot
 * walk cannot reach them, and asserting them from the stylesheet is what the
 * static gate already does — the thing that missed the cascade twice.
 *
 * So: mount a probe element carrying the real class INSIDE the real view, in
 * the real browser, and read getComputedStyle off it. The trick is Vue scoped
 * styles — they only apply to elements carrying the component's `data-v-*`
 * attribute, so the probe copies that attribute off an element already in the
 * DOM. Real CSS, real cascade, real engine.
 *
 * A probe that cannot find its host component fails loudly rather than
 * silently passing, because a silent skip here would be the same "green means
 * I looked" lie the static gate told.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const CREDS = { email: process.env.E2E_EMAIL, password: process.env.E2E_PASSWORD };
const AA_NORMAL_TEXT = 4.5;

let cachedToken = null;
async function login(baseURL) {
  if (cachedToken) return cachedToken;
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: CREDS,
  });
  expect(r.ok(), 'login should succeed').toBeTruthy();
  cachedToken = (await r.json()).access_token;
  await api.dispose();
  return cachedToken;
}

/**
 * Mount `<div class="...">text</div>` inside `host`, inheriting the host's
 * scoped-style attribute, and return its rendered colours with the real
 * background resolved by walking ancestors and compositing alpha.
 */
const PROBE = ({ hostSelector, classes }) => {
  const host = document.querySelector(hostSelector);
  if (!host) return { error: `host not found: ${hostSelector}` };

  // Vue scoped styles key off a data-v-* attribute; inherit it or the rule
  // simply will not apply and the probe measures nothing.
  const scopeAttr = [...host.attributes].map((a) => a.name).find((n) => n.startsWith('data-v-'));

  const el = document.createElement('div');
  if (scopeAttr) el.setAttribute(scopeAttr, '');
  el.className = classes;
  el.textContent = 'probe';
  host.appendChild(el);

  const cs = getComputedStyle(el);
  const color = cs.color;
  const backgrounds = [];
  let node = el;
  while (node) {
    backgrounds.push(getComputedStyle(node).backgroundColor);
    node = node.parentElement;
  }
  const ownBackground = cs.backgroundColor;
  el.remove();
  return { color, backgrounds, ownBackground, scoped: Boolean(scopeAttr) };
};

/**
 * Parse a computed colour. Chrome serialises a resolved color-mix() as
 * `color(srgb 0.12 0.29 0.24 / 0.5)` — 0-1 floats — NOT as rgb(). A naive
 * number-scrape reads 0.12 as 0/255 and reports the swatch as black, which
 * silently turns every color-mix probe into a vacuous white-on-black pass.
 * Caught exactly that on the first run.
 */
function parseRgb(v) {
  const s = String(v).trim();
  if (!s || s === 'transparent') return null;
  const srgb = /^color\(\s*srgb\s+([^)]+)\)$/i.exec(s);
  if (srgb) {
    const parts = srgb[1].split('/');
    const rgb = parts[0].trim().split(/\s+/).map(Number);
    if (rgb.length < 3 || rgb.some(Number.isNaN)) return null;
    const alpha = parts[1] === undefined ? 1 : Number(parts[1].trim());
    return [rgb[0] * 255, rgb[1] * 255, rgb[2] * 255, Number.isNaN(alpha) ? 1 : alpha];
  }
  const n = s.match(/[\d.]+/g);
  if (!n || n.length < 3) return null;
  return [Number(n[0]), Number(n[1]), Number(n[2]), n.length > 3 ? Number(n[3]) : 1];
}
function luminance([r, g, b]) {
  const [R, G, B] = [r, g, b].map((c) => c / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * R + 0.7152 * G + 0.0722 * B;
}
function contrast(fg, bg) {
  const a = luminance(fg); const b = luminance(bg);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}
function flatten(backgrounds) {
  let acc = null;
  for (const raw of backgrounds) {
    const c = parseRgb(raw);
    if (!c || c[3] === 0) continue;
    if (!acc) acc = c.slice();
    else {
      const a = acc[3];
      acc = [0, 1, 2].map((i) => acc[i] * a + c[i] * (1 - a)).concat([a + c[3] * (1 - a)]);
    }
    if (acc[3] >= 0.999) break;
  }
  return acc ? acc.slice(0, 3) : [255, 255, 255];
}

// route, host selector (must exist on that route), probe classes, label
const PROBES = [
  ['/dispatch', '.tech-timeline-tray', 'tech-timeline-tray tech-timeline-tray--drag-over', 'dispatch tray drag-over'],
  ['/dispatch', '.tech-timeline-body', 'tech-timeline-body tech-timeline-body--drag-over', 'dispatch body drag-over'],
  ['/dispatch', '.job-block', 'job-block', 'dispatch job block'],
  ['/billing', '.p-datatable', 'bulk-actions-bar', 'billing bulk-actions bar'],
  ['/billing', '.p-datatable', 'bulk-progress-row', 'billing bulk-progress row'],
  ['/quickbooks', '.qb-view, .view-card', 'sync-error-list', 'quickbooks sync-error list'],
  ['/budget', '.kpi', 'kpi', 'budget KPI tile'],
  ['/catalog', '.view-card', 'native-date', 'catalog native date'],
];

for (const [route, hostSelector, classes, label] of PROBES) {
  test(`probe: ${label} is readable in dark mode`, async ({ page, baseURL }) => {
    const token = await login(baseURL);
    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', 'dark');
    }, { t: token, tid: TENANT });

    await page.goto(route, { waitUntil: 'domcontentloaded' });
    await page.locator(hostSelector).first().waitFor({ timeout: 20000 });
    await page.waitForTimeout(1200);

    const res = await page.evaluate(PROBE, { hostSelector, classes });
    expect(res.error, `probe host missing for ${label}`).toBeUndefined();

    const fg = parseRgb(res.color);
    expect(parseRgb(res.ownBackground), `${label}: own background did not parse (${res.ownBackground})`).not.toBeNull();
    expect(parseRgb(res.ownBackground)[3], `${label}: rule did not apply — background is transparent`).toBeGreaterThan(0);
    const bg = flatten(res.backgrounds);
    const ratio = contrast(fg.slice(0, 3), bg);
    console.log(
      `${label}: own-bg=${res.ownBackground} resolved-bg=rgb(${bg.map(Math.round)}) ` +
      `fg=${res.color} ratio=${ratio.toFixed(2)} scoped=${res.scoped}`,
    );

    expect(ratio, `${label} in dark mode`).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
  });
}
