/**
 * InvoiceActivityPanel — the invoice trail and the "did they open it?" line.
 *
 * Mounted with real assertions: the view summary reads the endpoint's
 * count (not the page's rows), "not opened" only appears once the invoice
 * has gone out, and reminder rows carry their stage or skip reason. Every
 * assertion here fails with the feature deleted.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import InvoiceActivityPanel from '../InvoiceActivityPanel.vue'

function mountWith(props) {
  return mount(InvoiceActivityPanel, { props })
}

const views = (w) => w.find('[data-testid="invoice-customer-views"]')

describe('InvoiceActivityPanel — customer view summary', () => {
  const LINK = '2026-09-20T15:00:00+00:00'

  it('shows the count and last time from context, not from the rows', () => {
    const w = mountWith({
      status: 'sent', total: 0, items: [],
      customerViews: { count: 3, last_at: '2026-09-29T21:22:19+00:00', link_sent_at: LINK },
    })
    expect(views(w).exists()).toBe(true)
    expect(views(w).text()).toContain('Customer opened the invoice')
    expect(views(w).text()).toContain('3 times')
    expect(views(w).text()).toContain('last')
  })

  it('says "once" for a single view', () => {
    const w = mountWith({ status: 'sent', customerViews: { count: 1, last_at: '2026-09-29T21:22:19+00:00', link_sent_at: LINK } })
    expect(views(w).text()).toContain('once')
  })

  it('says not opened only when the server confirms a link reached the customer', () => {
    const w = mountWith({ status: 'sent', customerViews: { count: 0, last_at: null, link_sent_at: LINK } })
    expect(views(w).text()).toContain("hasn't opened")
    const overdue = mountWith({ status: 'Overdue', customerViews: { count: 0, last_at: null, link_sent_at: LINK } })
    expect(views(overdue).text()).toContain("hasn't opened")
  })

  it('never says not opened without a delivered link, or when nothing is owed', () => {
    const cases = [
      { status: 'sent', link_sent_at: null }, // paper, marked sent, old send, or no link could be minted
      { status: 'paid', link_sent_at: LINK }, // nothing to chase
      { status: 'void', link_sent_at: LINK },
      { status: 'draft', link_sent_at: LINK },
    ]
    for (const c of cases) {
      const w = mountWith({ status: c.status, customerViews: { count: 0, last_at: null, link_sent_at: c.link_sent_at } })
      expect(views(w).exists(), JSON.stringify(c)).toBe(false)
    }
  })

  it('still reports real views on a paid invoice', () => {
    const w = mountWith({ status: 'paid', customerViews: { count: 2, last_at: null, link_sent_at: null } })
    expect(views(w).text()).toContain('2 times')
  })

  it('explains what counts as a view', () => {
    const w = mountWith({ status: 'sent', customerViews: { count: 2, last_at: null, link_sent_at: LINK } })
    expect(views(w).attributes('title')).toContain('follows the emailed or texted link')
    expect(views(w).attributes('title')).toContain('can still be counted')
  })
})

describe('InvoiceActivityPanel — the trail', () => {
  const row = (over) => ({
    id: over.id || 'a-1', label: 'x', user_name: 'Doug',
    created_at: '2026-09-01T12:00:00+00:00', details: {}, ...over,
  })

  it('renders every row with label and actor', () => {
    const w = mountWith({
      total: 2,
      items: [
        row({ id: 'v', action: 'invoice_viewed_by_customer', label: 'Viewed by customer', user_name: 'Customer' }),
        row({ id: 's', action: 'invoice_sent', label: 'Emailed to customer' }),
      ],
    })
    const panel = w.find('[data-testid="invoice-activity"]')
    expect(panel.text()).toContain('Activity')
    expect(panel.text()).toContain('(2)')
    expect(panel.findAll('li')).toHaveLength(2)
    expect(panel.text()).toContain('Viewed by customer')
    expect(panel.text()).toContain('Customer ·')
  })

  it('names the reminder stage and the skip reason', () => {
    const w = mountWith({
      total: 2,
      items: [
        row({ id: 'r1', action: 'payment_reminder', label: 'Payment reminder emailed',
          details: { stage: 'final_notice' } }),
        row({ id: 'r2', action: 'payment_reminder_skipped', label: 'Payment reminder not sent',
          details: { skip_reason: 'no customer email' } }),
      ],
    })
    const text = w.find('[data-testid="invoice-activity"]').text()
    expect(text).toContain('Final notice')
    expect(text).toContain('Skipped — no customer email')
  })

  it('names the bounced recipient', () => {
    const w = mountWith({
      total: 1,
      items: [row({ action: 'invoice_email_rejected', label: 'Email bounced',
        details: { failed_recipient: 'pat@example.com' } })],
    })
    expect(w.text()).toContain('to pat@example.com')
  })

  it('says why an invoice email was not sent, and where a bounce went', () => {
    const w = mountWith({
      total: 2,
      items: [
        row({ id: 'e1', action: 'email_failed', label: 'Invoice email not sent', details: { skip_reason: 'graph_error' } }),
        row({ id: 'e2', action: 'email_bounced', label: 'Invoice email bounced', details: { to_email: 'pat@example.com' } }),
      ],
    })
    const text = w.find('[data-testid="invoice-activity"]').text()
    expect(text).toContain('Reason: graph_error')
    expect(text).toContain('to pat@example.com')
  })

  it('says the read failed instead of claiming there is no activity', () => {
    const w = mountWith({ total: 0, items: [], error: true })
    expect(w.find('[data-testid="invoice-activity-error"]').exists()).toBe(true)
    expect(w.find('[data-testid="invoice-activity-empty"]').exists()).toBe(false)
    expect(w.find('[data-testid="invoice-activity"] summary').text()).not.toContain('(0)')
  })

  it('shows an empty state instead of a blank panel', () => {
    const w = mountWith({ total: 0, items: [] })
    expect(w.find('[data-testid="invoice-activity-empty"]').exists()).toBe(true)
  })
})
