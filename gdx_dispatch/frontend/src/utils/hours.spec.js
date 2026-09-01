import { describe, expect, it } from 'vitest'

import { formatDurationHours, formatHoursNumber } from './hours'

// #521 — the trailing-zero trim ate zeros off whole numbers. These are the
// exact inputs from the issue's table; the whole-number rows are the ones the
// old regex got wrong.
describe('formatDurationHours', () => {
  it.each([
    [8, '8h'],
    [10, '10h'],
    [20, '20h'],
    [30, '30h'],
    [100, '100h'],
    [0, '0h'],
    [7.5, '7.5h'],
    [12, '12h'],
    [2.5, '2.5h'],
    ['10', '10h'],
    [1.005, '1h'],
    [0.125, '0.13h'],
  ])('renders %p as %p', (input, expected) => {
    expect(formatDurationHours(input)).toBe(expected)
  })

  it('marks a missing or non-numeric value as no estimate', () => {
    expect(formatDurationHours(null)).toBe('?h')
    expect(formatDurationHours(undefined)).toBe('?h')
    expect(formatDurationHours('')).toBe('?h')
    expect(formatDurationHours('abc')).toBe('?h')
    expect(formatDurationHours(NaN)).toBe('?h')
    expect(formatDurationHours(Infinity)).toBe('?h')
  })
})

describe('formatHoursNumber', () => {
  it('keeps whole numbers whole and trims to two places', () => {
    expect(formatHoursNumber(10)).toBe('10')
    expect(formatHoursNumber(0)).toBe('0')
    expect(formatHoursNumber(2.5)).toBe('2.5')
    expect(formatHoursNumber(2.5049)).toBe('2.5')
    expect(formatHoursNumber(100)).toBe('100')
  })

  it('returns null for anything that is not a number', () => {
    expect(formatHoursNumber(null)).toBeNull()
    expect(formatHoursNumber('')).toBeNull()
    expect(formatHoursNumber('x')).toBeNull()
  })
})
