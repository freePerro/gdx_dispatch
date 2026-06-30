<template>
    <section class="phone-com-cold-leads view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Phone.com — Cold Leads</h1>
        </template>
        <template #end>
          <Select
            v-model="minDuration"
            :options="durationOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="Min duration"
            class="filter-select"
            data-test="pc-cl-min-duration"
            @change="fetchColdLeads"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchColdLeads"
          />
        </template>
      </Toolbar>

      <p class="view-description text-muted">
        Inbound callers who aren't yet in your customer book, grouped by phone number.
        Quick-create a customer to start matching their future calls.
      </p>

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
        data-testid="cold-leads-datatable"
      >
        <template #empty>
          <div class="empty-message">No cold leads — every caller is already a customer.</div>
        </template>

        <Column header="Caller">
          <template #body="{ data }">{{ callerLabel(data) }}</template>
        </Column>
        <Column field="call_count" header="Calls" style="width: 90px" />
        <Column header="Last call" style="width: 200px">
          <template #body="{ data }">{{ formatDateTime(data.last_call_at) }}</template>
        </Column>
        <Column header="Last status" style="width: 130px">
          <template #body="{ data }">
            <Tag v-if="data.last_status" :value="prettyStatus(data.last_status)" :severity="statusSeverity(data.last_status)" />
            <span v-else class="text-muted">—</span>
          </template>
        </Column>
        <Column header="Voicemail snippet">
          <template #body="{ data }">
            <span v-if="data.voicemail_snippet" class="snippet">{{ data.voicemail_snippet }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </Column>
        <Column header="" style="width: 220px">
          <template #body="{ data }">
            <Button
              label="Create customer"
              icon="pi pi-user-plus"
              size="small"
              :as="'a'"
              :href="`/customers/new?phone=${encodeURIComponent(data.from_number)}`"
              data-test="pc-cl-create"
            />
            <a
              v-if="data.from_number"
              v-tooltip="'Call back'"
              :href="`tel:${data.from_number}`"
              class="action-icon"
              aria-label="Call back"
            >📞</a>
          </template>
        </Column>
      </DataTable>

      <div v-if="total > perPage" class="pagination-row">
        <Button label="Prev" icon="pi pi-chevron-left" :disabled="page <= 1" severity="secondary" @click="page = Math.max(1, page - 1)" />
        <span class="text-muted">Page {{ page }} · {{ total }} total</span>
        <Button label="Next" icon="pi pi-chevron-right" iconPos="right" :disabled="page * perPage >= total" severity="secondary" @click="page = page + 1" />
      </div>
    </section>
</template>

<script setup>
import { ref, onMounted, watch } from 'vue'
import { useApi } from '../composables/useApi'
import { isCnamJunk } from '../utils/phoneComLabels'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()

const items = ref([])
const total = ref(0)
const page = ref(1)
const perPage = ref(50)
const minDuration = ref(10)
const loading = ref(false)
const error = ref(null)

const durationOptions = [
  { label: 'Any duration', value: 0 },
  { label: '10s+', value: 10 },
  { label: '30s+', value: 30 },
  { label: '1m+', value: 60 },
]

function formatDateTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

function callerLabel(item) {
  const cnam = item.caller_cnam
  if (cnam && !isCnamJunk(cnam, item.from_number)) return `${cnam} · ${item.from_number}`
  return item.from_number
}

function prettyStatus(s) {
  if (!s) return ''
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function statusSeverity(s) {
  if (!s) return 'secondary'
  const k = s.toLowerCase()
  if (k === 'voicemail') return 'warning'
  if (k === 'forwarded') return 'info'
  if (k === 'answered' || k === 'completed') return 'success'
  if (k === 'missed' || k === 'canceled') return 'danger'
  return 'secondary'
}

const fetchColdLeads = async () => {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams()
    params.set('page', page.value)
    params.set('per_page', perPage.value)
    params.set('min_duration_s', minDuration.value)
    const r = await api.get(`/api/phone-com/cold-leads?${params.toString()}`)
    items.value = r.items || []
    total.value = r.total || 0
  } catch (err) {
    error.value = err.message || 'Failed to load cold leads'
  } finally {
    loading.value = false
  }
}

watch(page, fetchColdLeads)
onMounted(fetchColdLeads)
</script>

<style scoped>
.phone-com-cold-leads {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.view-description {
  margin: 0;
  font-size: 0.9rem;
}
.filter-select {
  min-width: 11rem;
}
.action-icon {
  margin-left: 0.5rem;
  text-decoration: none;
  color: inherit;
  font-size: 1.1rem;
}
.snippet {
  font-style: italic;
  display: block;
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
</style>
