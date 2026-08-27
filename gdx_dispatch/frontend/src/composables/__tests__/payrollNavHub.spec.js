/**
 * Payroll owns the hours (2026-08-27).
 *
 * Timeclock and Timesheets moved out of Operations into the Payroll
 * category, collapsed into one sidebar row with four tabs. Doug: "i think
 * timeclock and timesheets should live under payroll."
 *
 * The failures these pin — all of them things a nav move breaks quietly:
 *
 *  - A route changing. Every tab keeps its ORIGINAL absolute path, because
 *    three things already point at them: bookmarks, Dispatch's "Fix
 *    timeclock" deep link (?entry=&on=), and the `timesheet` bell
 *    notification added in v1.105.0. A nav tidy-up that breaks a
 *    notification is a worse trade than an untidy sidebar.
 *  - The hub landing on a tab the viewer cannot read. collapseClusters
 *    targets the first VISIBLE child, so this has to hold for a user who
 *    lacks scheduling.write too.
 *  - The tech redirect guard, which matches `to.path === '/timeclock'`
 *    literally and would silently stop redirecting if the path moved.
 *  - Timeclock quietly acquiring a permission gate. It is ungated on
 *    purpose: it is a person's own clock.
 */
import { describe, it, expect } from 'vitest';
import { MODULE_CATEGORIES, NAV_CLUSTERS, clusterByKey } from '../../constants/modules';
import { collapseClusters } from '../useTenantModules';

const payroll = MODULE_CATEGORIES.find((c) => c.key === 'payroll_comp');
const operations = MODULE_CATEGORIES.find((c) => c.key === 'operations');
const byKey = (cat, key) => cat.modules.find((m) => m.key === key);

describe('Payroll nav hub', () => {
  it('holds the hours, the clock, pay runs and commissions', () => {
    expect(payroll.modules.map((m) => m.key)).toEqual([
      'timesheets', 'timeclock', 'payroll', 'commissions',
    ]);
  });

  it('no longer lists them under Operations', () => {
    expect(byKey(operations, 'timeclock')).toBeUndefined();
    expect(byKey(operations, 'timesheets')).toBeUndefined();
  });

  it('keeps every route exactly where it was', () => {
    // Bookmarks, the Dispatch deep link and the bell notification all
    // resolve by PATH. Changing one is the actual cost of this move, so it
    // is the thing asserted hardest.
    expect(byKey(payroll, 'timesheets').to).toBe('/timesheets');
    expect(byKey(payroll, 'timeclock').to).toBe('/timeclock');
    expect(byKey(payroll, 'payroll').to).toBe('/payroll');
    expect(byKey(payroll, 'commissions').to).toBe('/commissions');
  });

  it('registers a cluster so the tab bar has a label to render', () => {
    const cluster = clusterByKey('payroll_hub');
    expect(cluster).toBeTruthy();
    expect(cluster.label).toBe('Payroll');
    expect(NAV_CLUSTERS.filter((c) => c.key === 'payroll_hub')).toHaveLength(1);
  });

  it('collapses to ONE sidebar row that opens on Timesheets', () => {
    const rows = collapseClusters(payroll.modules);
    expect(rows).toHaveLength(1);
    expect(rows[0].clusterHub).toBe('payroll_hub');
    expect(rows[0].to).toBe('/timesheets');
  });

  it('highlights the row from any of its four tabs', () => {
    const [row] = collapseClusters(payroll.modules);
    expect(row.matchPaths).toEqual(
      ['/timesheets', '/timeclock', '/payroll', '/commissions'],
    );
  });

  it('lands a viewer without scheduling.write somewhere they can read', () => {
    // collapseClusters runs on the ALREADY permission-filtered list, so
    // simulate that: drop Timesheets and the hub must fall to Time Clock,
    // not point at a page that 403s.
    const filtered = payroll.modules.filter((m) => m.key !== 'timesheets');
    const [row] = collapseClusters(filtered);
    expect(row.to).toBe('/timeclock');
  });

  it('leaves the clock ungated — it is a person\'s own time', () => {
    expect(byKey(payroll, 'timeclock').permission).toBeUndefined();
  });

  it('keeps the office timesheet behind the gate that matches the API', () => {
    // scheduling.write, not a .read key: `viewer` holds every .read key but
    // fails the backend's is_dispatch_manager gate, so a read key here shows
    // a nav entry whose every call 403s.
    expect(byKey(payroll, 'timesheets').permission).toBe('scheduling.write');
    expect(byKey(payroll, 'timesheets').requires).toBe('timeclock');
  });

  it('gives every tab a label short enough for a tab bar', () => {
    for (const m of payroll.modules) {
      expect(m.tabLabel, `${m.key} needs a tabLabel`).toBeTruthy();
      expect(m.tabLabel.length).toBeLessThanOrEqual(14);
    }
  });
});
