<script setup>
// Sprint tech_mobile Phase 2.2 — On-Site Invoicing.
//
// Tech opens this from a completed job (or anytime after a quote is
// accepted). Shows the financial summary (parts cost, labor hours,
// accepted-quote total, existing invoices), then a one-tap "Generate &
// email" button that calls POST /api/mobile/jobs/{id}/invoice.
//
// 2026-07-01 UX audit — field payment capture (cash/check). "Can I pay you
// now?" used to end with "no, the office will invoice you." The tech can
// now record a cash/check payment against an invoice with a balance due
// (POST /api/invoices/{id}/payments — same endpoint the office uses), then
// send the receipt on the spot. Card-in-field remains out of scope (no
// reader hardware); the emailed invoice is the customer's online pay path.
import { computed, ref, watch } from 'vue'
import Dialog from 'primevue/dialog'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { formatMoney } from '../composables/useFormatters'

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

// immediate: the dialog can be mounted already-visible.
watch(() => props.visible, (v) => {
  if (v && props.job?.id) loadSummary()
}, { immediate: true })

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

function fmtMoney(n) { return formatMoney(Number(n) || 0) }

const hasAcceptedQuote = computed(() => !!summary.value?.accepted_quote)
const hasInvoice = computed(() => (summary.value?.invoices || []).length > 0)

// ─── Field payment capture (cash / check) ────────────────────────────
const payingInvoiceId = ref(null)   // invoice id whose pay form is open
const payMethod = ref('cash')
const payAmount = ref(null)
const payReference = ref('')
const recordingPayment = ref(false)
const PAY_METHODS = [
  { label: 'Cash', value: 'cash' },
  { label: 'Check', value: 'check' },
]

function openPayForm(inv) {
  payingInvoiceId.value = inv.id
  payMethod.value = 'cash'
  payAmount.value = Number(inv.balance_due) || Number(inv.total) || null
  payReference.value = ''
}

function closePayForm() {
  payingInvoiceId.value = null
}

async function recordPayment(inv) {
  const amount = Number(payAmount.value)
  if (!(amount > 0) || recordingPayment.value) return
  recordingPayment.value = true
  try {
    const payload = {
      amount,
      method: payMethod.value,
      // Client-side date is deliberate: a payment collected offline must
      // carry the day it was actually taken, not the replay day.
      date: new Date().toISOString().slice(0, 10),
      reference: payReference.value.trim() || null,
    }
    const r = await api.postQueued(`/api/invoices/${inv.id}/payments`, payload, {
      actionType: 'invoice.payment', resourceId: String(inv.id),
    })
    if (r?.queued) {
      toast.add({
        severity: 'warn',
        summary: 'Payment saved offline',
        detail: 'No signal — it will post to the invoice when you reconnect.',
        life: 5000,
      })
    } else {
      toast.add({
        severity: 'success',
        summary: 'Payment recorded',
        detail: `${fmtMoney(amount)} ${payMethod.value} on #${inv.invoice_number}`,
        life: 3500,
      })
    }
    closePayForm()
    await loadSummary()
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Could not record payment', detail: e.message, life: 5000 })
  } finally {
    recordingPayment.value = false
  }
}

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
            <Button
              v-if="inv.balance_due > 0 && payingInvoiceId !== inv.id"
              label="Record payment"
              icon="pi pi-dollar"
              size="small"
              severity="success"
              text
              data-testid="mid-open-pay"
              @click="openPayForm(inv)"
            />
          </div>

          <!-- 2026-07-01 — inline cash/check capture at the customer's door -->
          <div v-if="payingInvoiceId === inv.id" class="pay-form" data-testid="mid-pay-form">
            <SelectButton
              v-model="payMethod"
              :options="PAY_METHODS"
              optionLabel="label"
              optionValue="value"
              :allowEmpty="false"
              aria-label="Payment method"
            />
            <InputNumber
              v-model="payAmount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              placeholder="Amount"
              inputId="mid-pay-amount"
              data-testid="mid-pay-amount"
            />
            <InputText
              v-if="payMethod === 'check'"
              v-model="payReference"
              placeholder="Check #"
              data-testid="mid-pay-ref"
            />
            <div class="pay-form-actions">
              <Button label="Cancel" size="small" text severity="secondary" @click="closePayForm" />
              <Button
                :label="`Record ${payMethod}`"
                icon="pi pi-check"
                size="small"
                severity="success"
                :disabled="!(Number(payAmount) > 0)"
                :loading="recordingPayment"
                data-testid="mid-pay-submit"
                @click="recordPayment(inv)"
              />
            </div>
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
.kv-row.hi { background: var(--color-success-bg); color: var(--color-success-500); }
.inv-no-quote {
  background: var(--color-warning-bg);
  border: 1px solid var(--color-warning-border);
  border-radius: 0.5rem;
  padding: 0.5rem 0.85rem;
  font-size: 0.8rem;
  color: var(--color-warning-500);
}

.invoice-list { margin-top: 0.6rem; border-top: 1px dashed var(--p-content-border-color); padding-top: 0.6rem; }
.invoice-list-head { font-size: 0.75rem; color: var(--p-text-muted-color); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.4rem; }
.invoice-item {
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  margin-bottom: 0.45rem;
  background: var(--p-content-background, var(--surface-panel));
}
.pay-form {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 0.5rem;
  padding: 0.6rem;
  border: 1px dashed var(--border-strong);
  border-radius: 0.5rem;
}
.pay-form :deep(input) { min-height: 44px; font-size: 1rem; }
.pay-form-actions { display: flex; justify-content: flex-end; gap: 0.4rem; }
.invoice-num { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.2rem; }
.invoice-totals { font-size: 0.85rem; }
.invoice-actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; }

.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.8rem; }
</style>
