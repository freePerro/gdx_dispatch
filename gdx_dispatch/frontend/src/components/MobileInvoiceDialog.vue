<script setup>
// Sprint tech_mobile Phase 2.2 — On-Site Invoicing.
//
// Tech opens this from a completed job (or anytime after a quote is
// accepted). Shows the financial summary (parts cost, labor hours,
// accepted-quote total, existing invoices), then a one-tap "Generate &
// email" button that calls POST /api/mobile/jobs/{id}/invoice.
//
// No payment capture (deferred per the sprint plan) — we just send the
// invoice; office reconciles the money. Send-receipt is available when
// the office has already recorded a payment.
import { computed, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

const props = defineProps({
  visible: { type: Boolean, default: false },
  job: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'invoiced'])

const api = useApi()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const summary = ref(null)
const loading = ref(false)
const submitting = ref(false)

watch(() => props.visible, (v) => {
  if (v && props.job?.id) loadSummary()
})

async function loadSummary() {
  loading.value = true
  try {
    summary.value = await api.get(`/api/mobile/jobs/${props.job.id}/financial`)
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not load summary', detail: e.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

function fmtMoney(n) { return `$${(Number(n) || 0).toFixed(2)}` }

const hasAcceptedQuote = computed(() => !!summary.value?.accepted_quote)
const hasInvoice = computed(() => (summary.value?.invoices || []).length > 0)

async function generateInvoice() {
  if (!props.job?.id) return
  submitting.value = true
  try {
    const payload = {
      estimate_id: summary.value?.accepted_quote?.id || null,
      send_email: true,
    }
    const inv = await api.post(`/api/mobile/jobs/${props.job.id}/invoice`, payload)
    toast.add({ severity: 'success', summary: 'Invoice sent', detail: `#${inv.invoice_number} emailed`, life: 3000 })
    emit('invoiced', inv)
    await loadSummary()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not invoice', detail: e.message, life: 5000 })
  } finally {
    submitting.value = false
  }
}

async function resendInvoice(inv) {
  submitting.value = true
  try {
    await api.post(`/api/mobile/invoices/${inv.id}/send`, {})
    toast.add({ severity: 'success', summary: 'Invoice re-sent', life: 2500 })
    await loadSummary()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not re-send', detail: e.message, life: 4000 })
  } finally {
    submitting.value = false
  }
}

async function sendReceipt(inv) {
  submitting.value = true
  try {
    await api.post(`/api/mobile/invoices/${inv.id}/send-receipt`, {})
    toast.add({ severity: 'success', summary: 'Receipt sent', life: 2500 })
  } catch (e) {
    const msg = e.message || 'Receipt failed'
    if (msg.includes('no payment')) {
      toast.add({ severity: 'warn', summary: 'No payment recorded', detail: 'Office must record the payment first.', life: 5000 })
    } else {
      toast.add({ severity: 'error', summary: 'Could not send receipt', detail: msg, life: 5000 })
    }
  } finally {
    submitting.value = false
  }
}

function statusSeverity(s) {
  if (s === 'paid') return 'success'
  if (s === 'sent') return 'info'
  if (s === 'overdue') return 'danger'
  return 'secondary'
}
</script>

<template>
  <Dialog
    v-model:visible="open"
    header="Close out"
    modal
    :style="{ width: '94vw', maxWidth: '480px' }"
  >
    <div v-if="loading" class="inv-loading">
      <i class="pi pi-spin pi-spinner" /> Loading…
    </div>

    <div v-else-if="summary" class="inv-summary">
      <div class="kv-row">
        <span>Parts cost</span>
        <strong>{{ fmtMoney(summary.parts_cost) }}</strong>
      </div>
      <div class="kv-row">
        <span>Labor hours</span>
        <strong>{{ summary.labor_hours }}</strong>
      </div>

      <div v-if="hasAcceptedQuote" class="kv-row hi">
        <span>Accepted quote</span>
        <strong>{{ fmtMoney(summary.accepted_quote.total) }}</strong>
      </div>
      <div v-else class="muted inv-no-quote">
        No accepted quote yet — invoice will start at $0 and office can
        add lines.
      </div>

      <div v-if="hasInvoice" class="invoice-list">
        <div class="invoice-list-head">Invoices</div>
        <div
          v-for="inv in summary.invoices"
          :key="inv.id"
          class="invoice-item"
        >
          <div class="invoice-num">
            <strong>#{{ inv.invoice_number }}</strong>
            <Tag :value="inv.status" :severity="statusSeverity(inv.status)" />
          </div>
          <div class="invoice-totals">
            <span>{{ fmtMoney(inv.total) }}</span>
            <span v-if="inv.balance_due > 0" class="muted">
              · {{ fmtMoney(inv.balance_due) }} due
            </span>
          </div>
          <div class="invoice-actions">
            <Button
              label="Re-send"
              icon="pi pi-envelope"
              size="small"
              text
              :loading="submitting"
              @click="resendInvoice(inv)"
            />
            <Button
              label="Send receipt"
              icon="pi pi-receipt"
              size="small"
              text
              :loading="submitting"
              @click="sendReceipt(inv)"
            />
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <Button label="Close" text @click="open = false" />
      <Button
        v-if="!hasInvoice"
        :label="hasAcceptedQuote ? 'Generate & email invoice' : 'Generate empty invoice'"
        icon="pi pi-send"
        severity="success"
        :loading="submitting"
        :disabled="!summary"
        @click="generateInvoice"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.inv-loading { padding: 1rem; text-align: center; color: var(--p-text-muted-color); }
.inv-summary { display: flex; flex-direction: column; gap: 0.4rem; }
.kv-row {
  display: flex;
  justify-content: space-between;
  padding: 0.5rem 0.85rem;
  background: var(--p-highlight-background, #f3f4f6);
  border-radius: 0.5rem;
  font-size: 0.95rem;
}
.kv-row.hi { background: #ecfdf5; color: #065f46; }
.inv-no-quote {
  background: #fef3c7;
  border: 1px solid #fcd34d;
  border-radius: 0.5rem;
  padding: 0.5rem 0.85rem;
  font-size: 0.8rem;
  color: #92400e;
}

.invoice-list { margin-top: 0.6rem; border-top: 1px dashed var(--p-content-border-color); padding-top: 0.6rem; }
.invoice-list-head { font-size: 0.75rem; color: var(--p-text-muted-color); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }
.invoice-item {
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  margin-bottom: 0.45rem;
  background: white;
}
.invoice-num { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.2rem; }
.invoice-totals { font-size: 0.85rem; }
.invoice-actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; }

.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.8rem; }
</style>
