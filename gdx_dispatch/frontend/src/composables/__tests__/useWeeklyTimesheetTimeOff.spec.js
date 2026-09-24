/**
 * useWeeklyTimesheet — paid time off in the tech's own week.
 *
 * A vacation day or a holiday is a closed entry with a synthetic span
 * (core/time_off.py). The week must show it, count it as PAID for the day,
 * and keep it OUT of worked hours — the same split the office page and the
 * emailed file make, so a tech's card and the bookkeeper's file agree.
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

import { useWeeklyTimesheet } from '../useWeeklyTimesheet';

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockResolvedValue([]);
});

// Mon Apr 27 – Sun May 3, 2026 (a past week, so no future-day skipping).
const PAST_MONDAY = new Date(2026, 3, 27);

async function pastWeek(entries) {
  apiGet.mockResolvedValue(entries);
  const ts = useWeeklyTimesheet();
  ts.weekStart.value = new Date(PAST_MONDAY);
  await ts.reload();
  return ts;
}

describe('useWeeklyTimesheet — time off', () => {
  it('keeps a vacation day out of worked hours and in its own total', async () => {
    const ts = await pastWeek([
      // Mon: worked 8h, 30m lunch (13:00Z = 8am Chicago).
      { id: 'w1', entry_type: 'clock', minutes: 480, break_minutes: 30,
        clock_in_at: '2026-04-27T13:00:00+00:00', clock_out_at: '2026-04-27T21:00:00+00:00' },
      // Tue: vacation, synthetic 8h span.
      { id: 'v1', entry_type: 'vacation', minutes: 480, break_minutes: null,
        clock_in_at: '2026-04-28T13:00:00+00:00', clock_out_at: '2026-04-28T21:00:00+00:00' },
      // Wed: holiday, half day.
      { id: 'h1', entry_type: 'holiday', minutes: 240, break_minutes: null,
        clock_in_at: '2026-04-29T13:00:00+00:00', clock_out_at: '2026-04-29T17:00:00+00:00' },
    ]);
    expect(ts.weekWorkedHours.value).toBeCloseTo(7.5, 5);
    expect(ts.weekTimeOffHours.value).toBeCloseTo(12, 5);

    const byKey = Object.fromEntries(ts.days.value.map((d) => [d.key, d]));
    expect(byKey['2026-04-27'].workedMinutes).toBe(450);
    expect(byKey['2026-04-27'].timeOffMinutes).toBe(0);
    expect(byKey['2026-04-28'].workedMinutes).toBe(0);
    expect(byKey['2026-04-28'].timeOffMinutes).toBe(480);
    expect(byKey['2026-04-29'].timeOffMinutes).toBe(240);

    const vac = byKey['2026-04-28'].entries[0];
    expect(ts.workedMinutes(vac)).toBe(0);
    expect(ts.timeOffMinutes(vac)).toBe(480);
    expect(ts.paidMinutes(vac)).toBe(480);
    const shift = byKey['2026-04-27'].entries[0];
    expect(ts.paidMinutes(shift)).toBe(450);
  });
});
