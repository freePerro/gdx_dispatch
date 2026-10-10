// GDXA-423: typing an ISO date key by key into a yy-mm-dd DatePicker keeps every
// key where it was typed. Uses the Expenses "new expense" dialog's date field
// and never saves, so it writes nothing.
import { test, expect } from './_fixtures.js';

const SHOTS = process.env.E2E_SHOT_DIR;

for (const theme of ['light', 'dark']) {
  for (const vp of [
    { name: 'desktop', size: { width: 1366, height: 900 } },
    { name: 'mobile', size: { width: 412, height: 915 } }, // Pixel 8 CSS viewport
  ]) {
    test(`yy-mm-dd typed key by key stores what was typed (${theme}, ${vp.name})`, async ({ page }) => {
      await page.setViewportSize(vp.size);
      await page.addInitScript((t) => { if (window === window.top) localStorage.setItem('gdx_theme', t); }, theme);
      await page.goto('/expenses');
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      await page.locator('[data-testid="expenses-new-btn"], [data-testid="expenses-empty-btn"]').first().click();
      const input = page.locator('[data-testid="expense-date"] input');
      await expect(input).toBeVisible({ timeout: 15000 });

      await input.click();
      await input.fill('');
      const seen = [];
      for (const ch of '2026-07-31') {
        await page.keyboard.type(ch);
        seen.push(await input.inputValue());
      }
      expect(seen).toEqual(['2', '20', '202', '2026', '2026-', '2026-0', '2026-07', '2026-07-', '2026-07-3', '2026-07-31']);

      // Typed at machine speed, the same text survives.
      await input.fill('');
      await page.keyboard.type('2026-07-31', { delay: 0 });
      await expect(input).toHaveValue('2026-07-31');

      // Leave the box: the date is committed and the overlay shows the 31st.
      await page.locator('[data-testid="expense-vendor"]').click();
      await expect(input).toHaveValue('2026-07-31');
      await input.click();
      await expect(page.locator('.p-datepicker-day-selected, [data-p-selected="true"]').first()).toHaveText('31');
      if (SHOTS) await page.screenshot({ path: `${SHOTS}/datepicker-423-${theme}-${vp.name}.png` });
      await page.keyboard.press('Escape');

      // A half-typed day left in the box still means that day.
      await input.fill('');
      await page.keyboard.type('2026-07-3');
      await expect(input).toHaveValue('2026-07-3');
      await page.locator('[data-testid="expense-vendor"]').click();
      await expect(input).toHaveValue('2026-07-03');
    });
  }
}
