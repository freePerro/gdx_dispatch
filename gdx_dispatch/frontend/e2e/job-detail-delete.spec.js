/**
 * Delete a job from the job page — browser proof.
 *
 * Doug 2026-08-11: "there is no way of deleting a job when in the job page."
 * The endpoint always existed; only the Jobs LIST row actions and the
 * Ready-for-Billing queue called it. This walks the verb where he looked for
 * it: open the job, click Delete, answer the confirm, land back on the list
 * with the job gone.
 *
 * The confirm is the part that needs a real browser. It runs through
 * useDestructiveConfirm → PrimeVue's confirm service → the single
 * <ConfirmDialog/> mounted in AppLayout. That chain silently auto-accepted for
 * months (issue #215) and a unit test with a mocked composable cannot see it —
 * only a rendered dialog proves the click is actually gated.
 *
 * Creates its own job over the API, so it never deletes anything real.
 */
import { test, expect, request as pwRequest } from "@playwright/test";

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

// One login for the whole file. Logging in per test trips the rate limiter on
// the second call and fails the run for a reason that has nothing to do with
// the feature under test.
let cachedToken = null;

async function apiContext(baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  if (!cachedToken) {
    const r = await api.post("/auth/login", {
      headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-e2e-test": "true" },
      data: { email: EMAIL, password: PASSWORD },
    });
    expect(r.ok(), `login failed: ${r.status()}`).toBeTruthy();
    cachedToken = (await r.json()).access_token;
  }
  return { api, token: cachedToken };
}

const authHeaders = (token) => ({
  authorization: `Bearer ${token}`,
  "x-tenant-id": TENANT,
  "content-type": "application/json",
});

test("Delete on the job page removes the job and returns to the list", async ({ page, baseURL }) => {
  const { api, token } = await apiContext(baseURL);

  const created = await api.post("/api/jobs", {
    headers: authHeaders(token),
    data: { title: "E2E delete-verb walk", job_type: "Service Call", priority: "Normal", status: "Scheduled" },
  });
  expect(created.ok()).toBeTruthy();
  const job = await created.json();

  await page.addInitScript(
    (a) => {
      sessionStorage.setItem("gdx_access_token", a.t);
      sessionStorage.setItem("gdx_tenant_slug", a.tid);
      localStorage.setItem("gdx_theme", a.theme);
    },
    { t: token, tid: TENANT, theme: "light" },
  );

  await page.goto(`/jobs/${job.id}`);

  // 1. The verb is on the job page at all.
  const del = page.locator('[data-testid="job-detail-delete"]');
  await expect(del).toBeVisible({ timeout: 20_000 });
  await page.screenshot({ path: "test-results/job-delete-light.png", fullPage: false });

  // 2. Clicking opens a REAL dialog — this is the #215 guard.
  await del.click();
  const dialog = page.locator(".p-confirmdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("Delete this job?");
  await expect(dialog).toContainText(job.job_number);
  await page.screenshot({ path: "test-results/job-delete-confirm-light.png", fullPage: false });

  // 3. Cancel deletes nothing.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(dialog).toBeHidden();
  const stillThere = await api.get(`/api/jobs/${job.id}`, { headers: authHeaders(token) });
  expect(stillThere.status()).toBe(200);

  // 4. Confirm deletes and lands back on the list.
  await del.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Delete job" }).click();
  await expect(page).toHaveURL(/\/jobs$/, { timeout: 20_000 });

  // 5. The job is actually gone, not just navigated away from.
  const after = await api.get(`/api/jobs/${job.id}`, { headers: authHeaders(token) });
  expect(after.status()).toBe(404);
  await expect(page.locator('[data-testid="jobs-datatable"]')).not.toContainText(job.job_number);

  await api.dispose();
});

test("the Delete verb and its dialog are legible in dark mode", async ({ page, baseURL }) => {
  const { api, token } = await apiContext(baseURL);
  const created = await api.post("/api/jobs", {
    headers: authHeaders(token),
    data: { title: "E2E delete-verb dark walk", job_type: "Service Call", priority: "Normal", status: "Scheduled" },
  });
  const job = await created.json();

  await page.addInitScript(
    (a) => {
      sessionStorage.setItem("gdx_access_token", a.t);
      sessionStorage.setItem("gdx_tenant_slug", a.tid);
      localStorage.setItem("gdx_theme", "dark");
    },
    { t: token, tid: TENANT },
  );

  await page.goto(`/jobs/${job.id}`);
  expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe("dark");

  const del = page.locator('[data-testid="job-detail-delete"]');
  await expect(del).toBeVisible({ timeout: 20_000 });
  await page.screenshot({ path: "test-results/job-delete-dark.png", fullPage: false });

  await del.click();
  const dialog = page.locator(".p-confirmdialog");
  await expect(dialog).toBeVisible();
  await page.screenshot({ path: "test-results/job-delete-confirm-dark.png", fullPage: false });

  // Clean up the probe job over the API rather than through the UI — this test
  // is about the look, not a second delete walk.
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await api.delete(`/api/jobs/${job.id}`, { headers: authHeaders(token) });
  await api.dispose();
});
