<template>
    <section class="mobile-estimates">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Estimates</h1>
          <div class="head-actions">
            <Button
              v-if="canCreateEstimate"
              label="New"
              icon="pi pi-plus"
              size="small"
              data-testid="mobile-estimates-new-btn"
              @click="createEstimate"
            />
            <Button v-tooltip="'Refresh'" icon="pi pi-refresh" aria-label="Refresh" text size="small" :loading="loading" @click="fetchEstimates" data-test="me-refresh" />
          </div>
        </div>
        <SelectButton
          v-model="filter"
          :options="FILTERS"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          aria-label="Filter"
          class="filter-switch"
        />
      </header>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading && !estimates.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading estimates…</span>
      </div>
      <div v-else-if="!visibleEstimates.length" class="state-msg">
        <i class="pi pi-file-edit empty-icon" />
        <div class="empty-title">{{ emptyTitle }}</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="e in visibleEstimates"
          :key="e.id"
          class="est-card"
          @click="openDetail(e)"
          data-test="me-est-row"
        >
          <div class="est-row">
            <span class="est-customer">{{ e.customer_name || e.customer?.name || '—' }}</span>
            <span class="est-total">${{ fmtMoney(e.total) }}</span>
          </div>
          <div class="est-row-2">
            <span class="est-number">#{{ e.number || e.estimate_number || (e.id || '').slice(0, 8) }}</span>
            <Tag :value="prettyStatus(e.status)" :severity="statusSeverity(e.status)" />
          </div>
          <div v-if="e.title" class="est-title">{{ e.title }}</div>
        </li>
      </ol>

      <!-- Detail dialog -->
      <Dialog
        v-model:visible="detailOpen"
        :header="detail ? `Estimate #${detail.number || detail.estimate_number || ''}` : 'Estimate'"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
      >
        <div v-if="detailLoading" class="state-msg">
          <i class="pi pi-spin pi-spinner" />
        </div>
        <div v-else-if="detail" class="detail-body">
          <div class="detail-meta">
            <div class="meta-line"><strong>Customer:</strong> {{ detail.customer_name || detail.customer?.name || '—' }}</div>
            <div class="meta-line"><strong>Status:</strong>
              <Tag :value="prettyStatus(detail.status)" :severity="statusSeverity(detail.status)" />
            </div>
            <div v-if="detail.title" class="meta-line"><strong>Title:</strong> {{ detail.title }}</div>
            <div v-if="detail.notes" class="meta-line muted">{{ detail.notes }}</div>
          </div>

          <h3 class="lines-heading">Line items</h3>
          <ol v-if="detail.lines?.length" class="lines-list">
            <li v-for="(line, i) in detail.lines" :key="line.id || i" class="line-row">
              <div class="line-name">{{ line.description || line.name || '—' }}</div>
              <div class="line-meta">
                <span>{{ line.quantity || 1 }} × ${{ fmtMoney(line.unit_price) }}</span>
                <span class="line-total">${{ fmtMoney(line.total ?? (Number(line.quantity || 1) * Number(line.unit_price || 0))) }}</span>
              </div>
            </li>
          </ol>
          <p v-else class="muted">No line items.</p>

          <div class="totals">
            <div class="total-row" v-if="detail.subtotal != null"><span>Subtotal</span><span>${{ fmtMoney(detail.subtotal) }}</span></div>
            <div class="total-row" v-if="detail.tax_amount != null"><span>Tax</span><span>${{ fmtMoney(detail.tax_amount) }}</span></div>
            <div class="total-row total-grand"><span>Total</span><span>${{ fmtMoney(detail.total) }}</span></div>
          </div>

          <div v-if="hasSignature" class="signature-block">
            <h3>Signature</h3>
            <img :src="hasSignature" alt="Customer signature" class="signature-img" />
          </div>
        </div>
        <template #footer>
          <Button v-if="detail && canAccept" label="Accept" icon="pi pi-check" :loading="actionSaving" @click="accept" data-test="me-accept" />
          <Button v-if="detail && canAccept" label="Decline" icon="pi pi-times" severity="danger" text :loading="actionSaving" @click="decline" data-test="me-decline" />
          <Button v-if="detail && canSign" label="Sign" icon="pi pi-pencil" severity="secondary" @click="openSign" data-test="me-sign-open" />
          <Button label="Close" severity="secondary" @click="closeDetail" />
        </template>
      </Dialog>

      <!-- Signature dialog -->
      <Dialog
        v-model:visible="signOpen"
        header="Customer signature"
        modal
        :style="{ width: '100vw', height: '100dvh' }"
        :breakpoints="{ '768px': '100vw' }"
        position="bottom"
        @hide="resetSignature"
      >
        <div class="form-stack">
          <div>
            <label>Signed by *</label>
            <InputText v-model="sigForm.signed_by" placeholder="Customer name" class="w-full" data-test="me-sign-name" />
          </div>
          <div>
            <label>Email (optional)</label>
            <InputText v-model="sigForm.signed_by_email" type="email" class="w-full" data-test="me-sign-email" />
          </div>
          <div>
            <label>Sign here</label>
            <div class="sig-canvas-wrap">
              <canvas
                ref="sigCanvas"
                class="sig-canvas"
                :width="canvasSize.w"
                :height="canvasSize.h"
                @pointerdown="sigStart"
                @pointermove="sigMove"
                @pointerup="sigEnd"
                @pointerleave="sigEnd"
                data-test="me-sign-canvas"
              />
              <button type="button" class="sig-clear" @click="clearCanvas">Clear</button>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="signOpen = false" />
          <Button
            label="Save signature"
            icon="pi pi-check"
            :loading="sigSaving"
            :disabled="!sigForm.signed_by.trim() || !sigDrawn"
            @click="submitSignature"
            data-test="me-sign-submit"
          />
        </template>
      </Dialog>

      <!-- Deposit prompt at accept — mobile is opt-IN per accept (parity with
           MobileCustomerQuoteDialog); the tenant % only prefills the amount. -->
      <Dialog
        v-model:visible="depositPromptOpen"
        header="Accept estimate"
        modal
        :style="{ width: '94vw', maxWidth: '420px' }"
      >
        <div class="form-stack" data-test="me-deposit-prompt">
          <div class="deposit-toggle-row">
            <label for="me-collect-deposit">Collect a deposit now?</label>
            <ToggleSwitch v-model="collectDeposit" inputId="me-collect-deposit" data-test="me-deposit-toggle" />
          </div>
          <div v-if="collectDeposit">
            <label>Deposit amount</label>
            <InputNumber
              v-model="depositAmount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              class="w-full"
              data-test="me-deposit-amount"
            />
            <small v-if="depositDefault" class="muted">
              Default {{ depositDefault.pct }}% of ${{ fmtMoney(depositDefault.estimate_total) }}
            </small>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="depositPromptOpen = false" />
          <Button
            :label="collectDeposit ? 'Accept + request deposit' : 'Accept'"
            icon="pi pi-check"
            :loading="actionSaving"
            :disabled="collectDeposit && !(Number(depositAmount) > 0)"
            data-test="me-deposit-accept"
            @click="doAccept(collectDeposit ? depositAmount : 0)"
          />
        </template>
      </Dialog>

      <!-- Deposit result — invoice number + pay link, mirrors the quote
           dialog's deposit step. -->
      <Dialog
        :visible="!!depositResult"
        header="Deposit requested"
        modal
        :style="{ width: '94vw', maxWidth: '420px' }"
        @update:visible="(v) => { if (!v) depositResult = null }"
      >
        <div v-if="depositResult" class="form-stack" data-test="me-deposit-result">
          <p>
            Deposit invoice <strong>#{{ depositResult.invoice_number }}</strong> for
            <strong>${{ fmtMoney(depositResult.balance_due ?? depositResult.amount) }}</strong> is due now.
          </p>
          <p v-if="!depositResult.pay_url" class="muted">
            Card payment isn't set up — collect cash/check, or the office can
            record the payment.
          </p>
        </div>
        <template #footer>
          <Button label="Done" severity="secondary" text @click="depositResult = null" />
          <Button
            v-if="depositResult && depositResult.pay_url"
            label="Copy pay link"
            icon="pi pi-copy"
            data-test="me-deposit-copy"
            @click="copyPayLink"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { usePermission } from '../composables/usePermission'
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()
const toast = useToast()
const router = useRouter()
const { hasPermission } = usePermission()

