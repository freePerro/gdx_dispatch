<template>
    <section class="phone-com-faxes view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Phone.com — Faxes</h1>
        </template>
        <template #end>
          <Select
            v-model="direction"
            :options="directionOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All directions"
            class="filter-select"
            data-test="pc-faxes-direction"
            @change="onFilterChange"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchFaxes"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else
        :value="faxes"
        :paginator="false"
        stripedRows
        responsiveLayout="scroll"
        :rowClass="() => 'row-clickable'"
        @row-click="(event) => openDetail(event.data)"
        data-testid="phone-com-faxes-table"
      >
        <template #empty>
          <div class="empty-message">No faxes yet.</div>
        </template>

        <Column header="Received" style="width: 180px">
          <template #body="{ data }">{{ formatDateTime(data.received_at) }}</template>
        </Column>
        <Column header="Direction" style="width: 100px">
          <template #body="{ data }">
            <Tag
              :value="data.direction === 'in' ? 'Inbound' : 'Outbound'"
              :severity="data.direction === 'in' ? 'info' : 'success'"
            />
          </template>
        </Column>
        <Column field="from_number" header="From" />
        <Column field="to_number" header="To" />
        <Column header="Pages" style="width: 80px">
          <template #body="{ data }">{{ data.pages || '—' }}</template>
        </Column>
        <Column header="Status" style="width: 130px">
          <template #body="{ data }">
            <Tag :value="prettyStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="" style="width: 100px">
          <template #body="{ data }">
            <Button
              icon="pi pi-file-pdf"
              text
              rounded
              severity="secondary"
              title="Open PDF"
              @click.stop="openPdf(data)"
              data-test="pc-fax-open-pdf"
            />
          </template>
        </Column>
      </DataTable>

      <div v-if="faxes.length >= perPage" class="pagination-row">
        <Button
          label="Prev"
          icon="pi pi-chevron-left"
          :disabled="page <= 1"
          severity="secondary"
          @click="page = Math.max(1, page - 1)"
        />
        <span class="text-muted">Page {{ page }}</span>
        <Button
          label="Next"
          icon="pi pi-chevron-right"
          iconPos="right"
          :disabled="faxes.length < perPage"
          severity="secondary"
          @click="page = page + 1"
        />
      </div>
    </section>

    <Dialog
      v-model:visible="detailVisible"
      modal
      header="Fax detail"
      :style="{ width: '720px' }"
      @hide="closeDetail"
    >
      <div v-if="detail" class="detail-grid">
        <div class="detail-row"><span class="label">Received</span><span>{{ formatDateTime(detail.received_at) }}</span></div>
        <div class="detail-row"><span class="label">Direction</span>
          <Tag :value="detail.direction === 'in' ? 'Inbound' : 'Outbound'" :severity="detail.direction === 'in' ? 'info' : 'success'" />
        </div>
        <div class="detail-row"><span class="label">From</span><span>{{ detail.from_number }}</span></div>
        <div class="detail-row"><span class="label">To</span><span>{{ detail.to_number }}</span></div>
        <div class="detail-row"><span class="label">Pages</span><span>{{ detail.pages || '—' }}</span></div>
        <div class="detail-row"><span class="label">Status</span>
          <Tag :value="prettyStatus(detail.status)" :severity="statusSeverity(detail.status)" />
        </div>

        <div v-if="pdfBlobUrl" class="pdf-block">
          <iframe :src="pdfBlobUrl" class="pdf-frame" title="Fax PDF" />
        </div>
        <div v-else-if="pdfLoading" class="spinner-wrap">
          <ProgressSpinner />
        </div>
        <div v-if="pdfError" class="error-banner">{{ pdfError }}</div>
      </div>

      <template #footer>
        <Button
          v-if="pdfBlobUrl"
          label="Download"
          icon="pi pi-download"
          severity="secondary"
          @click="downloadPdf"
        />
        <Button label="Close" severity="secondary" @click="closeDetail" />
      </template>
    </Dialog>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useApi } from '../composables/useApi'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()

