import { describe, expect, it } from 'vitest'

import { estimateStatusSeverity } from './statusSeverity'

describe('estimateStatusSeverity', () => {
  it('maps every authoritative estimate_status value to a valid PrimeVue token', () => {
    // enum: draft, sent, accepted, declined, rejected, expired
    expect(estimateStatusSeverity('draft')).toBe('secondary')
    expect(estimateStatusSeverity('sent')).toBe('info')
    expect(estimateStatusSeverity('accepted')).toBe('success')
    expect(estimateStatusSeverity('declined')).toBe('danger')
    expect(estimateStatusSeverity('rejected')).toBe('danger')
    expect(estimateStatusSeverity('expired')).toBe('warn')
  })

  it('is case-insensitive and falls back to secondary', () => {
    expect(estimateStatusSeverity('ACCEPTED')).toBe('success')
    expect(estimateStatusSeverity('')).toBe('secondary')
    expect(estimateStatusSeverity(null)).toBe('secondary')
    expect(estimateStatusSeverity(undefined)).toBe('secondary')
    expect(estimateStatusSeverity('something-unknown')).toBe('secondary')
  })

  it('never returns the invalid PrimeVue-3 token "warning"', () => {
    const tokens = ['draft', 'sent', 'accepted', 'declined', 'rejected', 'expired', 'x'].map(
      estimateStatusSeverity,
    )
    expect(tokens).not.toContain('warning')
  })
})