const canCreateEstimate = computed(() => hasPermission('estimates.write'))

function createEstimate() {
  router.push('/estimates/new')
}

const FILTERS = [
  { label: 'Open', value: 'open' },
  { label: 'Sent', value: 'sent' },
  { label: 'All', value: 'all' },
]
const filter = ref('open')

const estimates = ref([])
const loading = ref(false)
const error = ref(null)

const detailOpen = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
const actionSaving = ref(false)

const signOpen = ref(false)
const sigForm = ref({ signed_by: '', signed_by_email: '' })
const sigCanvas = ref(null)
const sigDrawn = ref(false)
const sigSaving = ref(false)
const canvasSize = ref({ w: 320, h: 180 })

const visibleEstimates = computed(() => {
  if (filter.value === 'all') return estimates.value
  if (filter.value === 'sent') return estimates.value.filter((e) => String(e.status).toLowerCase() === 'sent')
  // open = anything not declined / accepted / converted
  return estimates.value.filter((e) => {
    const s = String(e.status || '').toLowerCase()
    return !['declined', 'accepted', 'converted', 'rejected'].includes(s)
  })
})

const emptyTitle = computed(() => {
  if (filter.value === 'sent') return 'Nothing sent'
  if (filter.value === 'all') return 'No estimates yet'
  return 'No open estimates'
})

