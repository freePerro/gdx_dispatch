// GDXA-1756: the AI assistant card's primary button takes its text colour
// from --text-on-interactive-primary, so it stays readable in dark mode.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

function luminance(rgb) {
  const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map((v) => {
    const c = Number(v) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
const ratio = (a, b) => {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};

for (const theme of ['light', 'dark']) {
  test(`AI card Save & Test meets AA in ${theme} mode`, async ({ page, baseURL }) => {
    const api = await pwRequest.newContext({ baseURL });
    const r = await api.post('/auth/login', {
      headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(r.ok()).toBeTruthy();
    const { access_token } = await r.json();
    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
      localStorage.setItem('gdx_theme', a.theme);
    }, { t: access_token, tid: TENANT, theme });

    await page.goto('/settings?tab=integrations');
    expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
    const card = page.locator('[data-testid="ai-integration-card"]');
    await expect(card).toBeVisible({ timeout: 15000 });
    await card.locator('[data-test="ai-key-input"]').fill('sk-ant-not-a-real-key');
    const btn = card.locator('[data-test="ai-key-save"]');
    await expect(btn).toBeEnabled();
    await btn.scrollIntoViewIfNeeded();

    const { color, background } = await btn.evaluate((el) => {
      const s = getComputedStyle(el);
      return { color: s.color, background: s.backgroundColor };
    });
    const cr = ratio(color, background);
    console.log(`${theme}: color=${color} background=${background} contrast=${cr.toFixed(2)}:1`);
    await card.screenshot({ path: `test-results/ai-card-1756-${theme}.png` });
    expect(cr).toBeGreaterThanOrEqual(4.5);
    await api.dispose();
  });
}
