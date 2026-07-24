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

import {
  appointmentStatusSeverity,
  leadStageSeverity,
  payrollRunSeverity,
  timeclockEntrySeverity,
  timeclockStatusSeverity,
} from './statusSeverity'

describe('Tier-4 consolidated maps', () => {
  it('appointment map covers the lifecycle with valid PV4 tokens only', () => {
    const valid = ['secondary', 'success', 'info', 'warn', 'danger', 'contrast']
    for (const s of ['scheduled', 'confirmed', 'en_route', 'arrived', 'in_progress', 'completed', 'cancelled', 'no_show', 'bogus', null]) {
      expect(valid).toContain(appointmentStatusSeverity(s))
    }
    expect(appointmentStatusSeverity('en_route')).toBe('warn')
    expect(appointmentStatusSeverity('cancelled')).toBe('danger')
  })

  it('timeclock: clocked-out is neutral, not an alarm', () => {
    expect(timeclockStatusSeverity({ clockedIn: false, onBreak: false })).toBe('secondary')
    expect(timeclockStatusSeverity({ clockedIn: true, onBreak: true })).toBe('warn')
    expect(timeclockStatusSeverity({ clockedIn: true, onBreak: false })).toBe('success')
    expect(timeclockEntrySeverity('break')).toBe('warn')
    expect(timeclockEntrySeverity(undefined)).toBe('info')
  })

  it('lead + payroll maps never emit the invalid warning token', () => {
    for (const fn of [leadStageSeverity, payrollRunSeverity]) {
      for (const s of ['new', 'contacted', 'pending', 'won', 'failed', 'anything', null]) {
        expect(fn(s)).not.toBe('warning')
      }
    }
    expect(leadStageSeverity('Contacted')).toBe('warn')
    expect(payrollRunSeverity('processing')).toBe('warn')
  })
})
