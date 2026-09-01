/**
 * EstimateStatusContext — the story behind the status tag.
 *
 * Mounted with real assertions (not source pins): the banner names the
 * failed recipient and date, its two buttons emit the events EstimateView
 * wires to the customer page and the composer, the decline strip finally
 * renders declined_reason, and the activity list renders what the endpoint
 * returns. Every assertion here fails with the feature deleted.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import EstimateStatusContext from '../EstimateStatusContext.vue'

const STUBS = {
  Message: { template: '<div class="stub-message"><slot /></div>' },
  Button: {
    props: ['label', 'disabled'],
    // Declared, or the parent's @click also attaches as a native listener on
    // the root <button> and every click counts twice.
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
}

function mountWith(props) {
  return mount(EstimateStatusContext, { props, global: { stubs: STUBS } })
}

const BOUNCE = {
  bounce: {
    failed_recipient: 'bjfarms1888@example.com',
    ndr_subject: 'Undeliverable: Garage door',
    matched_by: 'conversation_time',
    at: '2026-08-14T06:00:08+00:00',
  },
  decline: null,
}

describe('EstimateStatusContext — customer reassignment in the trail', () => {
  const row = (details) => ({
    id: 'a-1', action: 'estimate_customer_reassigned',
    label: 'Customer changed', user_name: 'Doug',
    created_at: '2026-09-01T12:00:00+00:00', details,
  })

  it('names both customers and the reason — the point of the row', () => {
    // detailLine() is a per-action whitelist: registering the label in the
    // backend map alone leaves this row rendering as a bare "Customer changed"
    // that says nothing about what changed.
    const w = mountWith({
      status: 'draft', context: { bounce: null, decline: null }, total: 1,
      items: [row({
        from_customer_name: 'Acme Overhead',
        to_customer_name: 'Baker Property',
        reason: 'quoted under the wrong account',
      })],
    })
    const text = w.find('[data-testid="estimate-activity"]').text()
    expect(text).toContain('Acme Overhead')
    expect(text).toContain('Baker Property')
    expect(text).toContain('quoted under the wrong account')
  })

  it('still reads when the estimate had no customer before', () => {
    const w = mountWith({
      status: 'draft', context: { bounce: null, decline: null }, total: 1,
      items: [row({ from_customer_name: null, to_customer_name: 'Baker Property', reason: 'r' })],
    })
    expect(w.find('[data-testid="estimate-activity"]').text()).toContain('no customer')
  })
})

describe('EstimateStatusContext — send to a different address', () => {
  it('offers a third way out of a bounce, and emits send-to-other', async () => {
    // "Fix customer email" rewrites the account address for good; "Re-send"
    // reuses the address that bounced. Neither sends this one elsewhere.
    const w = mountWith({ status: 'rejected', context: BOUNCE, customerId: 'c-1' })
    const btn = w.find('[data-testid="estimate-send-to-other"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    expect(w.emitted('send-to-other')).toHaveLength(1)
  })

  it('is disabled with no customer — /send stops before reading an override', () => {
    const w = mountWith({ status: 'rejected', context: BOUNCE, customerId: null })
    expect(w.find('[data-testid="estimate-send-to-other"]').attributes('disabled')).toBeDefined()
  })

  it('does not appear on a healthy estimate', () => {
    const w = mountWith({ status: 'sent', context: { bounce: null, decline: null }, customerId: 'c-1' })
    expect(w.find('[data-testid="estimate-send-to-other"]').exists()).toBe(false)
  })
})

describe('EstimateStatusContext — Failed Email banner', () => {
  it('names the failed recipient and the bounce date when status is rejected', () => {
    const w = mountWith({ status: 'Rejected', context: BOUNCE, customerId: 'c-1' })
    const banner = w.find('[data-testid="estimate-bounce-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('Failed Email')
    expect(w.find('[data-testid="bounce-recipient"]').text()).toBe('bjfarms1888@example.com')
    expect(banner.text()).toMatch(/bounced on .*2026/)
    expect(banner.text()).toContain('never received it')
    // The stored enum word never reaches the screen.
    expect(banner.text()).not.toMatch(/rejected/i)
  })

  it('still explains a bounce whose audit row is missing, without inventing a recipient', () => {
    const w = mountWith({ status: 'rejected', context: { bounce: null, decline: null }, customerId: 'c-1' })
    const banner = w.find('[data-testid="estimate-bounce-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('The estimate email bounced')
    expect(w.find('[data-testid="bounce-recipient"]').exists()).toBe(false)
  })

  it('Re-send emits only resend (the composer), not fix-email', async () => {
    const w = mountWith({ status: 'rejected', context: BOUNCE, customerId: 'c-1' })
    await w.find('[data-testid="estimate-resend"]').trigger('click')
    expect(w.emitted('resend')).toHaveLength(1)
    expect(w.emitted('fix-email')).toBeUndefined()
  })

  it('Fix-email emits only fix-email (the customer page), not resend', async () => {
    const w = mountWith({ status: 'rejected', context: BOUNCE, customerId: 'c-1' })
    await w.find('[data-testid="estimate-fix-email"]').trigger('click')
    expect(w.emitted('fix-email')).toHaveLength(1)
    expect(w.emitted('resend')).toBeUndefined()
  })

  it('disables Fix-email when the estimate has no customer to edit', () => {
    const w = mountWith({ status: 'rejected', context: BOUNCE, customerId: null })
    expect(w.find('[data-testid="estimate-fix-email"]').attributes('disabled')).toBeDefined()
  })

  it('shows no banner for any other status', () => {
    for (const status of ['draft', 'sent', 'accepted', 'expired', '']) {
      const w = mountWith({ status, context: BOUNCE })
      expect(w.find('[data-testid="estimate-bounce-banner"]').exists()).toBe(false)
    }
  })
})

describe('EstimateStatusContext — declined strip', () => {
  it('renders when, by whom and why', () => {
    const w = mountWith({
      status: 'Declined',
      context: {
        bounce: null,
        decline: { reason: 'Went with a cheaper quote', at: '2026-08-10T15:30:00+00:00', user_name: 'Pat Office', actor_type: 'staff' },
      },
    })
    const strip = w.find('[data-testid="estimate-decline-strip"]')
    expect(strip.exists()).toBe(true)
    expect(strip.text()).toMatch(/^Declined .*2026/)
    expect(strip.text()).toContain('by Pat Office')
    expect(strip.text()).toContain('— Went with a cheaper quote')
    expect(w.find('[data-testid="estimate-bounce-banner"]').exists()).toBe(false)
  })

  it('degrades to a bare "Declined" when the context has nothing more', () => {
    const w = mountWith({ status: 'declined', context: { bounce: null, decline: null } })
    expect(w.find('[data-testid="estimate-decline-strip"]').text()).toBe('Declined')
  })
})

describe('EstimateStatusContext — activity panel', () => {
  const ITEMS = [
    { id: 'a1', action: 'estimate_email_rejected', label: 'Email bounced — the customer did not receive it', user_name: 'System — email bounce detector', created_at: '2026-08-14T06:00:08+00:00', details: { failed_recipient: 'bad@example.com' } },
    { id: 'a2', action: 'estimate_marked_sent', label: 'Marked sent', user_name: 'Doug', created_at: '2026-08-13T19:01:20+00:00', details: { channel: 'manual' } },
    { id: 'a3', action: 'estimate_created', label: 'Created', user_name: 'Doug', created_at: '2026-08-13T18:49:41+00:00', details: {} },
  ]

  it('lists every item with its label, actor and time, and the failed recipient as a detail line', () => {
    const w = mountWith({ status: 'rejected', context: BOUNCE, items: ITEMS, total: 3 })
    const rows = w.findAll('.activity-row')
    expect(rows).toHaveLength(3)
    expect(w.find('[data-testid="estimate-activity"] summary').text()).toContain('(3)')
    expect(rows[0].attributes('data-action')).toBe('estimate_email_rejected')
    expect(rows[0].text()).toContain('Email bounced')
    expect(rows[0].text()).toContain('System — email bounce detector')
    expect(rows[0].text()).toContain('to bad@example.com')
    expect(rows[1].text()).toContain('Marked sent')
    expect(rows[1].text()).toContain('Doug')
    // A manual mark-sent has no channel worth a second line.
    expect(rows[1].text()).not.toContain('via')
  })

  it('says so when there is nothing recorded, and while loading', () => {
    const empty = mountWith({ status: 'draft', items: [], total: 0 })
    expect(empty.find('[data-testid="estimate-activity-empty"]').exists()).toBe(true)
    const loading = mountWith({ status: 'draft', items: [], total: 0, loading: true })
    expect(loading.text()).toContain('Loading')
    expect(loading.find('[data-testid="estimate-activity-empty"]').exists()).toBe(false)
  })
})
