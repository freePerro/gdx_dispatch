/**
 * Time off and holiday pay (2026-09-23) — browser verification against a
 * throwaway container running the working tree.
 *
 * Doug: "nothing lets anyone put down a vacation day or holiday pay." The
 * walk, as the two people who use it would take it:
 *
 *  1. Office (desktop): Settings → Feature Settings → "Time off and holidays":
 *     set the day length, add two holidays inside the current period, save,
 *     post one of them for the e2e technician from the row.
 *  2. Office: /timesheets shows the requests card, a notice for the holiday
 *     nobody was paid for (with Post), and Add Entry → Type = Vacation over
 *     two workdays lands vacation rows with the split totals. The CSV the
 *     Export button downloads carries the split.
 *  3. Technician (phone viewport): /mobile/timeclock → Time off → Request →
 *     pending with Cancel. A second request for the deny path.
 *  4. Office: approve the first (the tech's card gains a day off), deny the
 *     second with a note the tech can read.
 *  5. Put the calendar back. Entries are cleaned by the runner (no delete
 *     route on entries — that is by design).
 *
 * Needs E2E_EMAIL/E2E_PASSWORD (an admin) and E2E_TECH_EMAIL/E2E_TECH_PASSWORD
 * (a technician with a full_name), both on the target with GDX_E2E_BYPASS=1.
 */
import fs from 'node:fs';
import path from 'node:path';
import { test, expect, request as pwRequest } from '@playwright/test';

const ADMIN_EMAIL = process.env.E2E_EMAIL;
const ADMIN_PASSWORD = process.env.E2E_PASSWORD;
const TECH_EMAIL = process.env.E2E_TECH_EMAIL;
const TECH_PASSWORD = process.env.E2E_TECH_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || '';

async function loginToken(baseURL, email, password) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email, password },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  const { access_token } = await r.json();
  return { api, token: access_token, headers: { authorization: `Bearer ${access_token}` } };
}

function userIdOf(token) {
  const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64').toString('utf8'));
  return String(payload.user_id || payload.sub);
}

async function primeAuth(page, token) {
  await page.addInitScript((t) => {
    sessionStorage.setItem('gdx_access_token', t);
    sessionStorage.removeItem('gdx_user');
  }, token);
}

