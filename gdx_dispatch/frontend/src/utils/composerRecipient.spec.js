/**
 * The regression net for "the typed address never reached the server".
 *
 * Every assertion here is on a VALUE the server receives, not on the presence
 * of a line of source. Delete `to_email` from composerSendPayload and all of
 * the override cases below go red.
 */
import { describe, expect, it } from 'vitest'
import {
  composerSendPayload,
  isValidRecipientEmail,
  typedAddress,
} from './composerRecipient'

const ACCOUNT = { contact_id: '', name: 'Acme', email: 'ap@acme.example', label: 'Account email' }

describe('typedAddress', () => {
  it('is the typed value when the account has no stored recipients', () => {
    expect(typedAddress({ recipients: [], to: 'typed@example.com' })).toBe('typed@example.com')
  })

  it('is empty while the saved-contact picker is driving', () => {
    // The picker owns the address; sending a stale `to` here would silently
    // override the contact the operator actually selected.
    expect(typedAddress({ recipients: [ACCOUNT], to: 'ap@acme.example' })).toBe('')
  })

  it('is the typed value once the operator overrides an existing recipient', () => {
    // The case the old UI could not express at all: one wrong address on file.
    expect(typedAddress({ recipients: [ACCOUNT], overrideMode: true, to: 'pm@bldg.example' }))
      .toBe('pm@bldg.example')
  })

  it('trims, and treats whitespace as absent', () => {
    expect(typedAddress({ recipients: [], to: '  a@b.co  ' })).toBe('a@b.co')
    expect(typedAddress({ recipients: [], to: '   ' })).toBe('')
  })

  it('survives an empty composer', () => {
    expect(typedAddress(undefined)).toBe('')
    expect(typedAddress({})).toBe('')
  })
})

describe('composerSendPayload', () => {
  it('CARRIES the typed address — the defect this suite exists for', () => {
    const body = composerSendPayload({
      recipients: [], to: 'typed@example.com', subject: 'S', body_text: 'B',
    })
    expect(body.to_email).toBe('typed@example.com')
  })

  it('carries an override even when stored recipients exist', () => {
    const body = composerSendPayload({
      recipients: [ACCOUNT], overrideMode: true, to: 'pm@bldg.example',
    })
    expect(body.to_email).toBe('pm@bldg.example')
  })

  it('sends null (not "") when the picker is driving, so the server resolves', () => {
    const body = composerSendPayload({ recipients: [ACCOUNT], contact_id: 'c-9' })
    expect(body.to_email).toBeNull()
    expect(body.contact_id).toBe('c-9')
  })

  it('always defines to_email, so an omission cannot regress silently', () => {
    expect(Object.keys(composerSendPayload({}))).toContain('to_email')
  })

  it('merges per-surface extras without dropping the recipient', () => {
    const body = composerSendPayload(
      { recipients: [], to: 'x@y.co' }, { extra_attachment_ids: ['d-1'] },
    )
    expect(body).toMatchObject({ to_email: 'x@y.co', extra_attachment_ids: ['d-1'] })
  })
})

describe('isValidRecipientEmail', () => {
  it('accepts a real address', () => {
    expect(isValidRecipientEmail('bob@acme.example')).toBe(true)
  })

  it('rejects what the server would reject as invalid_override', () => {
    for (const bad of ['not-an-email', 'a@b', 'a..b@c.co', '', '   ', null, undefined]) {
      expect(isValidRecipientEmail(bad)).toBe(false)
    }
  })
})

describe('is a typed address a NEW contact?', () => {
  // Pure form of the composer's typedAddressIsNew check, kept here so the
  // matching rule (case-insensitive, trimmed) is pinned independently of the
  // component: asking "is this a new contact?" about an address we already
  // hold is noise, and failing to ask about one we don't is the whole point.
  const isNew = (typed, recipients) => {
    const t = String(typed || '').trim().toLowerCase()
    return !!t && !recipients.some((r) => (r.email || '').trim().toLowerCase() === t)
  }
  const held = [{ email: 'ap@acme.example' }, { email: 'Bob@Acme.Example' }]

  it('asks for an address the customer does not have', () => {
    expect(isNew('pm@bldg.example', held)).toBe(true)
  })

  it('does not ask for one already on file', () => {
    expect(isNew('ap@acme.example', held)).toBe(false)
  })

  it('does not ask just because the case differs', () => {
    expect(isNew('bob@acme.example', held)).toBe(false)
    expect(isNew('  BOB@ACME.EXAMPLE  ', held)).toBe(false)
  })

  it('asks when the customer has nothing on file at all', () => {
    expect(isNew('first@contact.example', [])).toBe(true)
  })
})