const faxes = ref([])
const page = ref(1)
const perPage = ref(50)
const direction = ref(null)
const loading = ref(false)
const error = ref(null)

const directionOptions = [
  { label: 'All directions', value: null },
  { label: 'Inbound', value: 'in' },
  { label: 'Outbound', value: 'out' },
]

const detail = ref(null)
const detailVisible = ref(false)
const pdfBlobUrl = ref(null)
const pdfLoading = ref(false)
const pdfError = ref(null)

function formatDateTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

function prettyStatus(s) {
  if (!s) return '—'
  return s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, ' ')
}

function statusSeverity(s) {
  if (!s) return 'secondary'
  const k = String(s).toLowerCase()
  if (['received', 'sent', 'completed', 'success'].includes(k)) return 'success'
  if (['queued', 'sending', 'pending'].includes(k)) return 'warning'
  if (['failed', 'error'].includes(k)) return 'danger'
  return 'secondary'
}

function _authHeaders() {
  const tok = sessionStorage.getItem('gdx_access_token')
    || localStorage.getItem('gdx_access_token')
    || localStorage.getItem('auth_token')
    || ''
  return tok ? { Authorization: `Bearer ${tok}` } : {}
}

const fetchFaxes = async () => {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams()
    params.set('limit', perPage.value)
    params.set('offset', (page.value - 1) * perPage.value)
    if (direction.value) params.set('direction', direction.value)
    const r = await api.get(`/api/phone-com/faxes?${params.toString()}`)
    faxes.value = r.items || []
  } catch (err) {
    error.value = err.message || 'Failed to load faxes'
  } finally {
    loading.value = false
  }
}

const onFilterChange = () => {
  page.value = 1
  fetchFaxes()
}

const _revoke = (url) => {
  if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
}

const fetchPdfBlob = async (faxId) => {
  const r = await fetch(`/api/phone-com/faxes/${faxId}/pdf`, { headers: _authHeaders() })
  if (!r.ok) throw new Error(`PDF fetch ${r.status}`)
  return URL.createObjectURL(await r.blob())
}

const openDetail = async (fax) => {
  detail.value = fax
  detailVisible.value = true
  _revoke(pdfBlobUrl.value)
  pdfBlobUrl.value = null
  pdfError.value = null
  pdfLoading.value = true
  try {
    pdfBlobUrl.value = await fetchPdfBlob(fax.id)
  } catch (err) {
    pdfError.value = err.message || 'Failed to load PDF'
  } finally {
    pdfLoading.value = false
  }
}

const openPdf = (fax) => openDetail(fax)

const downloadPdf = () => {
  if (!pdfBlobUrl.value || !detail.value) return
  const a = document.createElement('a')
  a.href = pdfBlobUrl.value
  a.download = `fax-${detail.value.id}.pdf`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

const closeDetail = () => {
  detailVisible.value = false
  _revoke(pdfBlobUrl.value)
  pdfBlobUrl.value = null
  pdfError.value = null
  detail.value = null
}

watch(page, fetchFaxes)
onMounted(fetchFaxes)
</script>

<style scoped>
.phone-com-faxes {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.filter-select {
  min-width: 11rem;
}
.row-clickable {
  cursor: pointer;
}
.text-muted {
  color: var(--p-text-muted-color);
}
.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem;
}
.empty-message {
  text-align: center;
  padding: 1.5rem;
  color: var(--p-text-muted-color);
}
.pagination-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  justify-content: center;
}
.detail-grid {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.detail-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.detail-row .label {
  width: 90px;
  color: var(--p-text-muted-color);
  font-weight: 500;
}
.pdf-block {
  margin-top: 0.75rem;
}
.pdf-frame {
  width: 100%;
  height: 60vh;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
</style>
