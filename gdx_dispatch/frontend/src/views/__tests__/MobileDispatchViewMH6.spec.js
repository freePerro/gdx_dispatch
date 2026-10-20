/**
 * MH-6 — MobileDispatchView helper functions.
 *
 * Audit P1 #6: pre-fix the Unassigned cards rendered a badge of whatever
 * was in `job.status`. The audit caught values like "Service Call" (job
 * type) and "QB Import" (import source) appearing in the status slot.
 * P1 #7: ~33 of 38 "Unassigned" jobs were completed historical QB
 * imports — the actionable queue was unusable.
 *
 * The helper copies this file used to re-declare were deleted with
 * GDXA-1942: they had drifted from the view (which now reads the
 * /api/jobs labels through lib/jobStage), so they proved nothing about it.
 * MobileDispatchStatusLabels.spec.js mounts the real view for the badge
 * and the Unassigned filter.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { dateKeyInZone } from '../../composables/useTenantTimezone';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SFC_PATH = path.join(__dirname, '..', 'MobileDispatchView.vue');
const SRC = fs.readFileSync(SFC_PATH, 'utf8');

describe('MobileDispatchView — MH-6 source contract', () => {
  it('SFC declares isTerminal + uses it to filter unassignedJobs', () => {
    expect(SRC).toMatch(/function isTerminal/);
    expect(SRC).toMatch(/!isTerminal\(j\)/);
  });

  it('SFC unassigned-card Tag binds to statusBadgeValue, NOT raw job.status', () => {
    // The badge contract: non-canonical status (e.g. "Service Call",
    // "QB Import") must NOT pass through to the visible label.
    expect(SRC).toMatch(/:value="statusBadgeValue\(job\.status\)"/);
    // Negative check: the old `job.status || 'pending'` shape must
    // not still be in the unassigned-card section.
    const unassignedSection = SRC.split('Unassigned')[1]?.split('Tech sections')[0] || '';
    expect(unassignedSection).not.toMatch(/:value="job\.status \|\| 'pending'"/);
  });
});

// The board fetches the FULL job list (server ignores ?date=), so it must
// bucket to the selected day client-side or every tech column shows all-time
// jobs. This locks both that the SFC actually routes tech/unassigned through
// dayJobs, and the dayJobs predicate itself.
describe('MobileDispatchView — selected-day filtering', () => {
  it('SFC derives dayJobs and feeds tech columns + unassigned from it', () => {
    expect(SRC).toMatch(/const dayJobs\s*=\s*computed/);
    // tech grouping iterates dayJobs, not the raw jobs list
    expect(SRC).toMatch(/for \(const job of dayJobs\.value\)/);
    // unassigned queue is scoped to the day too
    expect(SRC).toMatch(/dayJobs\.value\.filter\(\(j\) => !j\.technician_id/);
  });

  // Mirror of dayJobs' predicate (office-tz day match; undated → today only),
  // exercised with the REAL dateKeyInZone so the office-tz math is the same.
  function dayFilter(jobs, key, isToday, tz) {
    return jobs.filter((j) =>
      j.scheduled_at ? dateKeyInZone(j.scheduled_at, tz) === key : isToday,
    );
  }

  const TZ = 'America/Chicago';

  it('keeps only jobs whose OFFICE-local day matches the selected day', () => {
    const jobs = [
      { id: 'a', scheduled_at: '2026-07-10T15:00:00Z' }, // 10am CDT → Jul 10
      { id: 'b', scheduled_at: '2026-07-11T01:00:00Z' }, // 8pm CDT Jul 10 → Jul 10
      { id: 'c', scheduled_at: '2026-07-11T14:00:00Z' }, // Jul 11
    ];
    const out = dayFilter(jobs, '2026-07-10', false, TZ);
    expect(out.map((j) => j.id)).toEqual(['a', 'b']); // NOT 'c'
  });

  it('shows an undated job only on today', () => {
    const jobs = [{ id: 'lead', scheduled_at: null }];
    expect(dayFilter(jobs, '2026-07-10', true, TZ).map((j) => j.id)).toEqual(['lead']);
    expect(dayFilter(jobs, '2026-07-10', false, TZ)).toEqual([]);
  });
});
