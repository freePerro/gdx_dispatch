<template>
    <section class="mobile-billing">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Billing</h1>
          <Button icon="pi pi-refresh" aria-label="Refresh" text size="small" :loading="loading" @click="fetchInvoices" data-test="mb-refresh" />
        </div>

        <!-- Compact money KPI strip — parity with desktop /billing. -->
        <div class="kpi-strip" data-test="mb-kpis">
          <div class="kpi" :class="{ alert: overdueAmount > 0 }">
            <span class="kpi-label">Outstanding</span>
            <span class="kpi-val">${{ fmtMoney(totalOutstanding) }}</span>
          </div>
          <div class="kpi" :class="{ alert: overdueAmount > 0 }">
            <span class="kpi-label">Overdue</span>
            <span class="kpi-val">${{ fmtMoney(overdueAmount) }}</span>
          </div>
          <div class="kpi">
            <span class="kpi-label">Paid (mo)</span>
            <span class="kpi-val">${{ fmtMoney(paidThisMonth) }}</span>
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

      <div v-if="loading && !invoices.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading…</span>
      </div>
      <div v-else-if="!visibleInvoices.length" class="state-msg">
        <i class="pi pi-dollar empty-icon" />
        <div class="empty-title">{{ emptyTitle }}</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="inv in visibleInvoices"
          :key="inv.id"
          class="inv-card"
          :class="{ overdue: isOverdue(inv) }"
          @click="openDetail(inv)"
          data-test="mb-inv-row"
        >
          <div class="inv-row">
            <span class="inv-customer">{{ inv.customer_name || inv.customer?.name || '—' }}</span>
            <span class="inv-total">${{ fmtMoney(inv.total) }}</span>
          </div>
          <div class="inv-row-2">
            <span class="inv-number">#{{ inv.number || inv.invoice_number || (inv.id || '').slice(0, 8) }}</span>
            <Tag :value="prettyStatus(inv.status)" :severity="statusSeverity(inv.status)" />
          </div>
          <div v-if="inv.due_date" class="inv-due" :class="{ overdue: isOverdue(inv) }">
            <i class="pi pi-calendar" /> Due {{ fmtDate(inv.due_date) }}
            <span v-if="isOverdue(inv)" class="overdue-flag">overdue</span>
          </div>
        </li>
      </ol>

      <Dialog
        v-model:visible="detailOpen"
        :header="detail ? `Invoice #${detail.number || detail.invoice_number || ''}` : 'Invoice'"
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
            <div v-if="detail.due_date" class="meta-line"><strong>Due:</strong> {{ fmtDate(detail.due_date) }}</div>
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
            <div class="total-row" v-if="detail.amount_paid != null"><span>Paid</span><span>${{ fmtMoney(detail.amount_paid) }}</span></div>
            <div class="total-row total-grand"><span>Balance</span><span>${{ fmtMoney(detail.balance_due ?? (Number(detail.total || 0) - Number(detail.amount_paid || 0))) }}</span></div>
          </div>
        </div>
        <template #footer>
          <Button v-if="detail && canSend" label="Send" icon="pi pi-send" :loading="actionSaving" @click="sendInvoice" data-test="mb-send" />
          <Button v-if="detail && canMarkPaid" label="Mark paid" icon="pi pi-check" severity="success" :loading="actionSaving" @click="markPaid" data-test="mb-mark-paid" />
          <Button label="Close" severity="secondary" @click="closeDetail" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()
const toast = useToast()

const FILTERS = [
  { label: 'Open', value: 'open' },
  { label: 'Overdue', value: 'overdue' },
  { label: 'All', value: 'all' },
]
const filter = ref('open')

const invoices = ref([])
const loading = ref(false)

const detailOpen = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
const actionSaving = ref(false)

const visibleInvoices = computed(() => {
  if (filter.value === 'all') return invoices.value
  if (filter.value === 'overdue') return invoices.value.filter(isOverdue)
  // 2026-05-12 audit — match the OUTSTANDING KPI's basis: exclude drafts
  // from the Open tab. Drafts aren't receivables; if the operator sums the
  // visible list it should equal the KPI total. Drafts still surface in
  // "All".
  return invoices.value.filter((inv) => {
    const s = String(inv.status || '').toLowerCase()
    return !['paid', 'draft', 'void', 'canceled'].includes(s)
  })
})

const emptyTitle = computed(() => {
  if (filter.value === 'overdue') return 'Nothing overdue'
  if (filter.value === 'all') return 'No invoices yet'
  return 'No open invoices'
})

// Money KPIs — server-side aggregator preferred (S113 D-S111-billing-summary),
// falls back to client-side over the visible invoice list when the endpoint
// is unavailable. Same shape as desktop BillingView.
const billingSummary = ref(null)

async function loadBillingSummary() {
  try {
    billingSummary.value = await api.get('/api/invoices/summary')
  } catch (_) {
    billingSummary.value = null
  }
}

