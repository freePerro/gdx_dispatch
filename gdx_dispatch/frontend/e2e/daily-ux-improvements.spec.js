/**
 * Browser verification for branch feat/daily-ux-improvements.
 *
 *  A. List filter/tab persistence (useListPrefs) — Jobs search + Billing status
 *     tab survive a full page reload.
 *  B. Dispatch board auto-refresh — board renders with no console errors and
 *     the manual Refresh button is not stuck/flickering a spinner.
 *  C. Customer dedup-at-entry — typing an identifier that matches an existing
 *     customer surfaces a non-blocking "Possible duplicate" warning.
 *
 * Run against a throwaway container serving the working-tree dist:
 *   E2E_BASE_URL=http://localhost:8003 E2E_TENANT_SLUG=<tid> \
 *   E2E_EMAIL=e2e_pw@gdx.internal E2E_PASSWORD=$(cat ../../../scratch_e2e/pw.txt) \
 *   node_modules/.bin/playwright test e2e/daily-ux-improvements.spec.js --retries=0
 */
import { test, expect, request as pwRequest } from "@playwright/test";

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;

async function authedPage(page, baseURL) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post("/auth/login", {
    headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-e2e-test": "true" },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), "login should succeed").toBeTruthy();
  const { access_token } = await r.json();
  await page.addInitScript(
    (a) => {
      sessionStorage.setItem("gdx_access_token", a.t);
      sessionStorage.setItem("gdx_tenant_slug", a.tid);
    },
    { t: access_token, tid: TENANT },
  );
  await api.dispose();
}

test("A1 — Jobs search persists across reload", async ({ page, baseURL }) => {
  await authedPage(page, baseURL);
  await page.goto("/jobs");
  const search = page.locator('[data-testid="jobs-search"]');
  await expect(search).toBeVisible({ timeout: 15000 });
  await search.fill("acme");
  // also pick a status tab to persist
  await page.locator('[data-testid="jobs-status-scheduled"]').click();
  await page.waitForTimeout(300); // let the persist watcher write

  await page.reload();
  await expect(page.locator('[data-testid="jobs-search"]')).toHaveValue("acme", { timeout: 15000 });
  const stored = await page.evaluate(() => localStorage.getItem("gdx.listprefs.jobs"));
  expect(JSON.parse(stored)).toMatchObject({ activeStatus: "Scheduled", searchQuery: "acme" });
});

test("A2 — Billing status tab persists across reload", async ({ page, baseURL }) => {
  await authedPage(page, baseURL);
  await page.goto("/billing");
  await expect(page.locator('[data-testid="billing-status-paid"]')).toBeVisible({ timeout: 15000 });
  await page.locator('[data-testid="billing-status-paid"]').click();
  await page.waitForTimeout(300);

  await page.reload();
  await expect(page.locator('[data-testid="billing-status-paid"]')).toBeVisible({ timeout: 15000 });
  const stored = await page.evaluate(() => localStorage.getItem("gdx.listprefs.billing"));
  expect(JSON.parse(stored)).toMatchObject({ activeStatus: "Paid" });
});

test("B — Dispatch board renders cleanly; Refresh button not stuck spinning", async ({ page, baseURL }) => {
  const errors = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(String(e)));

  await authedPage(page, baseURL);
  await page.goto("/dispatch");
  const refresh = page.locator('[data-testid="dispatch-refresh-btn"]');
  await expect(refresh).toBeVisible({ timeout: 15000 });
  // Let the board settle; the auto-poll must NOT toggle the manual button's
  // loading spinner.
  await page.waitForTimeout(1500);
  await expect(refresh).not.toHaveClass(/p-button-loading/);

  // Ignore benign third-party noise (e.g. missing Google Maps key in dev).
  const real = errors.filter(
    (e) => !/Maps|google|favicon|ResizeObserver|manifest/i.test(e),
  );
  expect(real, `unexpected console errors: ${real.join(" | ")}`).toHaveLength(0);
});

test("C — Customer dedup warning appears and is non-blocking", async ({ page, baseURL }) => {
  await authedPage(page, baseURL);
  await page.goto("/customers");
  await page.locator('[data-testid="new-customer-btn"]').click();
  await expect(page.locator('[data-testid="customer-form-dialog"]')).toBeVisible({ timeout: 15000 });

  await page.locator('[data-testid="customer-name-input"]').fill("ZZ Dedup Test");
  await page.locator('[data-testid="customer-phone-input"]').fill("(555) 867-5309");

  const warning = page.locator('[data-testid="customer-dup-warning"]');
  await expect(warning).toBeVisible({ timeout: 10000 });
  await expect(warning).toContainText("ZZ Dedup Test");

  // Non-blocking: the submit button must remain enabled.
  await expect(page.locator('[data-testid="customer-submit-btn"]')).toBeEnabled();
});

// Visual capture of the dedup warning in light + dark mode for human review of
// contrast (theme CSS vars vs hardcoded fallbacks). Not committed-critical; gated
// behind SHOT_DIR so the normal suite skips file writes.
test("C-visual — dedup warning light + dark screenshots", async ({ page, baseURL }) => {
  test.skip(!process.env.SHOT_DIR, "set SHOT_DIR to capture screenshots");
  const dir = process.env.SHOT_DIR;
  await authedPage(page, baseURL);

  for (const theme of ["light", "dark"]) {
    await page.addInitScript((t) => localStorage.setItem("gdx_theme", t), theme);
    await page.goto("/customers");
    await page.locator('[data-testid="new-customer-btn"]').click();
    await expect(page.locator('[data-testid="customer-form-dialog"]')).toBeVisible({ timeout: 15000 });
    await page.locator('[data-testid="customer-name-input"]').fill("ZZ Dedup Test");
    await page.locator('[data-testid="customer-phone-input"]').fill("(555) 867-5309");
    await expect(page.locator('[data-testid="customer-dup-warning"]')).toBeVisible({ timeout: 10000 });
    expect(await page.evaluate(() => document.documentElement.getAttribute("data-theme"))).toBe(theme);
    await page.screenshot({ path: `${dir}/dedup-${theme}.png`, fullPage: false });
  }
});
