/**
 * The login email and lock icons sit in the inputs' left padding at every
 * width (GDXA-1496).
 *
 * responsive.css sets `padding: 8px 12px` on email and password inputs below
 * 768px at the same specificity as LoginView's scoped `input` rule, so on a
 * phone the text started 12px in, under the 12px-to-30px icon. jsdom applies
 * no media queries, so only a real browser can see this; it runs by hand
 * against a throwaway container (ci.yml has no Playwright step).
 */
import { test, expect } from '@playwright/test';

const VIEWPORTS = {
  desktop: { width: 1440, height: 900 },
  phone: { width: 390, height: 844 },
};

for (const [vpName, viewport] of Object.entries(VIEWPORTS)) {
  for (const theme of ['light', 'dark']) {
    test(`login icons clear the input text (${vpName}, ${theme})`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript((t) => {
        if (window !== window.top) return;
        localStorage.setItem('gdx_theme', t);
      }, theme);
      await page.goto('/login');
      await expect(page.locator('[data-testid="login-password"]')).toBeVisible();

      for (const testid of ['login-email', 'login-password']) {
        const geometry = await page.locator(`[data-testid="${testid}"]`).evaluate((input) => {
          const icon = input.parentElement.querySelector('.input-icon');
          const box = input.getBoundingClientRect();
          const style = getComputedStyle(input);
          return {
            textStart: box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
            iconEnd: icon.getBoundingClientRect().right,
            fontSize: parseFloat(style.fontSize),
          };
        });
        expect(geometry.textStart, `${testid} text starts under its icon`).toBeGreaterThan(geometry.iconEnd);
        // iOS Safari zooms a focused input below 16px.
        expect(geometry.fontSize).toBeGreaterThanOrEqual(16);
      }
      await page.screenshot({ path: `test-results/login-icon-gutter-${vpName}-${theme}.png` });
    });
  }
}
