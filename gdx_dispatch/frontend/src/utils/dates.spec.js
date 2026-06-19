/**
 * D-S99-vitest-no-coverage — Cover the formatDate util that backstops the
 * UTC-shift bug caught during the slice-1 prod walk (statement 2026-05-03
 * was rendering as 5/2/2026 in CST). The date-only branch must format
 * components directly without crossing UTC.
 */
import { describe, expect, it } from 'vitest'
import { formatDate } from './dates'

describe('formatDate', () => {
  it('renders date-only YYYY-MM-DD without UTC shift', () => {
    expect(formatDate('2026-05-03')).toBe('5/3/2026')
    expect(formatDate('2025-11-22')).toBe('11/22/2025')
    expect(formatDate('2026-01-01')).toBe('1/1/2026')
  })

  it('renders full ISO timestamps via toLocaleDateString', () => {
    // Full ISO has a time portion → goes through Date.parse → locale string.
    // We only assert it returns *something* non-empty; the locale format
    // depends on the runner's zone.
    const out = formatDate('2026-05-03T15:30:00Z')
    expect(out).toBeTruthy()
    expect(typeof out).toBe('string')
  })

  it('returns empty string for null/undefined/empty', () => {
    expect(formatDate(null)).toBe('')
    expect(formatDate(undefined)).toBe('')
    expect(formatDate('')).toBe('')
  })

  it('returns the original string for unparseable input', () => {
    expect(formatDate('not-a-date')).toBe('not-a-date')
  })
})
