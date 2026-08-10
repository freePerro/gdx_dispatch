// Schedule-from-job verification.
//
// Regression guard for two dead ends found 2026-08-10:
//   1. "Schedule/Reschedule" on a job pushed /appointments?job_id=… and that
//      view never read the query param — you landed on an unfiltered
//      appointment list with no way to schedule the job you came from.
//   2. A job with a date but no tech was invisible on the dispatch board
//      whenever dispatch_show_unassigned_lane was off (its default).
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

// One login for the whole file. Three separate logins tripped the auth rate
// limiter (429) and the later tests failed before they ever opened a page.
let sharedSession = null;
async function login(baseURL) {
  if (!sharedSession) sharedSession = await doLogin(baseURL);
  return sharedSession;
}

async function doLogin(baseURL) {
  const bootstrap = await pwRequest.newContext({ baseURL });
  const r = await bootstrap.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok()).toBeTruthy();
  const { access_token } = await r.json();
  await bootstrap.dispose();
  // Every subsequent call needs BOTH the bearer and the tenant header —
  // without the bearer the list endpoints return an empty set rather than a
  // 401, which reads as "no data" and silently no-ops the seeding.
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: {
      authorization: `Bearer ${access_token}`,
      'x-tenant-id': TENANT,
      'x-e2e-test': 'true',
    },
  });
  return { api, access_token };
}

async function seedJob(api, { scheduled_at = null } = {}) {
  const auth = { 'content-type': 'application/json', 'x-tenant-id': TENANT };
  const custRes = await api.get('/api/customers?per_page=1', { headers: auth });
  const custBody = await custRes.json();
  const customers = Array.isArray(custBody) ? custBody : custBody.items || [];
  expect(customers.length).toBeGreaterThan(0);
  const jobRes = await api.post('/api/jobs', {
    headers: auth,
    data: {
      title: `E2E schedule ${scheduled_at ? 'dated' : 'undated'} ${Date.now()}`,
      customer_id: customers[0].id,
      job_type: 'Service Call',
      ...(scheduled_at ? { scheduled_at } : {}),
    },
  });
  expect(jobRes.ok()).toBeTruthy();
  return jobRes.json();
}

test.describe.configure({ mode: 'serial' });

