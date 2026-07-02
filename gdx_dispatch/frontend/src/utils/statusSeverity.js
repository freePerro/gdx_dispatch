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
