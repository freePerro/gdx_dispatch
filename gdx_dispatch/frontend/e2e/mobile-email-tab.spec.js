/**
 * Email tab on the mobile bottom nav (2026-09-22) — browser verification.
 *
 * Doug: "Inbox should be email and office roles should have an easy access
 * point to it from mobile." /mobile/inbox has existed since the first release
 * but only behind More → scroll. Pins, against a throwaway container:
 *  1. Office role: seven tabs, Email just before More, badge = the API's
 *     unread count, tap lands on /mobile/inbox with the tab active, and
 *     Inbox is gone from the More drawer (it has a tab now).
 *  2. Tech role: the strip is unchanged (no Email tab) and the More drawer
 *     still carries Inbox, pointed at the mobile view.
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function loginToken(baseURL, email, password) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email, password },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  const { access_token } = await r.json();
  return { api, token: access_token };
}

async function primeAuth(page, token) {
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
    sessionStorage.removeItem('gdx_user');
  }, token);
}

const TAB_LABELS = 'nav.bottom-nav > .tab-btn .tab-label';
// Retrying: the strip is role-shaped and `user` may hydrate after first paint.
const expectStrip = (page, labels) => expect(page.locator(TAB_LABELS)).toHaveText(labels);

test.use({ viewport: { width: 390, height: 844 } });

test('office role: Email tab with unread badge opens the mobile inbox', async ({ page, baseURL }) => {
  const { api, token } = await loginToken(baseURL, EMAIL, PASSWORD);
  const unread = await api.get('/api/outlook/messages/unread-count', {
    headers: { authorization: `Bearer ${token}` },
  });
  expect(unread.ok()).toBeTruthy();
  const { count } = await unread.json();
  await primeAuth(page, token);

  await page.goto('/mobile/jobs');
  await expect(page.locator('[data-testid="tab-inbox"]')).toBeVisible({ timeout: 15000 });
  await expectStrip(page, ['Jobs', 'Customers', 'Clock', 'Planner', 'Dispatch', 'Email', 'More']);

  const badge = page.locator('[data-testid="email-unread-badge-mobile"]');
  if (count > 0) {
    const shown = count > 99 ? '99+' : String(count);
    await expect(badge).toHaveText(shown);
    await expect(page.locator('[data-testid="tab-inbox"]')).toHaveAttribute('aria-label', `Email, ${shown} unread`);
  } else {
    await expect(badge).toHaveCount(0);
  }

  // No label may ellipsize at 7 columns — the 2026-07-22 "Custom…" regression.
  const clipped = await page.locator(TAB_LABELS).evaluateAll(
    (els) => els.filter((el) => el.scrollWidth > el.clientWidth).map((el) => el.textContent),
  );
  expect(clipped).toEqual([]);

  // Inbox is a tab now, so it leaves the More drawer for office roles.
  await page.locator('[data-testid="tab-more"]').click();
  await expect(page.locator('[data-testid="more-search"]')).toBeVisible();
  await expect(page.locator('.drawer-link[href="/mobile/inbox"]')).toHaveCount(0);
  await page.keyboard.press('Escape');

  await page.locator('[data-testid="tab-inbox"]').click();
  await expect(page).toHaveURL(/\/mobile\/inbox$/);
  await expect(page.locator('h1')).toHaveText('Inbox');
  await expect(page.locator('nav.bottom-nav > .tab-btn.active .tab-label')).toHaveText('Email');
  await api.dispose();
});

test.describe('narrow phone', () => {
  // 7 columns at 360px = 51px each; "Customers" at 10px is 48px. The unit
  // suite runs in jsdom and cannot see this, so it is asserted here.
  test.use({ viewport: { width: 360, height: 780 } });

  test('office role: no tab label clips at 360px', async ({ page, baseURL }) => {
    const { api, token } = await loginToken(baseURL, EMAIL, PASSWORD);
    await primeAuth(page, token);
    await page.goto('/mobile/jobs');
    await expect(page.locator('[data-testid="tab-inbox"]')).toBeVisible({ timeout: 15000 });
    await expect(page.locator(TAB_LABELS)).toHaveCount(7);
    const clipped = await page.locator(TAB_LABELS).evaluateAll(
      (els) => els.filter((el) => el.scrollWidth > el.clientWidth).map((el) => el.textContent),
    );
    expect(clipped).toEqual([]);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
    await api.dispose();
  });
});

test('tech role: strip unchanged, Inbox stays in the More drawer', async ({ page, baseURL }) => {
  const { api, token } = await loginToken(baseURL, EMAIL, PASSWORD);
  const techEmail = `e2e-emailtab-tech-${Date.now()}@example.com`;
  const techPassword = 'CorrectHorse9!';
  const created = await api.post('/api/users', {
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    data: { email: techEmail, name: 'Email Tab Tech', password: techPassword, role: 'tech' },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const { id: techId } = await created.json();

  try {
    const tech = await loginToken(baseURL, techEmail, techPassword);
    await primeAuth(page, tech.token);
    await page.goto('/mobile');
    await expect(page.locator('[data-testid="tab-more"]')).toBeVisible({ timeout: 15000 });
    await expectStrip(page, ['Today', 'Jobs', 'Customers', 'Clock', 'Photos', 'More']);
    await expect(page.locator('[data-testid="tab-inbox"]')).toHaveCount(0);

    await page.locator('[data-testid="tab-more"]').click();
    await expect(page.locator('[data-testid="more-search"]')).toBeVisible();
    await expect(page.locator('.drawer-link[href="/mobile/inbox"]')).toHaveCount(1);
    await tech.api.dispose();
  } finally {
    // The throwaway shares the long-lived local DB — do not leave a
    // technician row behind per run (soft delete; the row stays auditable).
    await api.delete(`/api/users/${techId}`, { headers: { authorization: `Bearer ${token}` } });
    await api.dispose();
  }
});
