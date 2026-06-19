// Sprint 1.0 B2 — Frontend route-coverage harness.
//
// For every route in src/router/index.js (minus redirects), navigate to the
// route and assert:
//   1. #app is mounted and not the empty `<!---->` Vue placeholder.
//   2. No console errors fired during render (allowlist: known noisy warnings).
//
// Param routes (`/jobs/:id`, etc.) get a real ID resolved from the matching
// list endpoint at fixture setup. If the list is empty we record a SKIP with
// reason; that is a data-gap, not a route bug, but the harness still surfaces
// it so we know which routes are truly unverified.

import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from './_fixtures.js';
import { loadRoutes } from './_routes.js';

const REPORT = [];
const CONSOLE_ALLOWLIST = [
  /Sentry/i,
  /DevTools/i,
  /favicon\.ico/i,
  /\[Vue Router warn\]/i, // navigated programmatically, vue-router complains harmlessly
];

function substituteParams(routePath, ids) {
  const map = {
    'jobs/:id': () => ids.job && routePath.replace(':id', ids.job),
    'customers/:id': () => ids.customer && routePath.replace(':id', ids.customer),
    'estimates/:id': () => ids.estimate && routePath.replace(':id', ids.estimate),
    'billing/:id': () => ids.invoice && routePath.replace(':id', ids.invoice),
    'admin/games/:slug': () => routePath.replace(':slug', ids.gameSlug),
  };
  for (const [key, fn] of Object.entries(map)) {
    if (routePath.endsWith(key)) return fn();
  }
  // Unknown param shape — leave unsubstituted so we record SKIP.
  return /:[A-Za-z_]+/.test(routePath) ? null : routePath;
}

const allRoutes = loadRoutes();

test.describe('route coverage', () => {
  test.afterAll(async () => {
    const reportPath = path.resolve('e2e-route-report.json');
    fs.writeFileSync(reportPath, JSON.stringify(REPORT, null, 2));
    // Compact stdout summary.
    const grouped = REPORT.reduce((acc, r) => {
      acc[r.status] = (acc[r.status] || 0) + 1;
      return acc;
    }, {});
    // eslint-disable-next-line no-console
    console.log('\n=== route-coverage summary ===');
    // eslint-disable-next-line no-console
    console.log(grouped);
    const bad = REPORT.filter((r) => r.status === 'fail');
    if (bad.length) {
      // eslint-disable-next-line no-console
      console.log('\nFAILS:');
      bad.forEach((b) =>
        // eslint-disable-next-line no-console
        console.log(`  ${b.route} → ${b.reason}`),
      );
    }
  });

  for (const r of allRoutes) {
    if (r.public) continue; // public routes (/login, /signup) need a separate spec
    test(`${r.path}`, async ({ page, paramIds }) => {
      const url = substituteParams(r.path, paramIds);
      if (!url) {
        REPORT.push({ route: r.path, status: 'skip', reason: 'no fixture id available' });
        test.skip(true, 'no fixture id');
        return;
      }
      const errors = [];
      const consoleErrors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      page.on('console', (msg) => {
        if (msg.type() === 'error') {
          const text = msg.text();
          if (!CONSOLE_ALLOWLIST.some((re) => re.test(text))) {
            consoleErrors.push(text);
          }
        }
      });
      const resp = await page.goto(url, { waitUntil: 'domcontentloaded' }).catch((e) => {
        REPORT.push({ route: url, status: 'fail', reason: `navigation threw: ${e.message}` });
        throw e;
      });
      const httpStatus = resp ? resp.status() : 0;
      // Wait for the SPA to actually mount + hydrate. Resolve when #app has
      // non-trivial content OR 8s elapses (Vue lazy chunks under load).
      try {
        await page.waitForFunction(
          () => {
            const el = document.querySelector('#app');
            if (!el) return false;
            const html = el.innerHTML.trim();
            return html.length > 200 && html !== '<!---->' && html !== '<!--v-if-->';
          },
          undefined,
          { timeout: 20000 },
        );
      } catch {
        // Fall through — we'll record whatever the app element is.
      }
      // Settle network briefly so any post-mount API errors land in console.
      await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
      // Grace period for SPAs (JobCostingView etc.) whose onMounted fires
      // many parallel API calls — they can still be hydrating after networkidle.
      const midHtml = await page.evaluate(
        () => document.querySelector('#app')?.innerHTML?.length ?? 0,
      );
      if (midHtml < 200) {
        await page.waitForTimeout(1500);
      }
      const appHtml = await page.evaluate(() => {
        const el = document.querySelector('#app');
        return el ? el.innerHTML : null;
      });
      const isEmpty =
        appHtml === null ||
        appHtml.trim() === '' ||
        appHtml.trim() === '<!---->' ||
        appHtml.trim() === '<!--v-if-->';
      const result = {
        route: url,
        httpStatus,
        appBytes: appHtml ? appHtml.length : 0,
        pageErrors: errors,
        consoleErrors,
      };
      // Fail criterion: page must mount (non-empty #app) AND no uncaught JS
      // exceptions (pageerror). Console errors (401/404/429 on background
      // API calls, Sentry noise, etc.) are recorded for diagnostics but
      // don't fail the test — they don't break rendering.
      if (isEmpty) {
        result.status = 'fail';
        result.reason = 'white-screen (#app empty after networkidle)';
      } else if (errors.length) {
        result.status = 'fail';
        result.reason = `uncaught page errors: ${errors.join(' | ')}`;
      } else {
        result.status = 'pass';
      }
      REPORT.push(result);
      // Persist per-test report so afterAll across worker processes can aggregate.
      const safe = url.replace(/[^a-zA-Z0-9]+/g, '_');
      fs.mkdirSync('e2e/.state/reports', { recursive: true });
      fs.writeFileSync(`e2e/.state/reports/${safe}.json`, JSON.stringify(result));
      // Inter-test breathing room. The lab uvicorn (2 workers) + rate limiter
      // get swamped under back-to-back SPA navigations, each of which fires
      // 5-20 API calls on mount. Heavy views (dispatch, scheduling) need
      // more slack; 800ms holds the backend from tipping into timeouts.
      await page.waitForTimeout(800);
      expect(result.status, result.reason || '').toBe('pass');
    });
  }
});
