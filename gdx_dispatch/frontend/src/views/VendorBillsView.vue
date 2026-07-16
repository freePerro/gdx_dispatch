<template>
  <section class="vendor-bills-view view-card">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Vendor Bills</h1>
      </template>
      <template #end>
        <SelectButton
          v-model="statusFilter"
          :options="filterOptions"
          optionLabel="label"
          optionValue="value"
          :allowEmpty="false"
          class="status-filter"
          @update:modelValue="fetchItems"
        />
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          :disabled="loading"
          @click="fetchItems"
        />
        <FileUpload
          mode="basic"
          name="file"
          accept="application/pdf"
          :auto="true"
          :customUpload="true"
          chooseLabel="Upload bill PDF"
          chooseIcon="pi pi-upload"
          data-testid="vendor-bill-upload"
          @uploader="onUpload"
        />
      </template>
    </Toolbar>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="notice" class="warn-banner">{{ notice }}</div>

    <div v-if="loading" class="spinner-wrap">
      <ProgressSpinner />
    </div>

    <DataTable
      v-else
      :value="items"
      stripedRows
      responsiveLayout="scroll"
      :rowClass="() => 'row-clickable'"
      data-testid="vendor-bills-table"
      @row-click="(event) => openDetail(event.data)"
    >
      <template #empty>
        <div class="empty-message">No vendor bills yet. Upload a supplier invoice PDF to begin.</div>
      </template>

      <Column header="Uploaded" style="width: 170px">
        <template #body="{ data }">{{ formatDateTime(data.created_at) }}</template>
      </Column>
      <Column field="vendor_name_raw" header="Vendor" />
      <Column field="invoice_number" header="Invoice #" style="width: 130px" />
      <Column header="PO / Job ref" style="min-width: 140px">
        <template #body="{ data }">{{ data.po_reference || '—' }}</template>
      </Column>
      <Column header="Due" style="width: 120px">
        <template #body="{ data }">{{ formatDate(data.due_date) }}</template>
      </Column>
      <Column header="Total" style="width: 130px; text-align: right">
        <template #body="{ data }">{{ formatCurrency(data.total) }}</template>
      </Column>
      <Column header="Status" style="width: 210px">
        <template #body="{ data }">
          <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          <Tag
            v-if="needsReview(data)"
            value="needs review"
            severity="warning"
            class="chip"
            data-testid="needs-review-chip"
          />
          <Tag
            v-if="mathMismatch(data)"
            value="check math"
            severity="danger"
            class="chip"
            data-testid="math-chip"
          />
          <Tag
            v-if="data.possible_duplicate_of_id"
            value="possible dup"
            severity="danger"
            class="chip"
            data-testid="possible-dup-chip"
          />
        </template>
      </Column>
      <Column header="" style="width: 70px">
        <template #body="{ data }">
          <Button
            v-tooltip="'Review'"
            aria-label="Review"
            icon="pi pi-arrow-right"
            text
            rounded
            severity="secondary"
            @click.stop="openDetail(data)"
          />
        </template>
      </Column>
    </DataTable>
  </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { formatDate } from '../utils/dates'
import { formatDateTime, formatMoney as formatCurrency } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import SelectButton from 'primevue/selectbutton'
import FileUpload from 'primevue/fileupload'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const router = useRouter()

const items = ref([])
const loading = ref(false)
const error = ref(null)
const notice = ref(null)
const statusFilter = ref('all')

const filterOptions = [
  { label: 'All', value: 'all' },
  { label: 'Needs review', value: 'needs_review' },
  { label: 'Open', value: 'open' },
  { label: 'Paid', value: 'paid' },
]

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (k === 'paid') return 'success'
  if (k === 'void') return 'secondary'
  if (k === 'open') return 'info'
  return 'secondary'
}

function needsReview(row) {
  // Aligns with the "Needs review" filter (backend: reviewed_at IS NULL).
  return !row.reviewed_at
}

function mathMismatch(row) {
  // Separate signal: the parser's arithmetic invariant didn't hold. Distinct
  // from "needs review" so a reviewed bill can still flag a math problem.
  return String(row.notes || '').startsWith('INVARIANT_MISMATCH')
}

const fetchItems = async () => {
  loading.value = true
  error.value = null
  try {
    let url = '/api/vendor-invoices'
    if (statusFilter.value === 'needs_review') url += '?needs_review=true'
    else if (statusFilter.value && statusFilter.value !== 'all') url += `?status=${statusFilter.value}`
    items.value = (await api.get(url)) || []
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

const onUpload = async (event) => {
  notice.value = null
  error.value = null
  const file = event.files?.[0]
  if (!file) return
  loading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('vendor', 'midwest')
    // The backend returns 200 with { created, duplicate_reason, invoice } — a
    // duplicate is not an error, it just returns the existing record.
    const result = await api.post('/api/vendor-invoices/upload', fd)
    await fetchItems()
    if (result && result.created === false) {
      // Duplicate (or already-imported): stay on the queue so the notice is
      // actually seen, rather than navigating away.
      notice.value = result.duplicate_reason
        ? `Already imported (${result.duplicate_reason.replace('_', ' ')}).`
        : 'Already imported.'
      return
    }
    if (result?.invoice?.id) router.push(`/vendor-bills/${result.invoice.id}`)
  } catch (err) {
    error.value = err.message || 'Upload failed'
  } finally {
    loading.value = false
  }
}

const openDetail = (row) => {
  router.push(`/vendor-bills/${row.id}`)
}

onMounted(fetchItems)

// Exposed for unit tests (data-loading + upload contract).
defineExpose({ items, statusFilter, notice, fetchItems, onUpload, needsReview, mathMismatch })
</script>

<style scoped>
.vendor-bills-view { display: flex; flex-direction: column; gap: 1rem; }
.view-heading { margin: 0; font-size: 1.25rem; font-weight: 600; }
.row-clickable { cursor: pointer; }
.chip { margin-left: 0.35rem; }
.status-filter :deep(.p-button) { padding: 0.3rem 0.6rem; font-size: 0.8rem; }
.error-banner {
  background: var(--p-red-50, #fef2f2); color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca); border-radius: 6px; padding: 0.5rem 0.75rem;
}
.warn-banner {
  background: var(--p-yellow-50, #fffbeb); color: var(--p-yellow-800, #92400e);
  border: 1px solid var(--p-yellow-200, #fde68a); border-radius: 6px; padding: 0.5rem 0.75rem;
}
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color); }
</style>
