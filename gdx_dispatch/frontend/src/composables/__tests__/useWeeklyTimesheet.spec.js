/**
 * useWeeklyTimesheet — the week logic under BOTH tech timesheet surfaces
 * (desktop card + mobile section). Pins the rules the old TimeclockView week
 * card broke, all inherited from the office /timesheets page:
 *
 *  - Monday-based weeks from the SHOP's today (the old card was Sunday-based
 *    and browser-local);
 *  - shop-day bucketing across the UTC evening boundary (the real 2-of-39
 *    prod case: 10:02 PM Central is the NEXT UTC calendar day);
 *  - worked = minutes − break_minutes (the old card's break column filtered
 *    an entry_type the backend never writes — ALWAYS 0.00);
 *  - the fetch opts into include_breaks and asks a day of slack each side;
 *  - no browsing into future weeks; future days not rendered;
 *  - the self-edit affordance window mirrors the server's.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { ref } from 'vue';

const apiGet = vi.fn(() => Promise.resolve([]));

vi.mock('../useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));

const tenantTz = ref('America/Chicago');
vi.mock('../useTenantTimezone', async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ...actual,
    useTenantTimezone: () => ({
      tenantTimezone: tenantTz,
      ensureLoaded: () => Promise.resolve(tenantTz.value),
      zonedDateKey: (v) => actual.dateKeyInZone(v, tenantTz.value),
    }),
  };
});

import { useWeeklyTimesheet, SELF_EDIT_WINDOW_DAYS } from '../useWeeklyTimesheet';

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockResolvedValue([]);
  tenantTz.value = 'America/Chicago';
});

// A fixed PAST week (Mon Apr 27 – Sun May 3, 2026) so the future-day skip
// can't interfere and the Chicago fixtures are deterministic in any runner tz.
const PAST_MONDAY = new Date(2026, 3, 27);

async function pastWeek(entries) {
  apiGet.mockResolvedValue(entries);
  const ts = useWeeklyTimesheet();
  ts.weekStart.value = new Date(PAST_MONDAY);
  await ts.reload();
  return ts;
}

describe('useWeeklyTimesheet — the week itself', () => {
  it('starts the week on the shop Monday containing today', async () => {
    const ts = useWeeklyTimesheet();
    await ts.init();
    expect(ts.weekStart.value.getDay()).toBe(1); // Monday
    expect(ts.isCurrentWeek.value).toBe(true);
    expect(ts.canGoNext.value).toBe(false);
  });

  it('asks the server for slack + breaks', async () => {
    await pastWeek([]);
    const call = apiGet.mock.calls.find((c) => c[0].startsWith('/api/timeclock/entries'));
    expect(call[0]).toContain('include_breaks=true');
    expect(call[0]).toContain('date_start=2026-04-26'); // Monday − 1
    expect(call[0]).toContain('date_end=2026-05-04');   // Sunday + 1
  });

  it('never navigates past the current week', async () => {
    const ts = useWeeklyTimesheet();
    await ts.init();
    const start = ts.weekStart.value.getTime();
    await ts.nextWeek();                      // no-op at the current week
    expect(ts.weekStart.value.getTime()).toBe(start);

    await ts.prevWeek();
    expect(ts.canGoNext.value).toBe(true);
    await ts.nextWeek();
    expect(ts.weekStart.value.getTime()).toBe(start);
  });

  it('renders no future days', async () => {
    const ts = useWeeklyTimesheet();
    await ts.init();
    const todayKey = ts.days.value.at(-1)?.key;
    expect(ts.days.value.length).toBeGreaterThanOrEqual(1);
    expect(ts.days.value.every((d) => d.key <= todayKey)).toBe(true);
    expect(ts.days.value.at(-1).isToday).toBe(true);
  });
});

describe('useWeeklyTimesheet — shop-day bucketing', () => {
  // 2026-05-03T03:02:49Z is 10:02 PM May 2 in Chicago — the real prod case.
  const LATE_SHIFT = {
    id: 'late', technician_id: 'me',
    clock_in_at: '2026-05-03T03:02:49Z', clock_out_at: '2026-05-03T05:02:49Z',
    minutes: 120, entry_type: 'clock', notes: null,
  };

  it('files the late-evening shift under the shop day it was worked', async () => {
    const ts = await pastWeek([LATE_SHIFT]);
    const saturday = ts.days.value.find((d) => d.key === '2026-05-02');
    const sunday = ts.days.value.find((d) => d.key === '2026-05-03');
    expect(saturday.entries).toHaveLength(1);
    expect(sunday.entries).toHaveLength(0);
  });

  it('keeps a shift whose UTC day fell outside the week', async () => {
    // Server-side UTC bucketing files it as May 3; had the client trusted
    // that, a week ending Sat May 2 would lose the row entirely — that is
    // what the fetch slack + client boundary exist for.
    apiGet.mockResolvedValue([LATE_SHIFT]);
    const ts = useWeeklyTimesheet();
    ts.weekStart.value = new Date(2026, 3, 26); // week ending May 2 (Sun-shifted probe)
    await ts.reload();
    expect(ts.weekEntries.value).toHaveLength(1);
  });
});

describe('useWeeklyTimesheet — totals', () => {
  it('nets break minutes out of worked hours', async () => {
    const ts = await pastWeek([
      { id: 'a', technician_id: 'me', clock_in_at: '2026-04-27T13:00:00Z', clock_out_at: '2026-04-27T21:00:00Z', minutes: 480, break_minutes: 30, entry_type: 'clock' },
      { id: 'b', technician_id: 'me', clock_in_at: '2026-04-28T13:00:00Z', clock_out_at: '2026-04-28T19:00:00Z', minutes: 360, entry_type: 'clock' },
    ]);
    const monday = ts.days.value.find((d) => d.key === '2026-04-27');
    expect(monday.workedMinutes).toBe(450);           // 480 − 30
    expect(ts.weekWorkedHours.value).toBeCloseTo(13.5, 5);
    expect(ts.weekBreakHours.value).toBeCloseTo(0.5, 5);
  });

  it('counts an unknown-duration shift as zero, not as its elapsed time', async () => {
    // Auto-closed rows carry minutes=null — worth nothing until a human sets
    // the real end time. Same rule as the office page and the backend.
    const ts = await pastWeek([
      { id: 'u', technician_id: 'me', clock_in_at: '2026-04-27T13:00:00Z', clock_out_at: '2026-04-29T13:00:00Z', minutes: null, entry_type: 'clock' },
    ]);
    expect(ts.weekWorkedHours.value).toBe(0);
  });
});

describe('useWeeklyTimesheet — unplaceable rows', () => {
  it('gives an unparseable stamp a labeled bucket instead of dropping the pay', async () => {
    // Audit 2026-08-13: an earlier version CLAIMED to keep these visible while
    // the day filter quietly dropped them from both the list and the totals.
    const ts = await pastWeek([
      { id: 'ok', technician_id: 'me', clock_in_at: '2026-04-27T13:00:00Z', clock_out_at: '2026-04-27T21:00:00Z', minutes: 480, entry_type: 'clock' },
      { id: 'corrupt', technician_id: 'me', clock_in_at: 'not-a-date', clock_out_at: null, minutes: 240, entry_type: 'clock' },
    ]);
    const bucket = ts.days.value.find((d) => d.key === 'unknown-date');
    expect(bucket).toBeTruthy();
    expect(bucket.entries.map((e) => e.id)).toEqual(['corrupt']);
    // ...and its minutes still count, so the week total doesn't understate.
    expect(ts.weekWorkedHours.value).toBeCloseTo(12, 5);
  });

  it('adds no such bucket when every stamp parses', async () => {
    const ts = await pastWeek([
      { id: 'ok', technician_id: 'me', clock_in_at: '2026-04-27T13:00:00Z', clock_out_at: '2026-04-27T21:00:00Z', minutes: 480, entry_type: 'clock' },
    ]);
    expect(ts.days.value.find((d) => d.key === 'unknown-date')).toBeUndefined();
  });
});

describe('useWeeklyTimesheet — self-edit affordance', () => {
  it('mirrors the server window', () => {
    const ts = useWeeklyTimesheet();
    const recent = { clock_in_at: new Date(Date.now() - 86_400_000).toISOString() };
    const old = { clock_in_at: new Date(Date.now() - (SELF_EDIT_WINDOW_DAYS + 2) * 86_400_000).toISOString() };
    expect(ts.canSelfEdit(recent)).toBe(true);
    expect(ts.canSelfEdit(old)).toBe(false);
  });

  it('refuses an unparseable stamp rather than offering a doomed edit', () => {
    const ts = useWeeklyTimesheet();
    expect(ts.canSelfEdit({ clock_in_at: 'not-a-date' })).toBe(false);
  });
});
