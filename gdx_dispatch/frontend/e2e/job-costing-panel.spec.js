// #653 / #455 — the Job Costing "Cost breakdown" dialog, in a real browser.
//
// Every write control in this dialog was wired to an endpoint that does not
// exist (GET/POST /api/jobs/{id}/parts 404, DELETE 405, PATCH 501, PATCH
// /api/jobs/{id}/costing 405), and the `|| []` on the read made the 404 look
// identical to "this job has no parts". It is now a read-only report over
// /api/costing/jobs/{id}.
//
// Unit tests mount the component; this asserts the SHIPPED bundle against the
// real API — including that the dead endpoints are not called and that the
// dialog is readable in both themes. Skips cleanly when env is unset.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG || '';
const EMAIL = process.env.E2E_EMAIL || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const JOB_ID = process.env.E2E_COSTING_JOB_ID || '';

let cachedToken = null;

async function login(baseURL) {
  const api = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
  });
  if (!cachedToken) {
    const r = await api.post('/auth/login', { data: { email: EMAIL, password: PASSWORD } });
    expect(r.ok(), `login: ${r.status()}`).toBeTruthy();
    cachedToken = (await r.json()).access_token;
  }
  return { api, token: cachedToken };
}

test.describe('#653 job costing panel', () => {
  test.beforeEach(() => {
    test.skip(!EMAIL || !PASSWORD || !JOB_ID, 'env not set');
  });

  for (const theme of ['light', 'dark']) {
    test(`the cost breakdown reads from the costing engine — ${theme}`, async ({ page, baseURL }) => {
      const { api, token } = await login(baseURL);

      // Record every request the page makes, so "it no longer calls the dead
      // endpoint" is proven against the shipped bundle rather than the source.
      const requested = [];
      page.on('request', (r) => requested.push(`${r.method()} ${new URL(r.url()).pathname}`));

      await page.addInitScript((t) => sessionStorage.setItem('gdx_access_token', t), token);
      await page.addInitScript((t) => localStorage.setItem('gdx_theme', t), theme);

      await page.goto('/job-costing');
      // Open the dialog the way a user does.
      const details = page.getByRole('button', { name: /details/i }).first();
      await expect(details).toBeVisible({ timeout: 20000 });
      await details.click();

      // Wait on the dialog, not on a parts table — there deliberately isn't one.
      await expect(page.locator('[data-testid="your-cost-card"]')).toBeVisible({
        timeout: 15000,
      });

      // Compare against the job the UI actually opened, not a job id from env.
      // Clicking "the first row" is what a user does; asserting against a
      // different job would either pass vacuously or fail for the wrong reason.
      const heading = await page.locator('.job-detail-header h3').innerText();
      const openedId = heading.replace(/^Job\s+/, '').trim();
      expect(openedId, 'the dialog must name the job it opened').toBeTruthy();
      const r = await api.get(`/api/costing/jobs/${openedId}`, {
        headers: { authorization: `Bearer ${token}` },
      });
      expect(r.ok(), await r.text()).toBeTruthy();
      const costing = await r.json();

      // The dead endpoints are never requested.
      const dead = requested.filter((u) => /\/api\/jobs\/[^/]+\/(parts|costing)$/.test(u));
      expect(dead, `dead endpoints called: ${dead.join(', ')}`).toHaveLength(0);
      // ...and the live one is, so this is not vacuous.
      expect(requested.some((u) => u.includes('/api/costing/jobs/'))).toBe(true);

      // No control that cannot save survived.
      for (const id of ['add-part-btn', 'add-item-btn', 'save-costing-btn']) {
        await expect(page.locator(`[data-testid="${id}"]`)).toHaveCount(0);
      }

      // No second parts table, and the pointer to the real one is there.
      await expect(page.locator('[data-testid="parts-table"]')).toHaveCount(0);
      await expect(page.locator('[data-testid="jc-parts-pointer"]')).toBeVisible();
      await expect(page.locator('[data-testid="jc-edit-parts-on-job"]')).toBeVisible();

      // "Your Cost" is the server's total_cost, to the cent.
      const shown = await page.locator('[data-testid="your-cost-card"]').innerText();
      const expected = new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
      }).format(costing.total_cost ?? 0);
      expect(shown, `card should show ${expected}`).toContain(expected);

      // Wait for the dialog's fade-in before capturing. Screenshotting
      // mid-transition produced an artifact that looked exactly like a
      // translucent-panel bug; the panel measures solid before and after.
      await expect
        .poll(
          async () =>
            page.evaluate(() => {
              const el = document.querySelector('.p-dialog');
              return el ? getComputedStyle(el).opacity : '0';
            }),
          { timeout: 5000 },
        )
        .toBe('1');
      await page.screenshot({ path: `e2e-artifacts/job-costing-${theme}.png` });

      // The pointer must be READABLE, not merely present — it is the only route
      // to the parts detail. Asserting "colour != body background" would pass
      // unconditionally; this asserts the class actually resolved to a rule, so
      // it goes red if `.section-note` is dropped from the stylesheet.
      const note = await page.evaluate(() => {
        const el = document.querySelector('[data-testid="jc-parts-pointer"]');
        if (!el) return null;
        const cs = getComputedStyle(el);
        return { size: cs.fontSize, color: cs.color, lineHeight: cs.lineHeight };
      });
      expect(note, 'the parts pointer must render').toBeTruthy();
      // 0.85rem from .section-note. Unstyled it would inherit the 1rem body size.
      expect(parseFloat(note.size)).toBeLessThan(16);
      expect(parseFloat(note.size)).toBeGreaterThan(10);

      await api.dispose();
    });
  }
});