// Toasts: the app mounts more than one <Toast> outlet, so one message can
// render twice; `.first()` keeps the assertion about the message, not the outlets.
async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`), fullPage: true });
}

/** Screenshot the current route in light and in dark, then leave it light.
 *  `prepare` re-opens the surface after each reload (a tab, a scroll). */
async function lightAndDark(page, name, prepare = async () => {}) {
  await prepare();
  await shot(page, `${name}-light`);
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'dark'));
  await page.reload();
  await page.waitForLoadState('networkidle');
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe('dark');
  await prepare();
  await shot(page, `${name}-dark`);
  await page.evaluate(() => localStorage.setItem('gdx_theme', 'light'));
  await page.reload();
  await page.waitForLoadState('networkidle');
  await prepare();
}

/** PrimeVue Select: open, optionally filter, pick the option by text. */
async function pickOption(page, testid, text, filterText) {
  await page.locator(`[data-testid="${testid}"]`).click();
  if (filterText !== undefined) {
    await page.locator('.p-select-overlay .p-select-filter').fill(filterText);
  }
  await page.locator('.p-select-overlay .p-select-option', { hasText: text }).first().click();
}

function iso(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The range /timesheets opens on: the current pay period, else this week. */
async function displayedRange(api, headers) {
  const r = await api.get('/api/timeclock/pay-periods', { headers });
  expect(r.ok()).toBeTruthy();
  const pp = await r.json();
  if (pp.configured && pp.current) return { start: pp.current.start, end: pp.current.end };
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const monday = new Date(today);
  monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  return { start: iso(monday), end: iso(sunday) };
}

function weekdaysIn(start, end) {
  const out = [];
  const [y, m, d] = start.split('-').map(Number);
  const cursor = new Date(y, m - 1, d);
  for (let i = 0; i < 40 && iso(cursor) <= end; i += 1) {
    if (cursor.getDay() >= 1 && cursor.getDay() <= 5) out.push(iso(cursor));
    cursor.setDate(cursor.getDate() + 1);
  }
  return out;
}

// Shared across the ordered tests below (workers: 1, fullyParallel: false).
const S = { techId: '', days: [], holidayA: '', holidayB: '', reqApprove: '', reqDeny: '', range: null };

test.describe.configure({ mode: 'serial' });

test('office: the Settings card saves the calendar and posts a holiday from the row', async ({ page, baseURL }) => {
  const { api, token, headers } = await loginToken(baseURL, ADMIN_EMAIL, ADMIN_PASSWORD);
  const tech = await loginToken(baseURL, TECH_EMAIL, TECH_PASSWORD);
  S.techId = userIdOf(tech.token);
  await tech.api.dispose();

  S.range = await displayedRange(api, headers);
  S.days = weekdaysIn(S.range.start, S.range.end);
  expect(S.days.length, `need 5 workdays in ${S.range.start}..${S.range.end}`).toBeGreaterThanOrEqual(5);
  S.holidayA = S.days[3];
  S.holidayB = S.days[4];

  // Start from a clean calendar so row indexes are predictable.
  const reset = await api.patch('/api/settings', { headers, data: { holiday_calendar: [] } });
  expect(reset.ok(), await reset.text()).toBeTruthy();

  await primeAuth(page, token);
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto('/settings');
  await page.getByRole('tab', { name: 'Feature Settings' }).click();
  const card = page.locator('[data-testid="time-off-card"]');
  await expect(card).toBeVisible({ timeout: 15000 });
  await card.scrollIntoViewIfNeeded();

  await page.locator('[data-testid="time-off-default-hours"] input').fill('8');
  await page.locator('[data-testid="holiday-add"]').click();
  await page.locator('[data-testid="holiday-date-0"]').fill(S.holidayA);
  await page.locator('[data-testid="holiday-name-0"]').fill('Walk Holiday A');
  await page.locator('[data-testid="holiday-hours-0"] input').fill('8');
  // Unsaved row: Post is disabled — the server pays the SAVED calendar.
  await expect(page.locator('[data-testid="holiday-post-0"]')).toBeDisabled();
  await page.locator('[data-testid="holiday-add"]').click();
  await page.locator('[data-testid="holiday-date-1"]').fill(S.holidayB);
  await page.locator('[data-testid="holiday-name-1"]').fill('Walk Holiday B');
  await page.locator('[data-testid="holiday-hours-1"] input').fill('4');
  await page.locator('[data-testid="time-off-save"]').click();
  await expect(page.locator('.p-toast-message', { hasText: 'Time off settings saved' }).first()).toBeVisible();

  const saved = await (await api.get('/api/settings', { headers })).json();
  expect(saved.holiday_calendar).toEqual([
    { date: S.holidayA, name: 'Walk Holiday A', minutes: 480 },
    { date: S.holidayB, name: 'Walk Holiday B', minutes: 240 },
  ]);
  expect(saved.time_off_default_minutes).toBe(480);
  expect(saved.time_off_counts_toward_overtime).toBe(false);

  await lightAndDark(page, 'settings-time-off', async () => {
    await page.getByRole('tab', { name: 'Feature Settings' }).click();
    await expect(card).toBeVisible();
    await card.scrollIntoViewIfNeeded();
  });

  // Post holiday A for the e2e tech only.
  await expect(page.locator('[data-testid="holiday-post-0"]')).toBeEnabled();
  await page.locator('[data-testid="holiday-post-0"]').click();
  const dlg = page.locator('[data-testid="holiday-post-dialog"]');
  await expect(dlg).toBeVisible();
  await expect(dlg.locator('[data-testid="holiday-people"]')).toBeVisible();
  const boxes = dlg.locator('[data-testid^="holiday-person-"]');
  const n = await boxes.count();
  for (let i = 0; i < n; i += 1) {
    const box = boxes.nth(i);
    const id = (await box.getAttribute('data-testid')).replace('holiday-person-', '');
    const checked = await box.locator('input').isChecked();
    if ((id === S.techId) !== checked) await box.click();
  }
  await expect(dlg.locator('[data-testid="holiday-post-confirm"]')).toHaveText('Post for 1');
  await dlg.locator('[data-testid="holiday-post-confirm"]').click();
  await expect(dlg.locator('[data-testid="holiday-post-result"]')).toContainText('Posted for 1 person');
  await dlg.locator('[data-testid="holiday-post-close"]').click();
  await expect(page.locator('[data-testid="holiday-posted-0"]')).toHaveText('1 person');
  await api.dispose();
});

test('office: Timesheets shows the notice, adds vacation days, splits the totals, and the CSV carries the split', async ({ page, baseURL }) => {
  const { api, token, headers } = await loginToken(baseURL, ADMIN_EMAIL, ADMIN_PASSWORD);
  await primeAuth(page, token);
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto('/timesheets');
  await expect(page.locator('[data-testid="time-off-requests"]')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('[data-testid="time-off-requests-empty"]')).toBeVisible();

  // Holiday B has nobody paid → notice with Post; A is posted → no notice.
  await expect(page.locator(`[data-testid="holiday-unposted-${S.holidayB}"]`)).toContainText('Walk Holiday B');
  await expect(page.locator(`[data-testid="holiday-unposted-${S.holidayA}"]`)).toHaveCount(0);
  await page.locator(`[data-testid="holiday-post-${S.holidayB}"]`).click();
  await expect(page.locator('[data-testid="holiday-post-dialog"]')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-testid="holiday-post-dialog"]')).toBeHidden();

  // Add Entry → Type = Vacation for the tech over the first two workdays.
  await page.locator('[data-testid="timesheets-add-entry"]').click();
  await expect(page.locator('[data-testid="entry-dialog"]')).toBeVisible();
  await pickOption(page, 'entry-tech-select', 'E2E Tech', 'E2E Tech');
  await pickOption(page, 'entry-kind-select', 'Vacation');
  await expect(page.locator('[data-testid="entry-clock-in"]')).toHaveCount(0);
  await page.locator('[data-testid="entry-off-start"] input').fill(S.days[0]);
  await page.locator('[data-testid="entry-off-start"] input').press('Tab');
  await page.locator('[data-testid="entry-off-end"] input').fill(S.days[1]);
  await page.locator('[data-testid="entry-off-end"] input').press('Tab');
  await page.locator('[data-testid="entry-off-hours"] input').fill('8');
  await page.locator('[data-testid="entry-off-hours"] input').press('Tab');
  await page.locator('[data-testid="entry-notes"]').fill('walk: office-entered vacation');
  await page.locator('[data-testid="entry-save"]').click();
  await expect(page.locator('.p-toast-message', { hasText: '2 vacation days added' }).first()).toBeVisible();

  // The card: worked 0, time off = 2 vacation days + holiday A = 24h.
  const card = page.locator(`[data-testid="timecard-${S.techId}"]`);
  await expect(card).toBeVisible();
  await expect(page.locator(`[data-testid="timecard-time-off-${S.techId}"]`)).toHaveText('+ 24.00h off');
  await expect(page.locator(`[data-testid="timecard-total-${S.techId}"]`)).toContainText('0.00h');
  const table = page.locator(`[data-testid="timecard-table-${S.techId}"]`);
  await expect(table.locator('.p-tag', { hasText: 'Vacation' })).toHaveCount(2);
  await expect(table.locator('.p-tag', { hasText: 'Holiday' })).toHaveCount(1);
  await expect(table.locator('.hours-off')).toHaveCount(3);
  await expect(page.locator('[data-testid="timesheets-time-off-tile"]')).toContainText('24.00h');
  await expect(page.locator('[data-testid="timesheets-ot-statement"]')).toHaveText('not counted toward overtime');

  // Export CSV from the page button.
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('[data-testid="timesheets-export-csv"]').click(),
  ]);
  const csv = fs.readFileSync(await download.path(), 'utf8');
  const lines = csv.trim().split('\n');
  const header = lines[0].split(',');
  expect(header).toContain('type');
  expect(header).toContain('time_off_hours');
  expect(header.join(',')).not.toMatch(/overtime|rate|gross|pay/);
  const vacRow = lines.map((l) => l.split(',')).find((cols) => cols[header.indexOf('type')] === 'vacation');
  expect(vacRow, csv).toBeTruthy();
  expect(vacRow[header.indexOf('clock_in')]).toBe('');
  expect(vacRow[header.indexOf('worked_hours')]).toBe('');
  expect(vacRow[header.indexOf('time_off_hours')]).toBe('8.00');

  await lightAndDark(page, 'timesheets-time-off');
  await api.dispose();
});

test.describe('technician on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('requests time off from the mobile clock and sees it pending', async ({ page, baseURL }) => {
    const { api, token, headers } = await loginToken(baseURL, TECH_EMAIL, TECH_PASSWORD);
    await primeAuth(page, token);
    await page.goto('/mobile/timeclock');
    const panel = page.locator('[data-testid="time-off-panel"]');
    await expect(panel).toBeVisible({ timeout: 15000 });
    await panel.scrollIntoViewIfNeeded();
    await expect(panel.locator('[data-testid="time-off-empty"]')).toBeVisible();

    async function requestDay(day, reason) {
      await panel.locator('[data-testid="time-off-request-btn"]').click();
      const dlg = page.locator('[data-testid="time-off-dialog"]');
      await expect(dlg).toBeVisible();
      await dlg.locator('[data-testid="to-start"] input').fill(day);
      await dlg.locator('[data-testid="to-start"] input').press('Tab');
      await dlg.locator('[data-testid="to-end"] input').fill(day);
      await dlg.locator('[data-testid="to-end"] input').press('Tab');
      await expect(dlg.locator('[data-testid="to-workday-preview"]')).toContainText('1 workday');
      await dlg.locator('[data-testid="to-notes"]').fill(reason);
      await dlg.locator('[data-testid="to-save"]').click();
      await expect(dlg).toBeHidden();
    }

    await requestDay(S.days[2], 'walk: family trip');
    await expect(panel.locator('[data-testid="time-off-request-list"]')).toContainText('family trip');
    await expect(panel.locator('[data-testid="time-off-request-list"]')).toContainText('pending');
    await expect(panel.locator('[data-testid^="time-off-cancel-"]')).toHaveCount(1);

    await requestDay(S.days[3], 'walk: dentist');
    await expect(panel.locator('[data-testid^="time-off-cancel-"]')).toHaveCount(2);

    const mine = await (await api.get('/api/timeclock/time-off/requests', { headers })).json();
    S.reqApprove = mine.find((r) => r.notes === 'walk: family trip').id;
    S.reqDeny = mine.find((r) => r.notes === 'walk: dentist').id;
    expect(S.reqApprove && S.reqDeny).toBeTruthy();

    await lightAndDark(page, 'mobile-timeclock-time-off', async () => {
      await expect(panel).toBeVisible({ timeout: 15000 });
      await panel.scrollIntoViewIfNeeded();
    });
    await api.dispose();
  });

});

test('office: approves one request (the day lands on the card) and denies the other with a note', async ({ page, baseURL }) => {
  const { api, token, headers } = await loginToken(baseURL, ADMIN_EMAIL, ADMIN_PASSWORD);
  await primeAuth(page, token);
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto('/timesheets');
  await expect(page.locator('[data-testid="time-off-pending-count"]')).toHaveText('2 pending', { timeout: 15000 });
  const row = page.locator(`[data-testid="time-off-request-${S.reqApprove}"]`);
  await expect(row).toContainText('E2E Tech');
  await expect(row).toContainText('1 workday × 8h');

  await page.locator(`[data-testid="time-off-approve-${S.reqApprove}"]`).click();
  await expect(page.locator('.p-toast-message', { hasText: 'Approved — 1 day added' }).first()).toBeVisible();
  await expect(page.locator(`[data-testid="timecard-time-off-${S.techId}"]`)).toHaveText('+ 32.00h off');
  await expect(page.locator('[data-testid="time-off-pending-count"]')).toHaveText('1 pending');

  await page.locator(`[data-testid="time-off-deny-${S.reqDeny}"]`).click();
  await page.locator(`[data-testid="time-off-deny-note-${S.reqDeny}"]`).fill('walk: short-staffed that day');
  await page.locator(`[data-testid="time-off-deny-confirm-${S.reqDeny}"]`).click();
  await expect(page.locator('.p-toast-message', { hasText: 'Denied' }).first()).toBeVisible();
  await expect(page.locator('[data-testid="time-off-pending-count"]')).toHaveCount(0);
  await page.locator('[data-testid="time-off-toggle-decided"]').click();
  await expect(page.locator(`[data-testid="time-off-request-${S.reqDeny}"]`)).toContainText('denied');
  await expect(page.locator(`[data-testid="time-off-request-${S.reqDeny}"]`)).toContainText('short-staffed');

  const all = await (await api.get('/api/timeclock/time-off/requests?all_technicians=true', { headers })).json();
  const approved = all.find((r) => r.id === S.reqApprove);
  const denied = all.find((r) => r.id === S.reqDeny);
  expect(approved.status).toBe('approved');
  expect(approved.entry_ids).toHaveLength(1);
  expect(denied.status).toBe('denied');
  expect(denied.review_note).toBe('walk: short-staffed that day');
  await api.dispose();
});

test.describe('technician on a phone, after the ruling', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('sees the ruling on the phone: the denial note, the approved day, no Cancel', async ({ page, baseURL }) => {
    const { api, token } = await loginToken(baseURL, TECH_EMAIL, TECH_PASSWORD);
    await primeAuth(page, token);
    await page.goto('/mobile/timeclock');
    const panel = page.locator('[data-testid="time-off-panel"]');
    await expect(panel).toBeVisible({ timeout: 15000 });
    await expect(panel.locator(`[data-testid="time-off-request-${S.reqDeny}"]`)).toContainText('Office: walk: short-staffed that day');
    await expect(panel.locator(`[data-testid="time-off-request-${S.reqApprove}"]`)).toContainText('approved');
    await expect(panel.locator('[data-testid^="time-off-cancel-"]')).toHaveCount(0);
    // The approved day is on the week view as a paid day off (it is in range).
    await expect(page.locator('[data-test="mt-time-off-tag"]').first()).toHaveText(/Vacation|Holiday/);
    // Today's Entries never prints clock stamps on a day off.
    const today = page.locator('[data-test="mt-entry-row"]');
    if (await today.count()) {
      const texts = await today.allTextContents();
      for (const t of texts) if (/HOLIDAY|VACATION/.test(t)) expect(t).toContain('Paid day off');
    }
    await api.dispose();
  });
});

test('the calendar is put back', async ({ baseURL }) => {
  const { api, headers } = await loginToken(baseURL, ADMIN_EMAIL, ADMIN_PASSWORD);
  const reset = await api.patch('/api/settings', { headers, data: { holiday_calendar: [] } });
  expect(reset.ok()).toBeTruthy();
  await api.dispose();
});
