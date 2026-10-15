import { describe, it, expect } from 'vitest'
import { destinationFor } from '../notificationDestinations'

describe('destinationFor', () => {
  it('sends a text alert to the SMS page for the surface', () => {
    expect(destinationFor('sms', { mobile: true })).toBe('/mobile/sms')
    expect(destinationFor('sms')).toBe('/phone-com/messages')
  })

  it('maps payments with invoices', () => {
    expect(destinationFor('payment', { mobile: true })).toBe('/mobile/billing')
    expect(destinationFor('payment')).toBe('/invoices')
  })

  it('opens a planner reminder on the planner Tasks tab', () => {
    expect(destinationFor('planner_reminder', { mobile: true })).toBe('/mobile/planner?tab=tasks')
    expect(destinationFor('planner_reminder')).toBe('/planner')
  })

  it('returns null for unknown or missing categories', () => {
    expect(destinationFor('nope')).toBeNull()
    expect(destinationFor('')).toBeNull()
    expect(destinationFor(undefined, { mobile: true })).toBeNull()
  })
})
