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
import AuthedImage from './AuthedImage.vue'
import { formatMoney } from '../composables/useFormatters'
import { useTenantTimezone } from '../composables/useTenantTimezone'
import { invoiceStatusSeverity as statusSeverity } from '../utils/statusSeverity'

const props = defineProps({
  visible: { type: Boolean, default: false },
  job: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'invoiced'])

const api = useApi()
const { zonedDateKey } = useTenantTimezone()
const toast = useToast()

const open = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const summary = ref(null)
const loading = ref(false)
const submitting = ref(false)

// Job photos the tech can print on this invoice. Read from job_photos — the
// same endpoint the office surfaces use, so what the tech ticks is exactly
// what the office would see.
const jobPhotos = ref([])
const attachedPhotoIds = ref([])

// immediate: the dialog can be mounted already-visible.
watch(() => props.visible, (v) => {
  if (v && props.job?.id) {
    loadSummary()
    loadJobPhotos()
  }
}, { immediate: true })

async function loadJobPhotos() {
  jobPhotos.value = []
  attachedPhotoIds.value = []
  try {
    const rows = await api.get(`/api/jobs/${props.job.id}/photos`, { suppressErrorToast: true })
    jobPhotos.value = Array.isArray(rows) ? rows : []
  } catch {
    // Silent: photos are an addition to the invoice, and a tech mid-signature
    // does not need a toast about a gallery. The block simply doesn't render.
    jobPhotos.value = []
  }
}

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
// A deposit invoice is money BEFORE the work — it must not read as "the job
// is billed" or the Generate button disappears and a deposit-taking job can
// never be final-billed from the truck (same exclusion as
// core/billing_predicates.job_billed_exists).
const hasFinalInvoice = computed(() =>
  (summary.value?.invoices || []).some((inv) => (inv.billing_type || 'standard') !== 'deposit'),
)

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
      // carry the day it was actually taken, not the replay day. Tenant-zone
      // day, not a UTC slice — toISOString() after ~7 PM Central is tomorrow.
      date: zonedDateKey(new Date()),
      reference: payReference.value.trim() || null,
    }
    const r = await api.postQueued(`/api/invoices/${inv.id}/payments`, payload, {
      actionType: 'invoice.payment', resourceId: String(inv.id),
      // A payments 409 is a business refusal (void invoice, closed-out
      // deposit, locked GL period) — never a dedup verdict. Surface it.
      conflictIsError: true,
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
      attached_photo_ids: attachedPhotoIds.value,
    }
    const inv = await api.post(`/api/mobile/jobs/${props.job.id}/invoice`, payload)
    toast.add({ severity: 'success', summary: 'Invoice sent', detail: `#${inv.invoice_number} emailed`, life: 3000 })
    // Deposit netting — same story InvoiceCreateView tells the office. The
    // unapplied case (deposit exceeds the final total) needs a human; until
    // now the truck never heard about it.
    const net = inv.deposit_netting
    if (net && !net.skipped) {
      const parts = []
      if (Number(net.deposit_paid_applied) > 0) parts.push(`${fmtMoney(net.deposit_paid_applied)} deposit applied`)
      if ((net.superseded || []).length) parts.push(`superseded ${net.superseded.join(', ')}`)
      if ((net.voided || []).length) parts.push(`voided unpaid ${net.voided.join(', ')}`)
      const unapplied = Number(net.deposit_unapplied) > 0
      if (unapplied) parts.push(`${fmtMoney(net.deposit_unapplied)} deposit NOT applied — tell the office`)
      if (parts.length) {
        toast.add({
          severity: unapplied ? 'warn' : 'info',
          summary: 'Deposit netting',
          detail: parts.join(' · '),
          life: unapplied ? 10000 : 6000,
        })
      }
    }
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

</script>

<template>
  <!-- "Bill / collect", not "Close out": the job screen puts this button right
       next to Complete, which opens the ACTUAL close-out dialog. Two adjacent
       buttons opening two dialogs both titled "Close out" is how a tech
       invoices a job they meant to finish. -->
  <Dialog
    v-model:visible="open"
    header="Bill / collect"
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
            <Tag
              v-if="inv.billing_type === 'deposit'"
              value="deposit"
              severity="info"
              data-testid="mid-deposit-tag"
            />
            <!-- Plan §11: an invisible gate reads as a broken button and gets
                 retried (the §4 lesson) — say WHY Send is unavailable. -->
            <Tag
              v-if="!inv.verified_at"
              value="awaiting office verification"
              severity="warn"
              data-testid="mid-awaiting-verification"
            />
          </div>
          <div class="invoice-totals">
            <span>{{ fmtMoney(inv.total) }}</span>
            <span v-if="inv.balance_due > 0" class="muted">
              · {{ fmtMoney(inv.balance_due) }} due
            </span>
          </div>
          <div class="invoice-actions">
            <Button
              v-if="inv.verified_at"
              label="Re-send"
              icon="pi pi-envelope"
              size="small"
              text
              :loading="submitting"
              @click="resendInvoice(inv)"
            />
            <span v-else class="muted" data-testid="mid-send-blocked">
              Send unlocks when the office verifies the hours.
            </span>
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

      <!-- Job photos on the invoice PDF (2026-08-12). The tech took these
           minutes ago and is about to email the bill; this is the only moment
           anyone can put the finished-door shot on it before the customer
           reads it. Hidden once a final invoice exists — the picks ride the
           invoice being generated, not one already sent. -->
      <div v-if="!hasFinalInvoice && jobPhotos.length" class="photo-pick-block" data-testid="mid-photos">
        <p class="photo-pick-title">Photos to include on the invoice</p>
        <div class="photo-pick-row">
          <label
            v-for="p in jobPhotos"
            :key="p.id"
            class="photo-pick"
            :class="{ selected: attachedPhotoIds.includes(p.id) }"
            :data-testid="`mid-photo-${p.id}`"
          >
            <input type="checkbox" :value="p.id" v-model="attachedPhotoIds" />
            <AuthedImage :src="p.url" :alt="p.caption || p.kind || 'Job photo'">
              <template #fallback><span class="photo-pick-failed">n/a</span></template>
            </AuthedImage>
            <span class="photo-pick-kind">{{ p.kind || 'photo' }}</span>
          </label>
        </div>
      </div>
    </div>

    <template #footer>
      <Button label="Close" text @click="open = false" />
      <Button
        v-if="!hasFinalInvoice"
        :label="hasAcceptedQuote ? 'Generate & email invoice' : 'Generate empty invoice'"
        icon="pi pi-send"
        severity="success"
        :loading="submitting"
        :disabled="!summary"
        data-testid="mid-generate"
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

/* Photo picker — thumb-sized targets, scrolling sideways rather than wrapping
   into a wall on a 390px screen. The whole tile is the tap target (44px+). */
.photo-pick-block { margin-top: 0.7rem; }
.photo-pick-title { margin: 0 0 0.4rem; font-size: 0.85rem; font-weight: 600; }
.photo-pick-row {
  display: flex; gap: 0.5rem; overflow-x: auto; padding-bottom: 0.2rem;
  scrollbar-width: none;
}
.photo-pick-row::-webkit-scrollbar { display: none; }
.photo-pick {
  flex: 0 0 auto; width: 88px; min-height: 44px; padding: 0.3rem;
  display: flex; flex-direction: column; align-items: center; gap: 0.25rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb); border-radius: 0.5rem;
}
.photo-pick.selected { border-color: var(--p-primary-color, #3b82f6); border-width: 2px; }
.photo-pick :deep(img) {
  width: 100%; height: 60px; object-fit: cover; border-radius: 0.25rem; display: block;
}
.photo-pick-failed {
  display: flex; align-items: center; justify-content: center;
  width: 100%; height: 60px; border-radius: 0.25rem;
  background: var(--p-highlight-background, #f3f4f6);
  color: var(--p-text-muted-color, #6b7280); font-size: 0.7rem;
}
.photo-pick-kind { font-size: 0.7rem; color: var(--p-text-muted-color, #6b7280); }
</style>
