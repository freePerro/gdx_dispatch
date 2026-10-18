/**
 * GDXA-1481: the active segment chip and the customer link read in both themes.
 *
 * Both painted with `var(--accent-b)`, which nothing defines, so the active
 * chip had a transparent fill under white text — invisible in light mode, on
 * the chip that names the audience Add Tag / Export CSV act on. This reads
 * the computed colours off the rendered elements and checks WCAG contrast
 * against what is actually behind them, light and dark, desktop and phone.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

const VIEWPORTS = { desktop: { width: 1366, height: 900 }, phone: { width: 390, height: 844 } };

// Runs in the page: colour of the element, its own fill, and the first opaque
// fill behind it, plus the contrast ratio of text against that fill.
function measure(sel) {
  const el = document.querySelector(sel);
  if (!el) return null;
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  let node = el;
  let fill = null;
  while (node && node.nodeType === 1) {
    const bg = parse(getComputedStyle(node).backgroundColor);
    if (bg && bg.a >= 0.99) { fill = bg; break; }
    node = node.parentElement;
  }
  fill = fill || { r: 255, g: 255, b: 255, a: 1 };
  const cs = getComputedStyle(el);
  const fg = parse(cs.color);
  const [hi, lo] = [lum(fg), lum(fill)].sort((x, y) => y - x);
  return {
    color: cs.color,
    ownBackground: cs.backgroundColor,
    behind: `rgb(${fill.r}, ${fill.g}, ${fill.b})`,
    ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100,
  };
}

for (const theme of ['light', 'dark']) {
  for (const [vp, size] of Object.entries(VIEWPORTS)) {
    test(`segment chip + customer link contrast — ${theme} ${vp}`, async ({ page, baseURL }) => {
      const api = await pwRequest.newContext({ baseURL });
      const r = await api.post('/auth/login', {
        headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
        data: { email: EMAIL, password: PASSWORD },
      });
      expect(r.ok()).toBeTruthy();
      const { access_token } = await r.json();
      await page.setViewportSize(size);
      await page.addInitScript((a) => {
        if (window !== window.top) return;
        sessionStorage.setItem('gdx_access_token', a.t);
        sessionStorage.setItem('gdx_tenant_slug', a.tid);
        localStorage.setItem('gdx_theme', a.theme);
      }, { t: access_token, tid: TENANT, theme });

      await page.goto('/segments');
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      const chip = page.locator('[data-testid="segment-chip-all"]');
      await expect(chip).toHaveClass(/active/, { timeout: 15000 });

      const c = await page.evaluate(measure, '[data-testid="segment-chip-all"]');
      console.log(`${theme}/${vp} active chip`, JSON.stringify(c));
      // The fill itself must be opaque — the bug was a transparent one.
      expect(c.ownBackground).not.toMatch(/rgba\(0, 0, 0, 0\)|transparent/);
      expect(c.ratio).toBeGreaterThanOrEqual(4.5);

      const link = page.locator('[data-testid="segments-customer-link"]').first();
      await expect(link).toBeVisible({ timeout: 15000 });
      const l = await page.evaluate(measure, '[data-testid="segments-customer-link"]');
      console.log(`${theme}/${vp} customer link`, JSON.stringify(l));
      expect(l.ratio).toBeGreaterThanOrEqual(4.5);

      await chip.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `test-results/gdxa1481-${theme}-${vp}.png` });
      await api.dispose();
    });
  }
}