const canAccept = computed(() => {
  const s = String(detail.value?.status || '').toLowerCase()
  return !['accepted', 'declined', 'converted', 'rejected'].includes(s)
})

const canSign = computed(() => {
  const s = String(detail.value?.status || '').toLowerCase()
  return !['declined', 'rejected', 'converted'].includes(s) && !hasSignature.value
})

const hasSignature = computed(() => detail.value?.signature?.signature_data || detail.value?.signature_data || null)

function fmtMoney(n) {
  const v = Number(n || 0)
  return v.toFixed(2)
}

function prettyStatus(s) {
  if (!s) return '—'
  return String(s).charAt(0).toUpperCase() + String(s).slice(1)
}

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (['accepted', 'converted'].includes(k)) return 'success'
  if (['sent'].includes(k)) return 'info'
  if (['draft', 'pending'].includes(k)) return 'warning'
  if (['declined', 'rejected', 'expired'].includes(k)) return 'danger'
  return 'secondary'
}

async function fetchEstimates() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/estimates')
    estimates.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

async function openDetail(e) {
  detail.value = null
  detailOpen.value = true
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/estimates/${e.id}`)
  } catch (err) {
    error.value = err.message || 'Failed to load estimate'
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detail.value = null
}

// Deposit-at-acceptance (2026-07-24) — parity with MobileCustomerQuoteDialog.
// Mobile stays opt-IN per accept (same-day repairs shouldn't demand 50% up
// front), so the toggle defaults off; the office default % only prefills the
// amount. Accept used to post an empty body here, silently making this the
// one accept surface that could never collect a deposit.
const depositPromptOpen = ref(false)
const depositDefault = ref(null)
const collectDeposit = ref(false)
const depositAmount = ref(null)
const depositResult = ref(null)

async function accept() {
  if (!detail.value) return
  let dflt = null
  try {
    dflt = await api.get(`/api/estimates/${detail.value.id}/deposit-default`, { suppressErrorToast: true })
  } catch {
    dflt = null // deposit prefill is best-effort — never block accepting
  }
  if (dflt && Number(dflt.pct) > 0 && !dflt.existing) {
    depositDefault.value = dflt
    collectDeposit.value = false
    depositAmount.value = Number(dflt.amount) || null
    depositPromptOpen.value = true
    return
  }
  await doAccept(0)
}

async function doAccept(depositAmt) {
  actionSaving.value = true
  try {
    const resp = await api.post(`/api/estimates/${detail.value.id}/accept`, {
      deposit_amount: Number(depositAmt) > 0 ? Number(depositAmt) : 0,
    })
    toast.add({ severity: 'success', summary: 'Estimate accepted', life: 2500 })
    if (resp?.deposit_skipped) {
      toast.add({ severity: 'warn', summary: 'Deposit not created', detail: resp.deposit_skipped, life: 6000 })
    }
    depositPromptOpen.value = false
    if (resp?.deposit) depositResult.value = resp.deposit
    await fetchEstimates()
    detail.value = await api.get(`/api/estimates/${detail.value.id}`)
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Accept failed', detail: err.message, life: 4000 })
  } finally {
    actionSaving.value = false
  }
}

async function copyPayLink() {
  const url = depositResult.value?.pay_url
  if (!url) return
  try {
    await navigator.clipboard.writeText(url)
    toast.add({ severity: 'success', summary: 'Pay link copied', life: 2000 })
  } catch {
    toast.add({ severity: 'warn', summary: 'Could not copy', detail: url, life: 6000 })
  }
}

async function decline() {
  if (!detail.value) return
  if (!(await confirmAsync({ header: 'Confirm', message: 'Decline this estimate?' }))) return
  actionSaving.value = true
  try {
    await api.post(`/api/estimates/${detail.value.id}/decline`, {})
    toast.add({ severity: 'success', summary: 'Estimate declined', life: 2500 })
    await fetchEstimates()
    closeDetail()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Decline failed', detail: err.message, life: 4000 })
  } finally {
    actionSaving.value = false
  }
}

// ── Signature pad ────────────────────────────────────────────────────
let drawing = false
let lastPt = null

function setCanvasSize() {
  const w = Math.min(window.innerWidth - 48, 600)
  canvasSize.value = { w, h: Math.round(w * 0.45) }
}

function openSign() {
  setCanvasSize()
  sigForm.value = { signed_by: detail.value?.customer_name || detail.value?.customer?.name || '', signed_by_email: '' }
  sigDrawn.value = false
  signOpen.value = true
  nextTick(() => {
    const c = sigCanvas.value
    if (c) {
      const ctx = c.getContext('2d')
      ctx.fillStyle = '#fff'
      ctx.fillRect(0, 0, c.width, c.height)
      ctx.strokeStyle = '#111'
      ctx.lineWidth = 2
      ctx.lineCap = 'round'
    }
  })
}

function sigStart(e) {
  drawing = true
  const c = sigCanvas.value
  const rect = c.getBoundingClientRect()
  lastPt = { x: (e.clientX - rect.left) * (c.width / rect.width), y: (e.clientY - rect.top) * (c.height / rect.height) }
  e.preventDefault()
}

function sigMove(e) {
  if (!drawing) return
  const c = sigCanvas.value
  const rect = c.getBoundingClientRect()
  const x = (e.clientX - rect.left) * (c.width / rect.width)
  const y = (e.clientY - rect.top) * (c.height / rect.height)
  const ctx = c.getContext('2d')
  ctx.beginPath()
  ctx.moveTo(lastPt.x, lastPt.y)
  ctx.lineTo(x, y)
  ctx.stroke()
  lastPt = { x, y }
  sigDrawn.value = true
  e.preventDefault()
}

function sigEnd() {
  drawing = false
  lastPt = null
}

function clearCanvas() {
  const c = sigCanvas.value
  if (!c) return
  const ctx = c.getContext('2d')
  ctx.fillStyle = '#fff'
  ctx.fillRect(0, 0, c.width, c.height)
  sigDrawn.value = false
}

function resetSignature() {
  drawing = false
  lastPt = null
  sigDrawn.value = false
}

async function submitSignature() {
  if (!detail.value || !sigDrawn.value || !sigForm.value.signed_by.trim()) return
  sigSaving.value = true
  try {
    const dataUrl = sigCanvas.value.toDataURL('image/png')
    await api.post('/api/signatures', {
      document_type: 'estimate',
      document_id: detail.value.id,
      signature_data: dataUrl,
      signed_by: sigForm.value.signed_by.trim(),
      signed_by_email: sigForm.value.signed_by_email.trim() || null,
    })
    toast.add({ severity: 'success', summary: 'Signature captured', life: 2500 })
    signOpen.value = false
    detail.value = await api.get(`/api/estimates/${detail.value.id}`)
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Save failed', detail: err.message, life: 4000 })
  } finally {
    sigSaving.value = false
  }
}

watch(() => window.innerWidth, setCanvasSize)
onMounted(() => {
  setCanvasSize()
  fetchEstimates()
})
</script>

<style scoped>
.mobile-estimates {
  padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.filter-switch :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  width: 100%;
}

.filter-switch :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
}

.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  margin-bottom: 0.5rem;
  font-size: 0.85rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.est-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.est-row,
.est-row-2 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.est-customer {
  font-weight: 700;
  font-size: 1rem;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.est-total {
  font-weight: 700;
  font-size: 1rem;
  color: var(--p-primary-color, #2563eb);
}

.est-number {
  font-family: monospace;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}

.est-title {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-body {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.detail-meta {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding-bottom: 0.6rem;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
}

.meta-line {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
  flex-wrap: wrap;
}

.lines-heading {
  margin: 0.5rem 0 0;
  font-size: 1rem;
  font-weight: 700;
}

.lines-list {
  list-style: none;
  margin: 0.25rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.line-row {
  background: var(--p-content-hover-background, #f3f4f6);
  border-radius: 0.4rem;
  padding: 0.5rem 0.65rem;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.line-name {
  font-weight: 600;
  font-size: 0.9rem;
}

.line-meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.82rem;
  color: var(--p-text-muted-color, #6b7280);
}

.line-total {
  font-family: monospace;
  font-weight: 700;
  color: var(--p-text-color);
}

.totals {
  margin-top: 0.5rem;
  padding-top: 0.5rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.total-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.9rem;
}

.total-grand {
  font-weight: 800;
  font-size: 1.1rem;
  margin-top: 0.25rem;
  padding-top: 0.4rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}

.signature-block {
  margin-top: 0.75rem;
}

.signature-block h3 {
  margin: 0 0 0.4rem;
  font-size: 1rem;
}

.signature-img {
  max-width: 100%;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.4rem;
  background: #fff;
}

.muted {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.85rem;
}

.state-msg {
  text-align: center;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.empty-icon {
  font-size: 2rem;
  opacity: 0.5;
}

.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
}

.deposit-toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.form-stack label {
  display: block;
  font-size: 0.85rem;
  font-weight: 500;
  margin-bottom: 0.2rem;
}

.w-full {
  width: 100%;
}

.sig-canvas-wrap {
  position: relative;
  background: #fff;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.4rem;
  overflow: hidden;
}

.sig-canvas {
  display: block;
  width: 100%;
  height: auto;
  background: #fff;
  touch-action: none;
}

.sig-clear {
  position: absolute;
  top: 0.4rem;
  right: 0.4rem;
  background: rgba(0, 0, 0, 0.06);
  border: 0;
  border-radius: 0.3rem;
  padding: 0.2rem 0.55rem;
  font-size: 0.8rem;
  cursor: pointer;
}
</style>
