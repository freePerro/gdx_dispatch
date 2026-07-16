<template>
  <section class="vendor-bill-detail-view view-card">
    <Toolbar>
      <template #start>
        <Button
          v-tooltip="'Back'"
          aria-label="Back"
          icon="pi pi-arrow-left"
          severity="secondary"
          text
          @click="$router.push('/vendor-bills')"
        />
        <h1 class="view-heading">
          {{ invoice?.vendor_name_raw || 'Vendor Bill' }}
          <span v-if="invoice?.invoice_number" class="muted">· #{{ invoice.invoice_number }}</span>
        </h1>
      </template>
      <template #end>
        <Tag v-if="invoice" :value="invoice.status" :severity="statusSeverity(invoice.status)" />
        <Button
          v-if="invoice && invoice.status !== 'paid'"
          label="Mark paid"
          icon="pi pi-check"
          size="small"
          severity="secondary"
          :disabled="busy"
          @click="setStatus('paid')"
        />
      </template>
    </Toolbar>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <template v-else-if="invoice">
      <div v-if="!invariantOk" class="warn-banner" data-testid="invariant-banner">
        ⚠ The extracted lines don't add up to the printed total — review carefully before confirming.
      </div>
      <div v-if="invoice.possible_duplicate_of_id" class="warn-banner" data-testid="dup-banner">
        ⚠ Possible duplicate of another bill from this vendor with the same total. Confirm this isn't a double-bill.
      </div>

      <div class="split">
        <!-- Left: the PDF -->
        <div class="pdf-pane">
          <iframe
            v-if="pdfUrl"
            :src="pdfUrl"
            class="pdf-frame"
            title="Vendor bill PDF"
            data-testid="pdf-frame"
          />
          <div v-else class="pdf-missing">No PDF attached.</div>
        </div>

        <!-- Right: header + lines -->
        <div class="review-pane">
          <div class="summary-grid">
            <div class="summary-tile">
              <div class="tile-label">Total</div>
              <div class="tile-value">{{ formatCurrency(invoice.total) }}</div>
            </div>
            <div class="summary-tile">
              <div class="tile-label">Due</div>
              <div class="tile-value">{{ formatDate(invoice.due_date) || '—' }}</div>
            </div>
            <div class="summary-tile">
              <div class="tile-label">Terms</div>
              <div class="tile-value">{{ invoice.terms || '—' }}</div>
            </div>
          </div>

          <!-- Header job match -->
          <div class="match-block">
            <div class="block-label">Job</div>
            <div class="match-controls">
              <Tag v-if="matchedJobLabel" :value="matchedJobLabel" severity="info" data-testid="matched-job" />
              <!-- Always available so a bill with no auto-match can still be
                   routed to its job (the suggestion chips below are quick-picks). -->
              <Select
                v-model="jobPick"
                :options="jobOptions"
                optionLabel="label"
                optionValue="value"
                filter
                showClear
                placeholder="Pick a job…"
                class="job-select"
                data-testid="job-select"
                :disabled="busy"
                @change="onJobPick"
              />
            </div>
            <div v-if="invoice.suggestions?.length" class="suggestions">
              <span class="muted small">Suggested:</span>
              <Button
                v-for="s in invoice.suggestions"
                :key="s.job_id"
                :label="`${s.customer_name || s.job_title || 'Job'} · ${Math.round((s.score || 0) * 100)}%`"
                size="small"
                severity="secondary"
                outlined
                class="suggestion-chip"
                :disabled="busy"
                @click="setMatch(s.job_id)"
              />
            </div>
          </div>

          <!-- Lines -->
          <DataTable :value="invoice.lines" stripedRows responsiveLayout="scroll" data-testid="lines-table">
            <template #empty><div class="empty-message">No line items.</div></template>

            <Column header="Description" style="min-width: 220px">
              <template #body="{ data }">
                <div class="desc">{{ data.description }}</div>
                <Tag v-if="data.kind !== 'item'" :value="data.kind" severity="secondary" class="kind-chip" />
              </template>
            </Column>
            <Column header="Qty" style="width: 70px; text-align: right">
              <template #body="{ data }">{{ data.quantity }}</template>
            </Column>
            <Column header="Cost" style="width: 110px; text-align: right">
              <template #body="{ data }">{{ formatCurrency(data.line_total) }}</template>
            </Column>
            <Column header="Route to" style="width: 260px">
              <template #body="{ data }">
                <Tag
                  v-if="data.status === 'confirmed'"
                  :value="confirmedLabel(data)"
                  severity="success"
                  data-testid="confirmed-tag"
                />
                <SelectButton
                  v-else
                  v-model="draft[data.id].disposition"
                  :options="dispositionOptionsFor(data)"
                  optionLabel="label"
                  optionValue="value"
                  :allowEmpty="false"
                  class="disp-select"
                  :disabled="busy"
                />
              </template>
            </Column>
            <Column header="" style="min-width: 220px">
              <template #body="{ data }">
                <template v-if="data.status !== 'confirmed'">
                  <!-- per-disposition target control -->
                  <Select
                    v-if="draft[data.id].disposition === 'stock'"
                    v-model="draft[data.id].inventory_item_id"
                    :options="inventoryItems"
                    optionLabel="label"
                    optionValue="value"
                    filter
                    showClear
                    placeholder="Inventory item…"
                    class="target-input"
                    :disabled="busy"
                  />
                  <InputText
                    v-else-if="draft[data.id].disposition === 'skip'"
                    v-model="draft[data.id].skip_reason"
                    placeholder="Reason (required)…"
                    class="target-input"
                    :disabled="busy"
                  />
                  <span v-else-if="draft[data.id].disposition === 'job' && !invoice.matched_job_id" class="muted small">
                    Pick a job above first
                  </span>
                  <Button
                    label="Confirm"
                    size="small"
                    icon="pi pi-check"
                    class="confirm-btn"
                    :disabled="busy || !canConfirm(data)"
                    @click="confirmLine(data)"
                  />
                </template>
              </template>
            </Column>
          </DataTable>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { useApi } from '../composables/useApi'
