// Job pickers search the server (GDXA-1965).
//
// The Appointments "Linked Job" picker loaded /api/jobs?page_size=200 once and
// filtered in the browser, so a job past that page could not be linked. It and
// JobDetail's "Blocked by" picker now query /api/jobs?search= as the user
// types. This picks a live job OUTSIDE the picker's first page (row >= 50 of
// the default ordering) and finds it by its customer's name, which is not in
// the option label: Select's own client filter must not hide the server match.
//
// Skips when the stack holds too few jobs to put one past the first page.
import { test, expect } from './_fixtures.js';

const FIRST_PAGE = 50;
// "Blocked by" lists its first 200 rows untyped; its job must sit past them.
const DEPENDENCY_PAGE = 200;
const TERMINAL = new Set(['complete', 'completed', 'cancelled', 'paid', 'invoiced', 'failed']);

async function jobOutsideFirstPage(page, firstPage = FIRST_PAGE) {
  await page.goto('/appointments');
  const rows = await page.evaluate(async () => {
    const token = sessionStorage.getItem('gdx_access_token');
    const r = await fetch('/api/jobs?page_size=500', { headers: { authorization: `Bearer ${token}` } });
    const d = await r.json();
    return Array.isArray(d) ? d : d.items || [];
  });
  const candidates = rows.slice(firstPage).filter((j) =>
    j.job_number && j.customer_name && !TERMINAL.has(String(j.lifecycle_stage || j.status || '').toLowerCase())
    // The customer name must reach the server's LIKE intact and match few rows.
    && !(j.title || '').includes(j.customer_name),
  );
  return { total: rows.length, job: candidates[candidates.length - 1] || null };
}

test('Appointments: a job past the first page is found by customer name', async ({ page }) => {
  const { total, job } = await jobOutsideFirstPage(page);
  test.skip(!job, `needs a live job past row ${FIRST_PAGE} (stack has ${total})`);

  await expect(page.getByTestId('new-appointment')).toBeVisible({ timeout: 15000 });
  await page.getByTestId('new-appointment').click();
  await page.getByTestId('appointment-job-select').click();
  const search = page.locator('.p-select-overlay input.p-select-filter');
  await expect(search).toBeVisible();

  const searched = page.waitForRequest((req) => req.url().includes('/api/jobs?') && req.url().includes('search='));
  await search.fill(job.customer_name);
  await searched;
  const option = page.locator('.p-select-overlay .p-select-option', { hasText: job.job_number });
  await expect(option).toBeVisible({ timeout: 10000 });
  await option.click();
  await expect(page.getByTestId('appointment-job-select')).toContainText(job.job_number);
});

test('JobDetail Blocked by: a job past the first page is found by job number', async ({ page }) => {
  const { total, job } = await jobOutsideFirstPage(page, DEPENDENCY_PAGE);
  test.skip(!job, `needs a live job past row ${DEPENDENCY_PAGE} (stack has ${total})`);
  const host = await page.evaluate(async () => {
    const token = sessionStorage.getItem('gdx_access_token');
    const r = await fetch('/api/jobs?page_size=1', { headers: { authorization: `Bearer ${token}` } });
    const d = await r.json();
    return (Array.isArray(d) ? d : d.items || [])[0];
  });
  test.skip(!host || host.id === job.id, 'needs a second job to host the picker');

  await page.goto(`/jobs/${host.id}`);
  const select = page.getByTestId('dependency-select');
  await expect(select).toBeVisible({ timeout: 15000 });
  await select.click();
  const search = page.locator('.p-select-overlay input.p-select-filter');
  await search.fill(job.job_number);
  // Only the searched job's row can be listed: the search is its unique job number.
  const options = page.locator('.p-select-overlay .p-select-option');
  await expect(options).toHaveCount(1, { timeout: 10000 });
  await expect(options.first()).toContainText(job.title || job.job_number);
});
