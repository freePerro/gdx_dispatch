/**
 * Tests for utility CSS rules in base.css — pins the contracts that
 * other components rely on. These rules don't have a JS counterpart so
 * a direct CSS-rule-presence check is the cheapest regression guard.
 *
 * Closes test gap from S110b (clickable-rows utility) and S112
 * (Driver.js mobile tour overrides).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE_CSS = readFileSync(
  resolve(__dirname, '..', 'assets', 'base.css'),
  'utf8',
);

describe('base.css utility rules', () => {
  describe('.clickable-rows utility (S110b)', () => {
    it('declares cursor:pointer on data cells', () => {
      // Check the rule exists. Using regex so whitespace formatting
      // doesn't break the test on autoformatter changes.
      const rule = /\.clickable-rows\s+\.p-datatable-tbody\s*>\s*tr\s*>\s*td\s*\{[^}]*cursor:\s*pointer/;
      expect(BASE_CSS).toMatch(rule);
    });

    it('declares hover background on body rows', () => {
      const rule = /\.clickable-rows\s+\.p-datatable-tbody\s*>\s*tr:hover\s*\{[^}]*background:/;
      expect(BASE_CSS).toMatch(rule);
    });

    it('does NOT apply cursor:pointer to header cells (sorting only)', () => {
      // Negative-test: the rule must scope to td (data cells), not th
      // (column headers — they're for sort, not navigation).
      const overscoped = /\.clickable-rows\s+\.p-datatable-tbody\s*>\s*tr\s*>\s*th[^{]*\{[^}]*cursor:\s*pointer/;
      expect(BASE_CSS).not.toMatch(overscoped);
    });
  });

  describe('.gdx-tour-mobile Driver.js overrides (S112)', () => {
    it('forces close button to ≥44px (Apple HIG minimum)', () => {
      const rule = /\.gdx-tour-mobile\s+\.driver-popover-close-btn\s*\{[^}]*min-width:\s*44px[^}]*min-height:\s*44px/s;
      expect(BASE_CSS).toMatch(rule);
    });

    it('forces nav buttons to ≥44px height', () => {
      const rule = /\.gdx-tour-mobile\s+\.driver-popover-navigation-btns\s+button\s*\{[^}]*min-height:\s*44px/s;
      expect(BASE_CSS).toMatch(rule);
    });

    // MH-3: tour popover must not overlap the bottom nav. Audit P1 #13.
    it('pushes the mobile tour popover above the bottom nav', () => {
      // Looks for a rule that bumps margin-bottom by `--bottom-nav-height`
      // (+ safe-area + a small gap). The variable read is the load-
      // bearing piece — if a future refactor changes the var name we
      // want this test to fail.
      const rule = /\.gdx-tour-mobile\.driver-popover\s*\{[^}]*margin-bottom:[^;]*var\(--bottom-nav-height[^)]*\)[^;]*!important/s;
      expect(BASE_CSS).toMatch(rule);
    });

    it('accounts for iOS safe-area-inset-bottom in the tour offset', () => {
      const rule = /\.gdx-tour-mobile\.driver-popover\s*\{[^}]*safe-area-inset-bottom/s;
      expect(BASE_CSS).toMatch(rule);
    });
  });
});