test.describe('scheduling a job', () => {
  test('Schedule on a job opens a dialog that actually schedules it', async ({ page, baseURL }) => {
    const { api, access_token } = await login(baseURL);
    const job = await seedJob(api, {});
    expect(job.scheduled_at).toBeFalsy();

    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    }, { t: access_token, tid: TENANT });

    await page.goto(`/jobs/${job.id}`);

    // The button reads "Schedule" on an undated job (was "Schedule/Reschedule").
    const scheduleBtn = page.locator('[data-testid="job-detail-schedule"]');
    await expect(scheduleBtn).toBeVisible({ timeout: 20000 });
    await expect(scheduleBtn).toContainText('Schedule');

    await scheduleBtn.click();

    // The whole point: a dialog, on this page — not a navigation away.
    const dialog = page.locator('[data-testid="job-schedule-dialog"]');
    await expect(dialog).toBeVisible({ timeout: 10000 });
    expect(page.url()).toContain(`/jobs/${job.id}`);

    // Pick a date via the datepicker's text input.
    const dateInput = page.locator('[data-testid="job-schedule-date"] input').first();
    await dateInput.fill('12/15/2026 09:00 AM');
    await page.keyboard.press('Escape');

    await page.locator('[data-testid="job-schedule-save"]').click();

    // Dialog closes and the job page now shows a real scheduled date.
    await expect(dialog).toBeHidden({ timeout: 15000 });

    // Server-side truth: the JOB row carries the date (not just an
    // appointment), which is what the appointment mirror and the lifecycle
    // stage both key off.
    await expect.poll(async () => {
      const res = await api.get(`/api/jobs/${job.id}`, { headers: { 'x-tenant-id': TENANT } });
      const body = await res.json();
      return body.scheduled_at;
    }, { timeout: 15000 }).toBeTruthy();

    const after = await (await api.get(`/api/jobs/${job.id}`, { headers: { 'x-tenant-id': TENANT } })).json();
    expect(String(after.status).toLowerCase()).toContain('scheduled');

    // The date that came back is the one that was typed (09:00 local), not a
    // timezone-shifted one. Compared in the browser's own zone, which is the
    // zone the picker wrote in.
    const shown = await page.evaluate((iso) => {
      const d = new Date(iso);
      return { y: d.getFullYear(), m: d.getMonth() + 1, day: d.getDate(), h: d.getHours() };
    }, after.scheduled_at);
    expect(shown).toEqual({ y: 2026, m: 12, day: 15, h: 9 });

    // The whole design rests on the job write mirroring into `appointments`
    // (routers/jobs._sync_job_appointment). If that stops happening the job is
    // scheduled but absent from the calendar, so assert the row exists.
    // Explicit window required: GET /api/appointments defaults to now-30d..now+90d,
    // and the date under test is further out than that.
    await expect.poll(async () => {
      const res = await api.get('/api/appointments?start=2026-12-01&end=2026-12-31&limit=500');
      const body = await res.json();
      const rows = Array.isArray(body) ? body : body.items || [];
      return rows.filter((a) => String(a.job_id) === String(job.id)).length;
    }, { timeout: 15000 }).toBeGreaterThan(0);

    // Scheduling clears the "Ready to Schedule" intake stamp — otherwise a
    // booked job keeps showing up as work waiting to be scheduled.
    expect(after.holding_area_id ?? null).toBeNull();
  });

  test('"Open calendar" reaches the calendar instead of bouncing back', async ({ page, baseURL }) => {
    const { api, access_token } = await login(baseURL);
    const job = await seedJob(api, {});

    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    }, { t: access_token, tid: TENANT });

    await page.goto(`/jobs/${job.id}`);
    await page.locator('.job-tabs').getByText('Schedule', { exact: true }).click();
    await page.locator('[data-testid="job-detail-open-appointments"]').click();

    // Must land on the appointments list. Passing ?job_id= here would redirect
    // straight back to this job and reopen the dialog — a button that returns
    // you to where you clicked it.
    await expect(page).toHaveURL(/\/appointments$/, { timeout: 15000 });
    await expect(page.locator('[data-testid="job-schedule-dialog"]')).toHaveCount(0);
  });

  test('legacy /appointments?job_id= lands on the job schedule dialog', async ({ page, baseURL }) => {
    const { api, access_token } = await login(baseURL);
    const job = await seedJob(api, {});

    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    }, { t: access_token, tid: TENANT });

    await page.goto(`/appointments?job_id=${job.id}`);

    await expect(page.locator('[data-testid="job-schedule-dialog"]')).toBeVisible({ timeout: 20000 });
    expect(page.url()).toContain(`/jobs/${job.id}`);

  });

  test('a new job is visible on the dispatch board, above the tech columns', async ({ page, baseURL }) => {
    const { api, access_token } = await login(baseURL);
    const undated = await seedJob(api, {});
    // Dated + no tech: the case that fell through every section when the
    // "Scheduled — Not Assigned" lane is off (its default).
    const dated = await seedJob(api, { scheduled_at: new Date(Date.now() + 86400000).toISOString() });

    await page.addInitScript((a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    }, { t: access_token, tid: TENANT });

    await page.goto('/dispatch');

    const section = page.locator('[data-testid="unassigned-section"]');
    await expect(section).toBeVisible({ timeout: 20000 });

    // A brand-new job is auto-routed into the "Ready to Schedule" holding
    // area server-side; the queue has to show it anyway, or the board reports
    // "nothing to schedule" while the backlog piles up out of sight.
    await expect(page.locator(`[data-testid="unassigned-job-${undated.id}"]`)).toBeVisible({ timeout: 15000 });

    // Dated + no tech has two legal homes depending on the tenant flag: the
    // red "Scheduled — Not Assigned" lane when it's on, this queue when it's
    // off. The invariant under test is that it is never in NEITHER.
    const settings = await (await api.get('/api/dispatch-settings')).json();
    if (settings.dispatch_show_unassigned_lane) {
      await expect(page.locator(`[data-testid="scheduled-unassigned-job-${dated.id}"]`))
        .toBeVisible({ timeout: 15000 });
    } else {
      await expect(page.locator(`[data-testid="unassigned-job-${dated.id}"]`))
        .toBeVisible({ timeout: 15000 });
      // Labelled as waiting on a tech, not on a date.
      await expect(page.locator(`[data-testid="unassigned-date-${dated.id}"]`)).toContainText('needs a tech');
    }

    // It renders ABOVE the tech grid — being below the fold is what made a
    // new job read as "never showed up on dispatch".
    const sectionTop = await section.evaluate((el) => el.getBoundingClientRect().top + window.scrollY);
    const grid = page.locator('.tech-columns-grid');
    if (await grid.count()) {
      const gridTop = await grid.first().evaluate((el) => el.getBoundingClientRect().top + window.scrollY);
      expect(sectionTop).toBeLessThan(gridTop);
    }

    // And the card offers a real scheduling verb, not just "assign a tech".
    await expect(page.locator(`[data-testid="schedule-job-${undated.id}"]`)).toBeVisible();

  });
});
