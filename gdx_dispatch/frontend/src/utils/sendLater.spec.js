import { describe, expect, it } from 'vitest'
import { isQuietHours, nextAt, presets, scheduledStatusLabel, whenLabel } from './sendLater'

// Local-time constructors: these helpers work in the browser's zone.
const at = (y, mo, d, h, mi = 0, s = 0) => new Date(y, mo - 1, d, h, mi, s, 0)

describe('nextAt', () => {
  it('is later today when the hour is still ahead', () => {
    expect(nextAt(8, 0, at(2026, 9, 30, 1, 15))).toEqual(at(2026, 9, 30, 8))
  })
  it('rolls to tomorrow once the hour has passed', () => {
    expect(nextAt(8, 0, at(2026, 9, 30, 22, 40))).toEqual(at(2026, 10, 1, 8))
  })
  it('rolls to tomorrow inside the one-minute lead the server requires', () => {
    expect(nextAt(8, 0, at(2026, 9, 30, 7, 59, 30))).toEqual(at(2026, 10, 1, 8))
  })
})

describe('whenLabel and presets', () => {
  const now = at(2026, 9, 30, 22, 40)
  it('names today and tomorrow, and dates anything later', () => {
    expect(whenLabel(at(2026, 9, 30, 23), now)).toMatch(/^Today /)
    expect(whenLabel(at(2026, 10, 1, 8), now)).toMatch(/^Tomorrow 8:00/)
    expect(whenLabel(at(2026, 10, 3, 8), now)).not.toMatch(/^(Today|Tomorrow)/)
  })
  it('offers tomorrow morning at night', () => {
    const [eight, nine] = presets(now)
    expect(eight.at).toEqual(at(2026, 10, 1, 8))
    expect(nine.at).toEqual(at(2026, 10, 1, 9))
    expect(eight.label).toMatch(/^Tomorrow 8:00/)
  })
})

describe('isQuietHours', () => {
  it('is 8 PM to 8 AM', () => {
    expect(isQuietHours(at(2026, 9, 30, 22))).toBe(true)
    expect(isQuietHours(at(2026, 9, 30, 7, 59))).toBe(true)
    expect(isQuietHours(at(2026, 9, 30, 8))).toBe(false)
    expect(isQuietHours(at(2026, 9, 30, 19, 59))).toBe(false)
  })
})

describe('scheduledStatusLabel', () => {
  it('says when a waiting text goes and what happened to one that did not', () => {
    const now = at(2026, 9, 30, 22)
    expect(scheduledStatusLabel({ status: 'scheduled', send_at: at(2026, 10, 1, 8).toISOString() }, now))
      .toMatch(/^Sends Tomorrow 8:00/)
    expect(scheduledStatusLabel({ status: 'scheduled', send_at: at(2026, 9, 30, 21, 50).toISOString() }, now))
      .toBe('Overdue — waiting to send')
    expect(scheduledStatusLabel({ status: 'scheduled', send_at: at(2026, 9, 30, 21, 0).toISOString() }, now))
      .toMatch(/will not send/)
    expect(scheduledStatusLabel({ status: 'unknown' }, now)).toMatch(/check the thread/)
    expect(scheduledStatusLabel({ status: 'skipped' }, now)).toBe('Not sent')
  })
})
