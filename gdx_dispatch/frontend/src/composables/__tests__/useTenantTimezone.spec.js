// Pins the dispatch-board off-by-one fix: a UTC timestamp must resolve to the
// correct OFFICE-local calendar day. These cases are deterministic regardless
// of the test runner's own timezone because Intl uses the passed timeZone.
import { describe, it, expect } from 'vitest';
import { dateKeyInZone } from '../useTenantTimezone';

describe('dateKeyInZone', () => {
  it('keeps a late-evening US-Central job on the SAME day (the reported bug)', () => {
    // "Jul 10 8:00 PM" US-Central serializes to 2026-07-11T01:00:00Z.
    // The old code sliced the UTC string → "2026-07-11" (wrong column).
    expect(dateKeyInZone('2026-07-11T01:00:00Z', 'America/Chicago')).toBe('2026-07-10');
  });

  it('rolls to the next day only once it is actually midnight in the zone', () => {
    // 05:00Z = 00:00 CDT → July 11 in Chicago.
    expect(dateKeyInZone('2026-07-11T05:00:00Z', 'America/Chicago')).toBe('2026-07-11');
    // 04:59Z = 23:59 CDT → still July 10.
    expect(dateKeyInZone('2026-07-11T04:59:00Z', 'America/Chicago')).toBe('2026-07-10');
  });

  it('resolves per the given zone (Eastern vs Central differ near midnight)', () => {
    expect(dateKeyInZone('2026-07-11T01:00:00Z', 'America/New_York')).toBe('2026-07-10');
    expect(dateKeyInZone('2026-07-11T03:30:00Z', 'America/New_York')).toBe('2026-07-10'); // 23:30 EDT
    expect(dateKeyInZone('2026-07-11T03:30:00Z', 'America/Chicago')).toBe('2026-07-10'); // 22:30 CDT
  });

  it('handles a plain midday timestamp', () => {
    expect(dateKeyInZone('2026-07-10T12:00:00Z', 'America/Chicago')).toBe('2026-07-10');
  });

  it('falls back to a valid YYYY-MM-DD (no throw) for an unknown/absent zone', () => {
    expect(dateKeyInZone('2026-07-11T01:00:00Z', 'Not/AZone')).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(dateKeyInZone('2026-07-11T01:00:00Z', null)).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it('returns empty string for an unparseable value', () => {
    expect(dateKeyInZone('', 'America/Chicago')).toBe('');
    expect(dateKeyInZone('not-a-date', 'America/Chicago')).toBe('');
    expect(dateKeyInZone(null, 'America/Chicago')).toBe('');
  });
});
