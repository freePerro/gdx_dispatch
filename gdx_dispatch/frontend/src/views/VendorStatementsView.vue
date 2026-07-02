<template>
    <section class="vendor-statements-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Vendor Statements</h1>
        </template>
        <template #end>
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
            chooseLabel="Upload Midwest PDF"
            chooseIcon="pi pi-upload"
            data-testid="vendor-statement-upload"
            @uploader="onUpload"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>
      <div v-if="duplicate" class="warn-banner">
        Duplicate document — already uploaded as
        <span class="mono">{{ duplicate.original_name || duplicate.existing_document_id }}</span>.
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else
        :value="items"
        stripedRows
        responsiveLayout="scroll"
        :rowClass="() => 'row-clickable'"
        data-testid="vendor-statements-table"
        @row-click="(event) => openDetail(event.data)"
      >
        <template #empty>
          <div class="empty-message">No vendor statements yet. Upload a Midwest PDF to begin.</div>
        </template>

        <Column header="Uploaded" style="width: 180px">
          <template #body="{ data }">{{ formatDateTime(data.created_at) }}</template>
        </Column>
        <Column field="vendor_name" header="Vendor" />
        <Column header="Statement Date" style="width: 160px">
          <template #body="{ data }">{{ formatDate(data.statement_date) }}</template>
        </Column>
        <Column header="Lines" style="width: 90px">
          <template #body="{ data }">{{ data.line_count }}</template>
        </Column>
        <Column header="Total" style="width: 140px">
          <template #body="{ data }">{{ formatCurrency(data.raw_total) }}</template>
        </Column>
        <Column header="Status" style="width: 120px">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="" style="width: 80px">
          <template #body="{ data }">
            <Button
              v-tooltip="'View details'"
              aria-label="View details"
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
import { useAuthStore } from '../stores/auth'
import { formatDate } from '../utils/dates'
import { formatDateTime, formatMoney as formatCurrency } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import FileUpload from 'primevue/fileupload'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const auth = useAuthStore()
const router = useRouter()

const items = ref([])
const loading = ref(false)
const error = ref(null)
const duplicate = ref(null)

function statusSeverity(s) {
  if (!s) return 'secondary'
  const k = String(s).toLowerCase()
  if (k === 'reconciled') return 'success'
  if (k === 'review') return 'warning'
  if (k === 'parsed') return 'info'
  return 'secondary'
}

const fetchItems = async () => {
  loading.value = true
  error.value = null
  try {
    items.value = (await api.get('/api/vendor-statements')) || []
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

function _resolveTenantId() {
  const stored = sessionStorage.getItem('gdx_tenant_slug')
  if (stored) return stored
  const parts = window.location.hostname.split('.')
  const sub = parts.length >= 3 ? parts[0] : null
  return sub && sub !== 'www' ? sub : null
}

const onUpload = async (event) => {
  duplicate.value = null
  error.value = null
  const file = event.files?.[0]
  if (!file) return
  loading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('vendor', 'midwest')

    const headers = {}
    const tenantId = _resolveTenantId()
    if (tenantId) headers['x-tenant-id'] = tenantId
    if (auth.accessToken) headers.Authorization = `Bearer ${auth.accessToken}`

    const resp = await fetch('/api/vendor-statements/upload', {
      method: 'POST',
      headers,
      credentials: 'include',
      body: fd,
    })

    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}))
      duplicate.value = body?.detail || { detail: 'duplicate' }
      await fetchItems()
      return
    }

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : `upload failed (${resp.status})`)
    }

    const stmt = await resp.json()
    await fetchItems()
    if (stmt?.id) router.push(`/vendor-statements/${stmt.id}`)
  } catch (err) {
    error.value = err.message || 'Upload failed'
  } finally {
    loading.value = false
  }
}

const openDetail = (row) => {
  router.push(`/vendor-statements/${row.id}`)
}

onMounted(fetchItems)
</script>

<style scoped>
.vendor-statements-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.row-clickable { cursor: pointer; }
.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.warn-banner {
  background: var(--p-yellow-50, #fffbeb);
  color: var(--p-yellow-800, #92400e);
  border: 1px solid var(--p-yellow-200, #fde68a);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.mono { font-family: var(--font-mono, ui-monospace, monospace); }
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message {
  text-align: center;
  padding: 1.5rem;
  color: var(--p-text-muted-color);
}
</style>