import { createAuthedBlobUrl } from '../composables/useAuthedFile'
import { formatDate } from '../utils/dates'
import { formatMoney as formatCurrency } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import SelectButton from 'primevue/selectbutton'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const route = useRoute()

const invoice = ref(null)
const loading = ref(false)
const busy = ref(false)
const error = ref(null)
const pdfUrl = ref(null)
const inventoryItems = ref([])
const jobOptions = ref([])
const jobPick = ref(null)
const draft = reactive({})

const ALL_DISPOSITIONS = [
  { label: 'Job', value: 'job' },
  { label: 'Stock', value: 'stock' },
  { label: 'Overhead', value: 'overhead' },
  { label: 'Skip', value: 'skip' },
]

// Freight/tax are costs, never stockable — only item lines can go to stock.
function dispositionOptionsFor(line) {
  return line.kind === 'item' ? ALL_DISPOSITIONS : ALL_DISPOSITIONS.filter((o) => o.value !== 'stock')
}

const invariantOk = computed(() => !String(invoice.value?.notes || '').startsWith('INVARIANT_MISMATCH'))

const matchedJobLabel = computed(() => {
  const id = invoice.value?.matched_job_id
  if (!id) return null
  const opt = jobOptions.value.find((o) => o.value === id)
  if (opt) return opt.label
  const s = (invoice.value.suggestions || []).find((x) => x.job_id === id)
  return s ? (s.customer_name || s.job_title || 'Job') : 'Job selected'
})

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (k === 'paid') return 'success'
  if (k === 'void') return 'secondary'
  if (k === 'open') return 'info'
  return 'secondary'
}

function confirmedLabel(line) {
  if (line.disposition === 'job') return 'billed to job'
  if (line.disposition === 'stock') return 'received into stock'
  if (line.disposition === 'overhead') return 'overhead expense'
  if (line.disposition === 'skip') return `skipped: ${line.skip_reason || ''}`
  return line.disposition
}

function canConfirm(line) {
  const d = draft[line.id]
  if (!d) return false
  if (d.disposition === 'job') return !!invoice.value?.matched_job_id
  if (d.disposition === 'stock') return !!d.inventory_item_id
  if (d.disposition === 'skip') return !!(d.skip_reason && d.skip_reason.trim())
  return d.disposition === 'overhead'
}

function seedDrafts() {
  for (const line of invoice.value?.lines || []) {
    if (!draft[line.id]) {
      draft[line.id] = {
        disposition: line.disposition && line.disposition !== 'pending' ? line.disposition : 'job',
        inventory_item_id: line.inventory_item_id || null,
        skip_reason: line.skip_reason || '',
      }
    }
  }
}

