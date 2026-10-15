import { describe, it, expect } from 'vitest'
import { remindAtIn, remindAtOut, reminderLabel, reminderPending } from '../plannerReminders'

describe('plannerReminders', () => {
  it('sends a picked time as ISO with an offset, which the server requires', () => {
    const d = new Date(2026, 9, 15, 9, 30)
    const out = remindAtOut(d)
    expect(out).toMatch(/Z$/)
    expect(new Date(out).getTime()).toBe(d.getTime())
  })

  it('a cleared picker clears the reminder', () => {
    expect(remindAtOut(null)).toBeNull()
    expect(remindAtOut(new Date('nope'))).toBeNull()
  })

  it('reads the server instant back to the same moment', () => {
    const iso = '2026-10-15T14:30:00+00:00'
    expect(remindAtIn(iso).getTime()).toBe(Date.parse(iso))
    expect(remindAtOut(remindAtIn(iso))).toBe('2026-10-15T14:30:00.000Z')
    expect(remindAtIn(null)).toBeNull()
    expect(remindAtIn('garbage')).toBeNull()
  })

  it('labels the reminder in the device time', () => {
    const d = new Date(2026, 9, 15, 9, 5)
    expect(reminderLabel(d.toISOString())).toBe('Oct 15, 9:05 AM')
    expect(reminderLabel('garbage')).toBe('')
  })

  it('shows the bell only until the reminder has gone off', () => {
    expect(reminderPending({ remind_at: '2026-10-15T14:30:00+00:00', reminder_sent_at: null })).toBe(true)
    expect(reminderPending({ remind_at: '2026-10-15T14:30:00+00:00', reminder_sent_at: '2026-10-15T14:30:05+00:00' })).toBe(false)
    expect(reminderPending({ remind_at: null })).toBe(false)
  })
})