const totalOutstanding = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.total_outstanding === 'number') {
    return billingSummary.value.total_outstanding
  }
  return invoices.value
    .filter((inv) => {
      const s = String(inv.status || '').toLowerCase()
      return s !== 'paid' && s !== 'draft' && s !== 'void' && s !== 'canceled'
    })
    .reduce((sum, inv) => sum + Number(inv.balance_due ?? inv.total ?? 0), 0)
})

const overdueAmount = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.overdue === 'number') {
    return billingSummary.value.overdue
  }
  return invoices.value
    .filter(isOverdue)
    .reduce((sum, inv) => sum + Number(inv.balance_due ?? inv.total ?? 0), 0)
})

const paidThisMonth = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.paid_this_month === 'number') {
    return billingSummary.value.paid_this_month
  }
  const now = new Date()
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10)
  return invoices.value
    .filter((inv) => {
      const s = String(inv.status || '').toLowerCase()
      const paidAt = String(inv.paid_at || inv.updated_at || '')
      return s === 'paid' && paidAt >= monthStart
    })
    .reduce((sum, inv) => sum + Number(inv.total || 0), 0)
})

const canSend = computed(() => {
  const s = String(detail.value?.status || '').toLowerCase()
  return s === 'draft'
})

const canMarkPaid = computed(() => {
  const s = String(detail.value?.status || '').toLowerCase()
  return !['paid', 'void', 'canceled'].includes(s)
})

function fmtMoney(n) {
  return Number(n || 0).toFixed(2)
}

function fmtDate(d) {
  if (!d) return ''
  try {
    return new Date(d).toLocaleDateString()
  } catch {
    return d
  }
}

function prettyStatus(s) {
  if (!s) return '—'
  return String(s).charAt(0).toUpperCase() + String(s).slice(1)
}

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (['paid'].includes(k)) return 'success'
  if (['sent'].includes(k)) return 'info'
  if (['draft', 'pending'].includes(k)) return 'warning'
  if (['void', 'canceled', 'overdue'].includes(k)) return 'danger'
  return 'secondary'
}

function isOverdue(inv) {
  if (!inv?.due_date) return false
  const s = String(inv.status || '').toLowerCase()
  if (['paid', 'void', 'canceled', 'draft'].includes(s)) return false
  return new Date(inv.due_date) < new Date()
}

async function fetchInvoices() {
  loading.value = true
  try {
    const r = await api.get('/api/invoices')
    invoices.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Load failed', detail: err.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

async function openDetail(inv) {
  detail.value = null
  detailOpen.value = true
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/invoices/${inv.id}`)
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Load failed', detail: err.message, life: 4000 })
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detail.value = null
}

async function sendInvoice() {
  if (!detail.value) return
  actionSaving.value = true
  try {
    await api.post(`/api/invoices/${detail.value.id}/send`, {})
    await api.patch(`/api/invoices/${detail.value.id}`, { status: 'Sent' }).catch(() => {})
    toast.add({ severity: 'success', summary: 'Invoice sent', life: 2500 })
    detail.value = await api.get(`/api/invoices/${detail.value.id}`)
    await fetchInvoices()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Send failed', detail: err.message, life: 4000 })
  } finally {
    actionSaving.value = false
  }
}

async function markPaid() {
  if (!detail.value) return
  if (!(await confirmAsync({ header: 'Confirm', message: 'Mark this invoice as paid?' }))) return
  actionSaving.value = true
  try {
    await api.patch(`/api/invoices/${detail.value.id}`, { status: 'Paid' })
    toast.add({ severity: 'success', summary: 'Marked paid', life: 2500 })
    detail.value = await api.get(`/api/invoices/${detail.value.id}`)
    await fetchInvoices()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Update failed', detail: err.message, life: 4000 })
  } finally {
    actionSaving.value = false
  }
}

onMounted(() => {
  loadBillingSummary()
  fetchInvoices()
})
</script>

<style scoped>
.mobile-billing {
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

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.kpi-strip {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 0.4rem;
}
.kpi-strip .kpi {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 0.55rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  background: var(--p-content-background, #fff);
  min-width: 0;
}
.kpi-strip .kpi-label {
  font-size: 0.7rem;
  color: var(--p-text-muted-color, #6b7280);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}
.kpi-strip .kpi-val {
  font-size: 0.95rem;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}
.kpi-strip .kpi.alert .kpi-val {
  color: var(--p-red-500, #ef4444);
}

.filter-switch :deep(.p-selectbutton) {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  width: 100%;
}

.filter-switch :deep(.p-selectbutton .p-button) {
  padding-block: 0.5rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.inv-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.inv-card.overdue {
  border-left: 3px solid #dc2626;
}

.inv-row,
.inv-row-2 {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.inv-customer {
  font-weight: 700;
  font-size: 1rem;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inv-total {
  font-weight: 700;
  font-size: 1rem;
  color: var(--p-primary-color, #2563eb);
}

.inv-number {
  font-family: monospace;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}

.inv-due {
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.inv-due.overdue {
  color: #dc2626;
}

.overdue-flag {
  background: #dc2626;
  color: #fff;
  font-size: 0.7rem;
  font-weight: 700;
  padding: 0.05rem 0.4rem;
  border-radius: 999px;
  margin-left: 0.3rem;
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
</style>