const fetchDetail = async () => {
  loading.value = true
  error.value = null
  try {
    invoice.value = await api.get(`/api/vendor-invoices/${route.params.id}`)
    jobPick.value = invoice.value?.matched_job_id || null
    seedDrafts()
    if (invoice.value?.document_id) {
      pdfUrl.value = await createAuthedBlobUrl(`/api/documents/${invoice.value.document_id}/download`)
    }
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

const loadInventory = async () => {
  try {
    const rows = (await api.get('/api/inventory/parts')) || []
    inventoryItems.value = rows.map((r) => ({
      label: `${r.part_name || r.name || 'Item'}${r.sku ? ` (${r.sku})` : ''}`,
      value: r.id,
    }))
  } catch {
    inventoryItems.value = []
  }
}

const loadJobs = async () => {
  try {
    const rows = (await api.get('/api/jobs?page_size=500')) || []
    jobOptions.value = rows.map((j) => ({
      label: `${j.title || 'Job'} (${String(j.id).slice(0, 8)})`,
      value: j.id,
    }))
  } catch {
    jobOptions.value = []
  }
}

function onJobPick() {
  if (jobPick.value) setMatch(jobPick.value)
}

async function setMatch(jobId) {
  busy.value = true
  try {
    invoice.value = await api.patch(`/api/vendor-invoices/${invoice.value.id}`, { matched_job_id: jobId })
    jobPick.value = invoice.value?.matched_job_id || jobId
    seedDrafts()
  } catch (err) {
    error.value = err.message || 'Failed to set job'
  } finally {
    busy.value = false
  }
}

async function setStatus(status) {
  busy.value = true
  try {
    invoice.value = await api.patch(`/api/vendor-invoices/${invoice.value.id}`, { status }, { successMessage: `Marked ${status}` })
    seedDrafts()
  } catch (err) {
    error.value = err.message || 'Failed to update status'
  } finally {
    busy.value = false
  }
}

async function confirmLine(line) {
  const d = draft[line.id]
  busy.value = true
  try {
    const payload = { disposition: d.disposition }
    if (d.disposition === 'stock') payload.inventory_item_id = d.inventory_item_id
    if (d.disposition === 'skip') payload.skip_reason = d.skip_reason
    invoice.value = await api.post(
      `/api/vendor-invoices/${invoice.value.id}/lines/${line.id}/confirm`,
      payload,
      { successMessage: 'Line confirmed' },
    )
    seedDrafts()
  } catch (err) {
    error.value = err.message || 'Failed to confirm line'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  await fetchDetail()
  await Promise.all([loadInventory(), loadJobs()])
})

onBeforeUnmount(() => {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
})

// Exposed for unit tests (load contract + confirm payloads).
defineExpose({ invoice, draft, pdfUrl, jobPick, jobOptions, confirmLine, setMatch, onJobPick, setStatus, canConfirm, dispositionOptionsFor })
</script>

<style scoped>
.vendor-bill-detail-view { display: flex; flex-direction: column; gap: 1rem; }
.view-heading { margin: 0; font-size: 1.25rem; font-weight: 600; }
.muted { color: var(--p-text-muted-color); font-weight: 400; }
.small { font-size: 0.8rem; }
.split { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr); gap: 1rem; }
@media (max-width: 900px) { .split { grid-template-columns: 1fr; } }
.pdf-pane { border: 1px solid var(--p-surface-200, #e2e8f0); border-radius: 8px; overflow: hidden; min-height: 500px; }
.pdf-frame { width: 100%; height: 100%; min-height: 500px; border: 0; }
.pdf-missing { padding: 2rem; text-align: center; color: var(--p-text-muted-color); }
.review-pane { display: flex; flex-direction: column; gap: 1rem; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 0.5rem; }
.summary-tile { background: var(--p-surface-50, #f8fafc); border: 1px solid var(--p-surface-200, #e2e8f0); border-radius: 8px; padding: 0.5rem 0.75rem; }
.tile-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--p-text-muted-color); }
.tile-value { font-size: 1.05rem; font-weight: 600; margin-top: 0.15rem; }
.match-block { display: flex; flex-direction: column; gap: 0.4rem; }
.block-label { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--p-text-muted-color); }
.matched-job { display: flex; align-items: center; gap: 0.5rem; }
.suggestions { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }
.suggestion-chip { font-size: 0.75rem; }
.desc { font-size: 0.85rem; }
.kind-chip { margin-top: 0.2rem; }
.disp-select :deep(.p-button) { padding: 0.2rem 0.45rem; font-size: 0.72rem; }
.target-input { width: 100%; margin-bottom: 0.35rem; }
.confirm-btn { width: 100%; }
.error-banner { background: var(--p-red-50, #fef2f2); color: var(--p-red-700, #b91c1c); border: 1px solid var(--p-red-200, #fecaca); border-radius: 6px; padding: 0.5rem 0.75rem; }
.warn-banner { background: var(--p-yellow-50, #fffbeb); color: var(--p-yellow-800, #92400e); border: 1px solid var(--p-yellow-200, #fde68a); border-radius: 6px; padding: 0.5rem 0.75rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color); }
</style>
