// Creating a job with "Schedule an appointment" ticked must still look like a
// SUCCESS, because the job really is created.
//
// The bug (prod, 2026-07-29): JobsView POSTed /api/appointments with
// {job_id, date, time, notes}. AppointmentIn requires title/start_at/end_at and
// has no date/time fields, so that call 422s every single time. The await was
// unguarded, so the throw unwound past `showFormDialog = false` AND
// `await fetchJobs()`. The operator saw a red error over a job that HAD been
// created, with the dialog still full of their data and the list not refreshed —
// so they saved again, and the shop got two records of one job.
//
// Scope correction from adversarial review: this does NOT explain the five
// duplicated job NUMBERS in prod. next_job_number locks the counter FOR UPDATE,
// so a second save takes the next number, never a colliding one — those come
// from the counter being reset backwards (plan §5) and are a separate fix.
//
// Asserted here against the real rendered SPA:
//   1. appointment ticked  → dialog CLOSES, job appears in the list, no error
//      toast, and NO request to /api/appointments or /api/feedback/client-error.
//   2. an appointment ROW really exists for the job afterwards — the date goes
//      out as `scheduled_at` and the backend's _sync_job_appointment mirrors it
//      into `appointments`. That already-live path is why the frontend POST was
//      never needed.
//   3. the operator's appointment note survives in the job's dispatch notes
//      (JobCreate had no `notes` field at all until this change).
//   4. appointment unticked → clean success, dialog closes, job appears.
//
// Credentials come from env (E2E_EMAIL/E2E_PASSWORD/E2E_TENANT_SLUG) so no
// secret is committed.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';

const stamp = process.env.E2E_STAMP || String(Date.now());

// One login for the whole file. Logging in per test 429s on the rate limiter
// even with the e2e bypass header.
let cachedToken = null;

async function login(baseURL) {
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: {
      'x-tenant-id': TENANT,
      'content-type': 'application/json',
      'x-e2e-test': 'true',
    },
  });
  if (!cachedToken) {
    const r = await api.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
    expect(r.ok(), `login: ${r.status()}`).toBeTruthy();
    cachedToken = (await r.json()).access_token;
  }
  return { api, token: cachedToken };
}

async function primeSession(page, token) {
  await page.addInitScript(
    (a) => {
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    },
    { t: token, tid: TENANT },
  );
}

async function openNewJob(page, { title, customerName }) {
  await page.goto('/jobs');
  await expect(page.locator('[data-testid="new-job-btn"]')).toBeVisible({ timeout: 20000 });
  await page.locator('[data-testid="new-job-btn"]').click();
  await expect(page.locator('[data-testid="jobs-form-dialog"]')).toBeVisible();

  // Customer picker (PrimeVue Select — click, then pick by visible label).
  await page.locator('[data-testid="job-customer-dropdown"]').click();
  await page.locator('li[role="option"]', { hasText: customerName }).first().click();

  await page.locator('[data-testid="job-title-input"]').fill(title);
}

