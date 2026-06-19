<!--
  Canonical GDX view template — list with detail Dialog.

  Reference: gdx/docs/frontend_view_pattern.md
  Live examples: CustomersView.vue, JobsView.vue, BillingView.vue,
                 PhoneComCallsView.vue.

  This file is NOT routed — leading underscore keeps it out of file-glob
  imports and makes its purpose obvious. Copy it when starting a new view,
  rename, wire to /api endpoint, drop sections you don't need.

  What this template demonstrates:
  - AppLayout wrapper (mandatory — gives sidebar + topbar + breadcrumb)
  - Toolbar with #start (heading) + #end (filters/actions)
  - DataTable + Column with severity-tagged status
  - Click-to-open detail Dialog
  - Loading + error + empty states using design tokens
  - Pagination row (use DataTable's :paginator="true" for simpler cases)
  - PrimeVue imports laid out in the same order as CustomersView
-->
<template>
    <section class="my-feature-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">My Feature</h1>
        </template>
        <template #end>
          <InputText
            v-model="searchQuery"
            placeholder="Search…"
            class="search-input"
            @input="onFilterChange"
          />
          <Select
            v-model="statusFilter"
            :options="statusOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All statuses"
            class="filter-select"
            @change="onFilterChange"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchItems"
          />
          <Button
            label="+ New"
            icon="pi pi-plus"
            @click="openCreate"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else
        :value="items"
        :paginator="false"
        stripedRows
        responsiveLayout="scroll"
        :rowClass="() => 'row-clickable'"
        @row-click="(event) => openDetail(event.data)"
      >
        <template #empty>
          <div class="empty-message">No items yet.</div>
        </template>

        <Column header="Created" style="width: 180px">
          <template #body="{ data }">{{ formatDateTime(data.created_at) }}</template>
        </Column>
        <Column field="name" header="Name" sortable />
        <Column header="Status" style="width: 130px">
          <template #body="{ data }">
            <Tag :value="prettyStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="" style="width: 100px">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil"
              text
              rounded
              severity="secondary"
              @click.stop="openDetail(data)"
            />
          </template>
        </Column>
      </DataTable>

      <div v-if="total > perPage" class="pagination-row">
        <Button
          label="Prev"
          icon="pi pi-chevron-left"
          :disabled="page <= 1"
          severity="secondary"
          @click="page = Math.max(1, page - 1)"
        />
        <span class="text-muted">Page {{ page }} · {{ total }} total</span>
        <Button
          label="Next"
          icon="pi pi-chevron-right"
          iconPos="right"
          :disabled="page * perPage >= total"
          severity="secondary"
          @click="page = page + 1"
        />
      </div>
    </section>

    <Dialog
      v-model:visible="detailVisible"
      modal
      header="Item detail"
      :style="{ width: '640px' }"
      @hide="closeDetail"
    >
      <div v-if="detailLoading" class="spinner-wrap">
        <ProgressSpinner />
      </div>
      <div v-else-if="detail" class="detail-grid">
        <div class="detail-row">
          <span class="label">Created</span>
          <span>{{ formatDateTime(detail.created_at) }}</span>
        </div>
        <div class="detail-row">
          <span class="label">Name</span>
          <span>{{ detail.name }}</span>
        </div>
        <div class="detail-row">
          <span class="label">Status</span>
          <Tag :value="prettyStatus(detail.status)" :severity="statusSeverity(detail.status)" />
        </div>
      </div>
      <template #footer>
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
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()

// ── List state ────────────────────────────────────────────────────────
const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = ref(50)
const loading = ref(false)
const error = ref(null)

const searchQuery = ref('')
const statusFilter = ref(null)
const statusOptions = [
  { label: 'All statuses', value: null },
  { label: 'Active', value: 'active' },
  { label: 'Pending', value: 'pending' },
  { label: 'Closed', value: 'closed' },
]

// ── Detail Dialog state ───────────────────────────────────────────────
const detail = ref(null)
const detailVisible = ref(false)
const detailLoading = ref(false)

// ── Helpers (canonical patterns) ──────────────────────────────────────

function formatDateTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

function prettyStatus(s) {
  if (!s) return ''
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// Map raw status to PrimeVue Tag severity. Keep this consistent across
// views per gdx/docs/frontend_view_pattern.md.
function statusSeverity(s) {
  if (!s) return 'secondary'
  const k = String(s).toLowerCase()
  if (['active', 'paid', 'completed', 'success'].includes(k)) return 'success'
  if (['pending', 'queued', 'voicemail'].includes(k)) return 'warning'
  if (['scheduled', 'forwarded', 'info'].includes(k)) return 'info'
  if (['canceled', 'failed', 'missed', 'overdue'].includes(k)) return 'danger'
  return 'secondary'
}

// ── Fetch ─────────────────────────────────────────────────────────────

const fetchItems = async () => {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams()
    params.set('page', page.value)
    params.set('per_page', perPage.value)
    if (searchQuery.value) params.set('q', searchQuery.value)
    if (statusFilter.value) params.set('status', statusFilter.value)
    const r = await api.get(`/api/my-feature?${params.toString()}`)
    items.value = r.items || []
    total.value = r.total || 0
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

const onFilterChange = () => {
  page.value = 1
  fetchItems()
}

// ── Detail open / close ───────────────────────────────────────────────

const openDetail = async (item) => {
  detail.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/my-feature/${item.id}`)
  } catch (err) {
    error.value = err.message || 'Failed to load detail'
  } finally {
    detailLoading.value = false
  }
}

const closeDetail = () => {
  detailVisible.value = false
  detail.value = null
}

const openCreate = () => {
  // Replace with router push or a separate Dialog for creation.
  // Kept stub for the template.
}

watch(page, fetchItems)
onMounted(fetchItems)
</script>

<style scoped>
/* All view styles use --p-* design tokens. Hex literals → Doug's visual
   drift complaint. Keep this scoped block tight — most layout is provided
   by AppLayout + view-card + Toolbar + DataTable. */

.my-feature-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}

.search-input {
  min-width: 16rem;
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
  width: 100px;
  color: var(--p-text-muted-color);
  font-weight: 500;
}
</style>
