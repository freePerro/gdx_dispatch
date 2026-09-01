<!--
  EstimateStatusContext — the story behind an estimate's status tag, and
  its activity trail.

  estimate-rejection-visibility plan, PR 1. Every status path already wrote
  a complete audit row; the estimate page showed none of it. A bounce set
  `rejected` and the office saw a red tag it could not explain — five days
  later the person who asked for bounce detection could not tell what the
  word meant, and the customer still had not seen the estimate.

  Three things, all read from GET /api/estimates/{id}/activity:
  - status = rejected  → a warn banner naming the failed recipient and date,
    with the two actions that fix it: "Fix customer email" (the customer
    record) and "Re-send" (the existing composer — /send and /mark-sent
    already accept `rejected`; the banner is the visible way onto them).
  - status = declined  → one line: when, by whom, why. `declined_reason`
    was fetched and never rendered before this.
  - the activity list — who did what, when. The audit trail, on the page.

  Dumb by design: props in, events out. EstimateView owns the fetch and the
  handlers, so this can be mount-tested with real assertions.
-->
<template>
  <div class="est-status-context" data-testid="estimate-status-context">
    <Message
      v-if="isRejected"
      severity="warn"
      :closable="false"
      class="bounce-banner"
      data-testid="estimate-bounce-banner"
    >
      <div class="banner-body">
        <p class="banner-text">
          <strong>Failed Email.</strong>
          <template v-if="bounce?.failed_recipient">
            The estimate email to <strong data-testid="bounce-recipient">{{ bounce.failed_recipient }}</strong>
            bounced<template v-if="bounce.at"> on {{ fmtDateTime(bounce.at) }}</template>.
          </template>
          <template v-else>The estimate email bounced.</template>
          The customer never received it — fix the address, then re-send.
        </p>
        <div class="banner-actions">
          <Button
            label="Fix customer email"
            icon="pi pi-user-edit"
            size="small"
            severity="secondary"
            :disabled="!customerId"
            data-testid="estimate-fix-email"
            @click="$emit('fix-email')"
          />
          <Button
            label="Re-send"
            icon="pi pi-send"
            size="small"
            data-testid="estimate-resend"
            @click="$emit('resend')"
          />
        </div>
      </div>
    </Message>

    <div v-else-if="isDeclined" class="decline-strip" data-testid="estimate-decline-strip">
      <i class="pi pi-times-circle" aria-hidden="true" />
      <span>{{ declineLine }}</span>
    </div>

    <details class="activity-panel" data-testid="estimate-activity">
      <summary>Activity <span class="muted">({{ total }})</span></summary>
      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="!items.length" class="muted" data-testid="estimate-activity-empty">No activity recorded.</p>
      <ul v-else class="activity-list">
        <li v-for="it in items" :key="it.id" class="activity-row" :data-action="it.action">
          <span class="activity-label">{{ it.label || it.action }}</span>
          <span v-if="detailLine(it)" class="activity-detail">{{ detailLine(it) }}</span>
          <span class="activity-meta">{{ it.user_name || 'System' }} · {{ fmtDateTime(it.created_at) }}</span>
        </li>
      </ul>
    </details>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { formatDateTime } from '../composables/useFormatters'

const props = defineProps({
  /** Backend enum value in any case ("rejected" / "Rejected"). */
  status: { type: String, default: '' },
  /** `context` from the activity endpoint: { bounce, decline }. */
  context: { type: Object, default: () => ({ bounce: null, decline: null }) },
  /** `items` from the activity endpoint, newest first. */
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  loading: { type: Boolean, default: false },
  /** Enables "Fix customer email"; null when the estimate has no customer. */
  customerId: { type: String, default: null },
})
defineEmits(['resend', 'fix-email'])

const norm = computed(() => String(props.status || '').toLowerCase())
const isRejected = computed(() => norm.value === 'rejected')
const isDeclined = computed(() => norm.value === 'declined')
const bounce = computed(() => props.context?.bounce || null)
const decline = computed(() => props.context?.decline || null)

const declineLine = computed(() => {
  const d = decline.value || {}
  let line = 'Declined'
  if (d.at) line += ` ${fmtDateTime(d.at)}`
  if (d.user_name) line += ` by ${d.user_name}`
  if (d.reason) line += ` — ${d.reason}`
  return line
})

function fmtDateTime(v) {
  if (!v) return ''
  try {
    return formatDateTime(v) || String(v)
  } catch {
    return String(v)
  }
}

/** The one detail worth a second line, per action. */
function detailLine(it) {
  const d = it?.details || {}
  if (it.action === 'estimate_email_rejected' && d.failed_recipient) return `to ${d.failed_recipient}`
  if (it.action === 'estimate_resend_detected' && d.recipient) return `to ${d.recipient}`
  if (String(it.action || '').endsWith('_declined') && d.reason) return `“${d.reason}”`
  if (it.action === 'estimate_marked_sent' && d.channel && d.channel !== 'manual') return `via ${d.channel}`
  return ''
}
</script>

<style scoped>
.est-status-context { display: flex; flex-direction: column; gap: 0.6rem; margin-top: 0.5rem; }
.banner-body { display: flex; flex-wrap: wrap; align-items: center; gap: 0.75rem 1.25rem; }
.banner-text { margin: 0; flex: 1 1 22rem; line-height: 1.45; }
.banner-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.decline-strip {
  display: flex; align-items: center; gap: 0.5rem;
  padding: 0.5rem 0.75rem; border-radius: var(--p-border-radius, 6px);
  background: var(--p-red-50, rgba(220, 38, 38, 0.08));
  color: var(--p-red-700, #b91c1c);
}
:root[data-theme="dark"] .decline-strip,
[data-theme="dark"] .decline-strip {
  background: rgba(248, 113, 113, 0.12);
  color: var(--p-red-300, #fca5a5);
}
.activity-panel summary { cursor: pointer; font-weight: 600; }
.activity-list { list-style: none; margin: 0.5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.activity-row { display: grid; grid-template-columns: 1fr auto; gap: 0.15rem 1rem; padding: 0.35rem 0; border-bottom: 1px solid var(--p-content-border-color, rgba(128,128,128,0.2)); }
.activity-label { font-weight: 500; }
.activity-detail { grid-column: 1 / -1; color: var(--p-text-muted-color, #6b7280); font-size: 0.9em; }
.activity-meta { color: var(--p-text-muted-color, #6b7280); font-size: 0.85em; white-space: nowrap; }
.muted { color: var(--p-text-muted-color, #6b7280); }
@media (max-width: 640px) {
  .activity-row { grid-template-columns: 1fr; }
  .activity-meta { white-space: normal; }
}
</style>
