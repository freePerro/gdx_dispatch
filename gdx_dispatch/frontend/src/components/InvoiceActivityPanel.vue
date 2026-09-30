<!--
  InvoiceActivityPanel — the invoice's trail, and whether the customer opened it.

  Reads GET /api/invoices/{id}/activity (the invoice twin of the estimate
  activity endpoint). Two parts:
  - a one-line customer-view summary that is visible while the panel is
    collapsed, so "did they even look at it?" needs no click;
  - the activity list — sends, what each invoice email actually did, texts,
    bounces, customer views, payment reminders (manual and automatic),
    voids — who did what, when.

  Payments are not in the list: Payment History on the same page is the
  record for those (see the endpoint for why the audit trail can't be).

  A "view" is the customer following the emailed or texted link to the pay
  page — a click, not a tracking pixel (core/customer_views.py). Recorded
  since 2026-07-29, so an older invoice can read "not opened" and still
  have been seen.

  Dumb by design: props in, nothing out. InvoiceDetailView owns the fetch.
-->
<template>
  <div class="inv-activity" data-testid="invoice-activity-context">
    <p
      v-if="viewCount > 0"
      class="view-line viewed"
      data-testid="invoice-customer-views"
      :title="VIEW_HINT"
    >
      <i class="pi pi-eye" aria-hidden="true" />
      Customer opened the invoice
      <template v-if="viewCount === 1">once</template>
      <template v-else>{{ viewCount }} times</template>
      <template v-if="lastViewAt"> · last {{ fmtDateTime(lastViewAt) }}</template>
    </p>
    <p
      v-else-if="showNotOpened"
      class="view-line"
      data-testid="invoice-customer-views"
      :title="VIEW_HINT"
    >
      <i class="pi pi-eye-slash" aria-hidden="true" />
      Customer hasn't opened the invoice link yet
    </p>

    <details class="activity-panel" data-testid="invoice-activity">
      <summary>Activity <span v-if="!error" class="muted">({{ total }})</span></summary>
      <p v-if="loading" class="muted">Loading…</p>
      <!-- A failed read must not read as "nothing happened". -->
      <p v-else-if="error" class="muted" data-testid="invoice-activity-error">Couldn't load the activity. Reload the page to try again.</p>
      <p v-else-if="!items.length" class="muted" data-testid="invoice-activity-empty">No activity recorded.</p>
      <ul v-else class="activity-list">
        <li v-for="it in items" :key="it.id" class="activity-row" :data-action="it.action">
          <span class="activity-label">{{ it.label || it.action }}</span>
          <span v-if="detailLine(it)" class="activity-detail">{{ detailLine(it) }}</span>
          <span class="activity-meta">{{ it.user_name || 'System' }} · {{ fmtDateTime(it.created_at) }}</span>
        </li>
      </ul>
      <p class="muted payments-note">Payments are listed under Payment History.</p>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { formatDateTime } from '../composables/useFormatters'

const props = defineProps({
  /** `items` from the activity endpoint, newest first. */
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  loading: { type: Boolean, default: false },
  /** The activity read failed — say so instead of showing an empty trail. */
  error: { type: Boolean, default: false },
  /** `context.customer_views` from the endpoint: { count, last_at, link_sent_at }. */
  customerViews: { type: Object, default: () => ({ count: 0, last_at: null, link_sent_at: null }) },
  /** Invoice status, any case. "Not opened" is not said of a paid or void invoice. */
  status: { type: String, default: '' },

})

// Worded to what core/customer_views.py actually filters: known bot user
// agents, anything in the first 90 seconds after the invoice was sent, and
// repeats within 30 minutes. A scanner that follows a link later, or anyone
// else holding the link, still counts — so "opened" is a strong hint, not proof.
const VIEW_HINT =
  'Counted when someone follows the emailed or texted link to the invoice. ' +
  'Known bots, loads in the first 90 seconds after sending, and repeats within 30 minutes are ignored; ' +
  'a mail scanner that checks the link later can still be counted. Recorded since July 29, 2026.'

const viewCount = computed(() => Number(props.customerViews?.count) || 0)
const lastViewAt = computed(() => props.customerViews?.last_at || null)

// "Not opened" is said only when the server confirms a pay link actually
// reached the customer (link_sent_at: an email that carried the link, or a
// text, sent while views were recorded) and the invoice still awaits them.
// A mailed paper invoice, a "marked sent", an old send or a paid invoice
// never qualifies — on prod, 2026-09-30, guessing from sent_at would have
// told the office 300+ customers ignored a link most of them never had.
const showNotOpened = computed(() => {
  if (viewCount.value > 0 || !props.customerViews?.link_sent_at) return false
  const st = String(props.status || '').toLowerCase()
  return !['paid', 'void', 'draft'].includes(st)
})

function fmtDateTime(v) {
  if (!v) return ''
  try {
    return formatDateTime(v) || String(v)
  } catch {
    return String(v)
  }
}

// routers/invoice_reminders.py REMINDER_STAGES
const STAGE_LABEL = {
  friendly: 'Friendly reminder',
  first_reminder: 'First reminder',
  second_reminder: 'Second reminder',
  final_notice: 'Final notice',
  collections: 'Collections notice',
}

/** The one detail worth a second line, per action. */
function detailLine(it) {
  const d = it?.details || {}
  if (it.action === 'invoice_email_rejected' && d.failed_recipient) return `to ${d.failed_recipient}`
  if (it.action === 'email_failed') return d.skip_reason ? `Reason: ${d.skip_reason}` : ''
  if (it.action === 'email_bounced' && d.to_email) return `to ${d.to_email}`
  if (it.action === 'invoice_marked_sent' && d.channel && d.channel !== 'manual') return `via ${d.channel}`
  if (it.action === 'payment_reminder_skipped') {
    return d.skip_reason ? `Skipped — ${d.skip_reason}` : 'Skipped'
  }
  if (it.action === 'payment_reminder' && d.stage) return STAGE_LABEL[d.stage] || d.stage
  return ''
}
</script>

<style scoped>
.inv-activity { display: flex; flex-direction: column; gap: 0.5rem; margin: 0.25rem 0 1rem; }
.view-line { margin: 0; display: flex; align-items: center; gap: 0.4rem; color: var(--p-text-muted-color, #6b7280); cursor: help; }
.view-line.viewed { color: var(--p-text-color, inherit); font-weight: 500; }
.activity-panel summary { cursor: pointer; font-weight: 600; }
.activity-list { list-style: none; margin: 0.5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.activity-row { display: grid; grid-template-columns: 1fr auto; gap: 0.15rem 1rem; padding: 0.35rem 0; border-bottom: 1px solid var(--p-content-border-color, rgba(128,128,128,0.2)); }
.activity-label { font-weight: 500; }
.activity-detail { grid-column: 1 / -1; color: var(--p-text-muted-color, #6b7280); font-size: 0.9em; }
.activity-meta { color: var(--p-text-muted-color, #6b7280); font-size: 0.85em; white-space: nowrap; }
.payments-note { font-size: 0.85em; margin: 0.5rem 0 0; }
.muted { color: var(--p-text-muted-color, #6b7280); }
@media (max-width: 640px) {
  .activity-row { grid-template-columns: 1fr; }
  .activity-meta { white-space: normal; }
}
</style>
