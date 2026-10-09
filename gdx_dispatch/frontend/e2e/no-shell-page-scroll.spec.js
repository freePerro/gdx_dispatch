// A no-shell page (login, customer portal, public proposal) scrolls as a normal
// page. base.css locks body scrolling for AppLayout, which scrolls inside its
// own main panel; before App.vue set body.no-shell, the portal clipped
// everything below the fold and a wheel moved nothing. Read-only: writes nothing.
import { test, expect } from '@playwright/test';
import { test as staffTest } from './_fixtures.js';

for (const path of ['/login', '/customer-portal']) {
  test(`${path} scrolls when its content is taller than the window`, async ({ page }) => {
    await page.setViewportSize({ width: 412, height: 240 });
    await page.goto(path);
    await expect(page.locator('body')).toHaveClass(/\bno-shell\b/);
    const overflow = await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight);
    expect(overflow, 'the page must be taller than the window or this proves nothing').toBeGreaterThan(0);
    await page.mouse.move(200, 120);
    await page.mouse.wheel(0, 2000);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  });
}

// The class must come off again: a staff page keeps body locked so only
// AppLayout's main panel scrolls. Leaving it on would scroll the whole shell.
staffTest('a staff page leaves the shell lock in place after a no-shell page', async ({ page }) => {
  await page.goto('/customer-portal');
  await expect(page.locator('body')).toHaveClass(/\bno-shell\b/);
  // An in-app route change, not a reload: a fresh document would start clean.
  await page.evaluate(() => document.querySelector('#app').__vue_app__.config.globalProperties.$router.push('/dashboard'));
  await expect(page.locator('.app-layout')).toBeVisible();
  await expect(page.locator('body')).not.toHaveClass(/\bno-shell\b/);
  expect(await page.evaluate(() => getComputedStyle(document.body).overflow)).toBe('hidden');
});
