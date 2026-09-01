import { describe, expect, it } from 'vitest'

import { formatTimestamp } from './formatTimestamp'

// PG timestamptz serialises as "2026-04-08 16:12:20.49811+00" — a space, six
// fractional digits and a bare hour offset. Date() rejects the bare "+00", so
// every such value fell through to the raw string (seen on the Reviews page's
// Received column, 2026-09-01). The formatter must parse the PG shape, the ISO
// shape, and still show a genuinely malformed value raw.
describe('formatTimestamp', () => {
  it('parses the Postgres timestamptz shape (space, microseconds, bare offset)', () => {
    const out = formatTimestamp('2026-04-08 16:12:20.49811+00')
    expect(out).not.toBe('2026-04-08 16:12:20.49811+00')
    expect(out).toMatch(/2026/)
  })

  it('parses ISO-8601 with Z and with a full offset', () => {
    expect(formatTimestamp('2026-04-08T16:12:20Z')).toMatch(/2026/)
    expect(formatTimestamp('2026-04-08T16:12:20.123+00:00')).toMatch(/2026/)
  })

  it('keeps the date across the two shapes', () => {
    expect(formatTimestamp('2026-04-08 16:12:20.49811+00')).toBe(formatTimestamp('2026-04-08T16:12:20.498Z'))
  })

  it('renders datetime and short styles for the PG shape', () => {
    expect(formatTimestamp('2026-04-08 16:12:20.49811+00', 'datetime')).toMatch(/2026.*\d{1,2}:\d{2}/)
    expect(formatTimestamp('2026-04-08 16:12:20.49811+00', 'short')).toMatch(/Apr/)
  })

  it('shows a malformed value raw and a missing value as a dash', () => {
    expect(formatTimestamp('not a date')).toBe('not a date')
    expect(formatTimestamp(null)).toBe('—')
  })
})
