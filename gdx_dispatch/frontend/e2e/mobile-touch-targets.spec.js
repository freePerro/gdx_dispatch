// Mobile touch-target audit — closes D-S112-mobile-touch-target-test.
//
// Walk every /mobile/* route at iPhone X viewport (375×812) and assert no
// clickable element (<button>, <a>, [role="button"], <input>) has a
// rendered rect smaller than the 44×44 px Apple HIG / 48×48 dp Material
// minimum. We use 44 as the floor (Apple is stricter on iPhones, which is
// where the field techs run GDX).
//
// Allowlist: driver.js onboarding popover (separate styling pass; fix in
// base.css `.gdx-tour-mobile`), and elements explicitly marked
// data-test-skip-tap-audit (none today, future opt-out).
//
// This is a regression test for the S112 mobile audit, where the
// MobilePlannerView checkbox was 20×20 (half the minimum) for months
// without anyone catching it.

import { test, expect } from './_fixtures.js';

const VIEWPORT = { width: 375, height: 812 };
const MIN_SIZE = 44;
const ALLOWLIST_SELECTORS = [
  '.driver-popover',                // onboarding tour — own CSS pass
  '[data-test-skip-tap-audit]',     // explicit per-element opt-out
];

const MOBILE_ROUTES = [
  '/mobile',
  '/mobile/jobs',
  '/mobile/summary',
  '/mobile/dispatch',
  '/mobile/planner',
  '/mobile/customers',
  '/mobile/inbox',
  '/mobile/estimates',
  '/mobile/billing',
  '/mobile/inventory',
  '/mobile/parts-to-order',
];

for (const route of MOBILE_ROUTES) {
  test(`mobile tap targets ≥ ${MIN_SIZE}px on ${route}`, async ({ page }) => {
    await page.setViewportSize(VIEWPORT);
    await page.goto(route, { waitUntil: 'networkidle' });
    // Dismiss any active driver.js tour so its known-already-flagged
    // popover doesn't pollute the audit.
    await page
      .locator('.driver-popover-close-btn')
      .first()
      .click({ timeout: 500 })
      .catch(() => {});
    await page.waitForTimeout(500);

    const tooSmall = await page.evaluate(
      ({ minSize, allowlistSelectors }) => {
        const allowed = new Set();
        for (const sel of allowlistSelectors) {
          for (const el of document.querySelectorAll(sel)) {
            // mark this element AND all its descendants as allowed
            allowed.add(el);
            for (const d of el.querySelectorAll('*')) allowed.add(d);
          }
        }
        const clickables = document.querySelectorAll(
          'button:not([disabled]), a[href], [role="button"], input:not([type="hidden"])'
        );
        const failures = [];
        for (const el of clickables) {
          if (allowed.has(el)) continue;
          const r = el.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue; // hidden — skip
          if (r.width < minSize || r.height < minSize) {
            failures.push({
              tag: el.tagName,
              text: (el.innerText || el.getAttribute('aria-label') || '').slice(0, 40),
              cls: (el.className?.toString?.() || '').slice(0, 60),
              w: Math.round(r.width),
              h: Math.round(r.height),
            });
          }
        }
        return failures;
      },
      { minSize: MIN_SIZE, allowlistSelectors: ALLOWLIST_SELECTORS }
    );

    if (tooSmall.length > 0) {
      const lines = tooSmall.map((f) => `  - ${f.tag} "${f.text}" (${f.w}x${f.h}) class="${f.cls}"`);
      throw new Error(
        `[${route}] ${tooSmall.length} touch target(s) below ${MIN_SIZE}px:\n${lines.join('\n')}`
      );
    }
    expect(tooSmall.length).toBe(0);
  });
}
