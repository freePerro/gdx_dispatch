/**
 * Estimate status → PrimeVue Tag `severity`.
 *
 * Single source of truth for estimate-status colour, shared by JobDetailView
 * and MobileCustomerDetailView (which previously each hand-rolled a divergent
 * copy — one even used the invalid PrimeVue-3 token `'warning'`).
 *
 * Status values are the authoritative `estimate_status` enum
 * (gdx_dispatch/modules/proposals/models.py): draft, sent, accepted, declined,
 * rejected, expired. PrimeVue 4 severity tokens: secondary, info, success,
 * warn, danger, contrast.
 */
export function estimateStatusSeverity(status) {
  const map = {
    draft: 'secondary', // not yet sent — neutral
    sent: 'info', // awaiting customer response
    accepted: 'success',
    declined: 'danger',
    rejected: 'danger',
    expired: 'warn', // lapsed — needs attention
  }
  return map[String(status || '').toLowerCase()] || 'secondary'
}

/**
 * Invoice status → PrimeVue Tag `severity`.
 *
 * Shared by InvoiceDetailView, BillingView, MobileBillingView and
 * MobileCustomerDetailView, which previously carried three divergent local
 * maps — none knew `void` (which the deposit lifecycle produces routinely:
 * an abandoned unpaid deposit is voided at final-invoice creation), and the
 * mobile copies used the invalid PrimeVue-3 token `'warning'`.
 *
 * Backend enum (models/tenant_models.py Invoice.status): draft, sent, paid,
 * overdue, void. The extra keys cover derived/display statuses: `partial`
 * (BillingView effective_status), `pending`/`canceled` (mobile), `unpaid`
 * (portal payment_status). `void` is `contrast`, not `danger` — a voided
 * invoice is closed, not a problem needing action.
 */
export function invoiceStatusSeverity(status) {
  const map = {
    draft: 'secondary',
    pending: 'secondary',
    sent: 'info',
    partial: 'warn',
    unpaid: 'warn',
    paid: 'success',
    overdue: 'danger',
    canceled: 'danger',
    void: 'contrast',
  }
  return map[String(status || '').toLowerCase()] || 'secondary'
}

/**
 * Appointment status → PrimeVue Tag `severity`.
 *
 * AppointmentsView and JobDetailView carried DIVERGENT hand-rolled maps —
 * the same appointment rendered a different colour depending on which screen
 * you saw it on (scheduled: warn↔info, confirmed: info↔success, en_route and
 * arrived flipped too), and both used the invalid `'warning'` token.
 * Canon: awaiting=info, locked-in/positive=success, in-motion=warn,
 * terminal-bad=danger.
 */
export function appointmentStatusSeverity(status) {
  const map = {
    scheduled: 'info', // on the calendar, awaiting the day
    confirmed: 'success', // customer confirmed
    en_route: 'warn', // live, in motion — watch it
    arrived: 'success', // tech on site
    in_progress: 'warn',
    completed: 'success',
    cancelled: 'danger',
    no_show: 'danger',
  }
  return map[String(status || '').toLowerCase()] || 'secondary'
}

/**
 * Timeclock presence → severity. Desktop said clocked-out = danger, mobile
 * said secondary; a tech who has gone home is a neutral fact, not an alarm.
 */
export function timeclockStatusSeverity({ clockedIn, onBreak }) {
  if (!clockedIn) return 'secondary'
  if (onBreak) return 'warn'
  return 'success'
}

/** Timeclock entry rows: break entries stand out from work entries. */
export function timeclockEntrySeverity(entryType) {
  return String(entryType || 'work').toLowerCase() === 'break' ? 'warn' : 'info'
}

/** Lead pipeline stage → severity (LeadsView). */
export function leadStageSeverity(stage) {
  const map = {
    new: 'info',
    contacted: 'warn',
    qualified: 'success',
    quoted: 'info',
    won: 'success',
    lost: 'danger',
  }
  return map[String(stage || '').toLowerCase()] || 'secondary'
}

/** Payroll run status → severity (PayrollView). */
export function payrollRunSeverity(status) {
  const normalized = String(status || '').toLowerCase()
  if (!normalized) return 'info'
  if (['paid', 'finalized', 'completed'].includes(normalized)) return 'success'
  if (['failed', 'rejected', 'denied'].includes(normalized)) return 'danger'
  if (['pending', 'processing', 'running'].includes(normalized)) return 'warn'
  return 'info'
}