test.describe('job create — a saved job never looks unsaved', () => {
  test('appointment ticked: dialog closes, job listed, warn not error', async ({ page, baseURL }) => {
    test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL/E2E_PASSWORD not set');
    const { api, token } = await login(baseURL);

    const custName = `E2E ApptFail Cust ${stamp}`;
    const cust = await api.post('/api/customers', {
      headers: { authorization: `Bearer ${token}` },
      data: { name: custName, phone: '5550009931' },
    });
    expect(cust.ok(), await cust.text()).toBeTruthy();

    await primeSession(page, token);

    const jobTitle = `E2E appt-fail job ${stamp}`;
    await openNewJob(page, { title: jobTitle, customerName: custName });

    // Tick "schedule an appointment" and give it a date — the path that used
    // to fire the doomed POST.
    await page.locator('[data-testid="new-job-appt-schedule-toggle"]').click();
    const dateInput = page.locator('[data-testid="new-job-appt-date"] input');
    await expect(dateInput).toBeVisible();
    await dateInput.fill('07/31/2026');
    await dateInput.press('Escape');
    await page.locator('[data-testid="new-job-appt-notes"]').fill('Call before arrival');

    // Record every request the save makes, so we can prove the doomed endpoint
    // is not touched — and, just as importantly, that no client-error report is
    // pumped into the CC tracker. useApi reports non-2xx from the transport
    // layer, *before* suppressErrorToast is consulted, so a guaranteed 422 here
    // would flood /api/feedback/client-error forever.
    const posted = [];
    page.on('request', (r) => {
      if (r.method() === 'POST') posted.push(new URL(r.url()).pathname);
    });

    await page.locator('[data-testid="job-submit-btn"]').click();

    // Read the toast FIRST — it auto-dismisses, well before the longer
    // dialog/table waits below would finish.
    const jobsToast = page.locator('[data-testid="jobs-toast"]');
    await expect(jobsToast).toContainText(/appointment/i, { timeout: 15000 });
    const tt = await jobsToast.innerText();
    expect(tt).toMatch(/on the calendar/i);
    // The confirmation names the job, so it is proof even when the row isn't
    // on the visible page.
    expect(tt).toMatch(/JOB-/);

    // No error-severity toast anywhere on screen — the previous version of this
    // assertion checked for the literal string "failed to save", which a red
    // `Error / Request failed (422)` toast would have sailed straight past.
    const errorToasts = await page.locator('.p-toast .p-toast-message-error').count();
    expect(errorToasts, 'no error toast may appear over a saved job').toBe(0);
    const allToasts = (await page.locator('.p-toast').allInnerTexts()).join('\n');
    expect(allToasts).not.toMatch(/"type"\s*:\s*"missing"/);

    expect(posted).toContain('/api/jobs');
    expect(posted, 'the doomed appointment POST must be gone').not.toContain('/api/appointments');
    expect(posted, 'and it must not be reporting itself as a client error').not.toContain(
      '/api/feedback/client-error',
    );

    // THE FIX: the dialog closes and the list refreshes, unconditionally.
    await expect(page.locator('[data-testid="jobs-form-dialog"]')).toBeHidden({ timeout: 20000 });

    // The list refreshed and the job is really there.
    await expect(page.locator('[data-testid="jobs-datatable"]')).toContainText(jobTitle, {
      timeout: 20000,
    });

    // The job carries the date, and the operator's note survived.
    const found = await api.get(`/api/jobs?search=${encodeURIComponent(jobTitle)}&page_size=5`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(found.ok(), await found.text()).toBeTruthy();
    const listBody = await found.json();
    const rows = Array.isArray(listBody) ? listBody : listBody?.data || listBody?.items || [];
    const mine = rows.find((j) => (j.title || '') === jobTitle);
    expect(mine, 'created job should be findable').toBeTruthy();
    expect(mine.scheduled_at, 'appointment date must reach scheduled_at').toBeTruthy();

    const detail = await api.get(`/api/jobs/${mine.id}`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(detail.ok(), await detail.text()).toBeTruthy();
    expect(
      (await detail.json()).notes || '',
      'the appointment note must survive (JobCreate had no notes field before this change)',
    ).toContain('Appointment note: Call before arrival');

    // THE POINT: a real appointment row exists, created by the backend sync.
    const appts = await api.get(`/api/appointments?job_id=${mine.id}`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(appts.ok(), await appts.text()).toBeTruthy();
    const apptBody = await appts.json();
    const apptRows = Array.isArray(apptBody) ? apptBody : apptBody?.data || apptBody?.items || [];
    expect(
      apptRows.filter((a) => String(a.job_id) === String(mine.id)).length,
      'the ticked appointment must actually be on the calendar',
    ).toBeGreaterThan(0);

    await api.dispose();
  });

  test('appointment unticked: clean create', async ({ page, baseURL }) => {
    test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL/E2E_PASSWORD not set');
    const { api, token } = await login(baseURL);

    const custName = `E2E CleanCreate Cust ${stamp}`;
    const cust = await api.post('/api/customers', {
      headers: { authorization: `Bearer ${token}` },
      data: { name: custName, phone: '5550009932' },
    });
    expect(cust.ok(), await cust.text()).toBeTruthy();

    await primeSession(page, token);

    const jobTitle = `E2E clean job ${stamp}`;
    await openNewJob(page, { title: jobTitle, customerName: custName });
    await page.locator('[data-testid="job-submit-btn"]').click();

    await expect(page.locator('[data-testid="jobs-form-dialog"]')).toBeHidden({ timeout: 20000 });

    // Deliberately NOT asserting the row is on screen. The list is ordered by
    // scheduled date with undated jobs last, so a dateless job lands on the
    // final page — a real discoverability gap, tracked separately. What must
    // hold is that the save is confirmed by name and the job really exists.
    const toastText = await page.locator('[data-testid="jobs-toast"]').innerText();
    expect(toastText).toMatch(/JOB-/);

    const found = await api.get(`/api/jobs?search=${encodeURIComponent(jobTitle)}&page_size=5`, {
      headers: { authorization: `Bearer ${token}` },
    });
    expect(found.ok(), await found.text()).toBeTruthy();
    const listBody = await found.json();
    const rows = Array.isArray(listBody) ? listBody : listBody?.data || listBody?.items || [];
    expect(rows.some((j) => (j.title || '') === jobTitle), 'job must exist').toBe(true);

    await api.dispose();
  });

  // Visual pass — the dialog in both themes, captured for a human to look at.
  for (const theme of ['light', 'dark']) {
    test(`new-job dialog renders in ${theme} mode`, async ({ page, baseURL }) => {
      test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL/E2E_PASSWORD not set');
      const { api, token } = await login(baseURL);
      await primeSession(page, token);
      await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);

      await page.goto('/jobs');
      await expect(page.locator('[data-testid="new-job-btn"]')).toBeVisible({ timeout: 20000 });
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(
        theme,
      );

      await page.locator('[data-testid="new-job-btn"]').click();
      await expect(page.locator('[data-testid="jobs-form-dialog"]')).toBeVisible();
      await page.locator('[data-testid="new-job-appt-schedule-toggle"]').click();
      await expect(page.locator('[data-testid="new-job-appt-date"]')).toBeVisible();

      await page.screenshot({ path: `e2e-artifacts/job-create-${theme}.png`, fullPage: false });
      await api.dispose();
    });
  }
});
