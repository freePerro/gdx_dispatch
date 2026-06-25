<template>
    <section class="phone-com-calls view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Phone.com — Calls</h1>
        </template>
        <template #end>
          <Select
            v-model="direction"
            :options="directionOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All directions"
            class="filter-select"
            data-test="pc-calls-direction"
            @change="onFilterChange"
          />
          <Select
            v-model="hasVoicemail"
            :options="voicemailOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="Voicemail filter"
            class="filter-select"
            data-test="pc-calls-has-vm"
            @change="onFilterChange"
          />
          <DatePicker
            v-model="dateFrom"
            placeholder="From"
            dateFormat="yy-mm-dd"
            class="filter-select"
            showIcon
            showButtonBar
            data-test="pc-calls-from"
            @update:modelValue="onFilterChange"
          />
          <DatePicker
            v-model="dateTo"
            placeholder="To"
            dateFormat="yy-mm-dd"
            class="filter-select"
            showIcon
            showButtonBar
            data-test="pc-calls-to"
            @update:modelValue="onFilterChange"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchCalls"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
        v-else
        :value="calls"
        :paginator="false"
        stripedRows
        responsiveLayout="scroll"
        @row-click="(event) => openDetail(event.data)"
        :rowClass="() => 'row-clickable'"
        data-testid="phone-com-calls-table"
      >
        <template #empty>
          <div class="empty-message">No calls yet.</div>
        </template>

        <Column header="When" style="width: 180px">
          <template #body="{ data }">{{ formatDateTime(data.started_at) }}</template>
        </Column>
        <Column header="Direction" style="width: 110px">
          <template #body="{ data }">
            <Tag
              :value="prettyDirection(data.direction)"
              :severity="data.direction === 'in' ? 'info' : 'success'"
            />
          </template>
        </Column>
        <Column header="From">
          <template #body="{ data }">{{ callerDisplay(data) }}</template>
        </Column>
        <Column header="To">
          <template #body="{ data }">{{ renderOwnNumber(data.to_number, ownNumbers) }}</template>
        </Column>
        <Column header="Customer">
          <template #body="{ data }">
            <span v-if="data.customer_name">{{ data.customer_name }}</span>
            <Button
              v-else-if="data.from_number"
              label="+ Add"
              link
              size="small"
              :as="'a'"
              :href="`/customers/new?phone=${encodeURIComponent(data.from_number)}`"
              @click.stop
            />
            <span v-else class="text-muted">—</span>
          </template>
        </Column>
        <Column header="Duration" style="width: 100px">
          <template #body="{ data }">{{ formatDuration(data.duration_s) }}</template>
        </Column>
        <Column header="Status" style="width: 130px">
          <template #body="{ data }">
            <Tag :value="friendlyStatus(data)" :severity="statusSeverity(data)" />
          </template>
        </Column>
        <Column header="" style="width: 150px">
          <template #body="{ data }">
            <Button
              v-if="customerNumber(data)"
              icon="pi pi-phone"
              text
              rounded
              severity="success"
              size="small"
              :title="`Call ${customerNumber(data)} — rings your extension first`"
              @click.stop="originateCall(data)"
              data-test="pc-click-to-call"
            />
            <i v-if="data.has_recording" class="pi pi-microphone action-icon" title="has recording" />
            <i v-if="data.has_voicemail" class="pi pi-envelope action-icon" title="has voicemail" />
            <Button
              v-if="data.direction === 'in' && data.from_number"
              icon="pi pi-ban"
              text
              rounded
              severity="danger"
              size="small"
              :title="`Block ${data.from_number}`"
              @click.stop="blockCallerNumber(data)"
              data-test="pc-block-caller"
            />
          </template>
        </Column>
      </DataTable>

      <div v-if="total > perPage" class="pagination-row">
        <Button label="Prev" icon="pi pi-chevron-left" :disabled="page <= 1" severity="secondary" @click="page = Math.max(1, page - 1)" />
        <span class="text-muted">Page {{ page }} · {{ total }} total</span>
        <Button label="Next" icon="pi pi-chevron-right" iconPos="right" :disabled="page * perPage >= total" severity="secondary" @click="page = page + 1" />
      </div>
    </section>

    <Dialog
      v-model:visible="detailVisible"
      modal
      :header="'Call detail'"
      :style="{ width: '640px' }"
      @hide="closeDetail"
    >
      <div v-if="detailLoading" class="spinner-wrap">
        <ProgressSpinner />
      </div>
      <div v-else-if="detail" class="detail-grid">
        <div class="detail-row"><span class="label">When</span><span>{{ formatDateTime(detail.started_at) }}</span></div>
        <div class="detail-row"><span class="label">Direction</span>
          <Tag :value="prettyDirection(detail.direction)" :severity="detail.direction === 'in' ? 'info' : 'success'" />
        </div>
        <div class="detail-row"><span class="label">From</span><span>{{ detail.from_number }}</span></div>
        <div class="detail-row"><span class="label">To</span><span>{{ renderOwnNumber(detail.to_number, ownNumbers) }}</span></div>
        <div class="detail-row"><span class="label">Duration</span><span>{{ formatDuration(detail.duration_s) }}</span></div>
        <div class="detail-row"><span class="label">Status</span>
          <Tag :value="friendlyStatus(detail)" :severity="statusSeverity(detail)" />
        </div>
        <div v-if="detail.customer_name" class="detail-row"><span class="label">Customer</span><span>{{ detail.customer_name }}</span></div>
        <div v-if="detail.job_title" class="detail-row">
          <span class="label">Job</span>
          <span>
            {{ detail.job_title }}
            <Button label="Unlink" link severity="danger" size="small" @click="unlinkJob" data-test="pc-unlink-job" />
          </span>
        </div>

        <div v-if="recordingBlobUrl" class="audio-block">
          <h3>Recording</h3>
          <audio :src="recordingBlobUrl" controls />
        </div>

        <div v-if="voicemailBlobUrl" class="audio-block">
          <h3>Voicemail</h3>
          <audio :src="voicemailBlobUrl" controls @play="markHeard" />
          <p v-if="transcript" class="transcript">{{ transcript }}</p>
        </div>

        <div v-if="audioError" class="error-banner">{{ audioError }}</div>

        <div class="job-linker">
          <Button
            v-if="!jobPickerOpen && !detail.job_id"
            label="Link to job"
            icon="pi pi-link"
            severity="secondary"
            size="small"
            @click="openJobPicker"
            data-test="pc-link-job-open"
          />
          <div v-if="jobPickerOpen" class="job-picker">
            <InputText
              v-model="jobPickerQuery"
              placeholder="Search jobs by title / customer / id…"
              class="picker-input"
              @input="searchJobs"
              data-test="pc-job-picker-query"
            />
            <div v-if="jobPickerError" class="error-banner">{{ jobPickerError }}</div>
            <ul class="job-list" v-if="jobPickerResults.length > 0">
              <li
                v-for="j in jobPickerResults"
                :key="j.id"
                class="job-row"
                @click="linkJob(j.id)"
                data-test="pc-job-pick-row"
              >
                <span class="job-title">{{ j.title || j.id }}</span>
                <span v-if="j.customer_name" class="text-muted"> · {{ j.customer_name }}</span>
                <span v-if="j.lifecycle_stage" class="text-muted"> · {{ j.lifecycle_stage }}</span>
              </li>
            </ul>
            <div v-else-if="!jobPickerError" class="text-muted">No matches.</div>
            <Button label="Cancel" link size="small" @click="jobPickerOpen = false" />
          </div>
        </div>
      </div>
    </Dialog>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { callerDisplay, friendlyStatus, prettyDirection, renderOwnNumber } from '../utils/phoneComLabels'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Select from 'primevue/select'
import DatePicker from 'primevue/datepicker'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import ProgressSpinner from 'primevue/progressspinner'
import Dialog from 'primevue/dialog'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()
const toast = useToast()

const calls = ref([])
const total = ref(0)
const page = ref(1)
const perPage = ref(50)
const direction = ref(null)
const hasVoicemail = ref(null)
const dateFrom = ref(null)
const dateTo = ref(null)
const loading = ref(false)
const error = ref(null)

const ownNumbers = ref([])

const directionOptions = [
  { label: 'All directions', value: null },
  { label: 'Inbound', value: 'in' },
  { label: 'Outbound', value: 'out' },
]
const voicemailOptions = [
  { label: 'All', value: null },
  { label: 'With voicemail', value: 'true' },
  { label: 'No voicemail', value: 'false' },
]

function statusSeverity(call) {
  const s = friendlyStatus(call)
  if (s === 'Voicemail') return 'warning'
  if (s === 'Forwarded') return 'info'
  if (s === 'Completed') return 'success'
  if (s === 'Missed' || s === 'Canceled') return 'danger'
  return 'secondary'
}

function formatDateTime(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

function formatDuration(s) {
  if (!s) return ''
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}:${String(sec).padStart(2, '0')}`
}

const fetchCalls = async () => {
  loading.value = true
  error.value = null
  try {
    const params = new URLSearchParams()
    params.set('page', page.value)
    params.set('per_page', perPage.value)
    if (direction.value) params.set('direction', direction.value)
    if (hasVoicemail.value !== null && hasVoicemail.value !== '') {
      params.set('has_voicemail', hasVoicemail.value)
    }
    if (dateFrom.value) {
      const d = dateFrom.value instanceof Date ? dateFrom.value : new Date(dateFrom.value)
      params.set('from', d.toISOString())
    }
    if (dateTo.value) {
      const d = dateTo.value instanceof Date ? dateTo.value : new Date(dateTo.value)
      // End of day so the inclusive range matches what users expect.
      d.setHours(23, 59, 59, 999)
      params.set('to', d.toISOString())
    }
    const r = await api.get(`/api/phone-com/calls?${params.toString()}`)
    calls.value = r.items
    total.value = r.total
  } catch (err) {
    error.value = err.message || 'Failed to load calls'
  } finally {
    loading.value = false
  }
}

const fetchOwnNumbers = async () => {
  try {
    const r = await api.get('/api/phone-com/numbers')
    ownNumbers.value = r.items || []
  } catch (_e) {
    ownNumbers.value = []
  }
}

const onFilterChange = () => {
  page.value = 1
  fetchCalls()
}

// Detail modal state
const selected = ref(null)
const detail = ref(null)
const transcript = ref('')
const detailLoading = ref(false)
const detailVisible = ref(false)

const recordingBlobUrl = ref(null)
const voicemailBlobUrl = ref(null)
const audioError = ref(null)

function _authHeaders() {
  const tok = sessionStorage.getItem('gdx_access_token')
    || localStorage.getItem('gdx_access_token')
    || localStorage.getItem('auth_token')
    || ''
  return tok ? { Authorization: `Bearer ${tok}` } : {}
}

async function _fetchAudioBlob(path) {
  const r = await fetch(path, { headers: _authHeaders() })
  if (!r.ok) throw new Error(`audio fetch ${r.status}`)
  return URL.createObjectURL(await r.blob())
}

function _revoke(url) {
  if (url && url.startsWith('blob:')) URL.revokeObjectURL(url)
}

const openDetail = async (call) => {
  selected.value = call
  detail.value = null
  transcript.value = ''
  audioError.value = null
  _revoke(recordingBlobUrl.value)
  _revoke(voicemailBlobUrl.value)
  recordingBlobUrl.value = null
  voicemailBlobUrl.value = null
  detailVisible.value = true
  detailLoading.value = true
  try {
    detail.value = await api.get(`/api/phone-com/calls/${call.id}`)
    if (detail.value.has_voicemail) {
      try {
        const t = await api.get(`/api/phone-com/calls/${call.id}/voicemail-transcript`)
        transcript.value = t.transcript || ''
      } catch (_e) {
        transcript.value = ''
      }
      try {
        voicemailBlobUrl.value = await _fetchAudioBlob(
          `/api/phone-com/calls/${call.id}/voicemail-audio`,
        )
      } catch (err) {
        audioError.value = `voicemail: ${err.message}`
      }
    }
    if (detail.value.has_recording) {
      try {
        recordingBlobUrl.value = await _fetchAudioBlob(
          `/api/phone-com/calls/${call.id}/recording`,
        )
      } catch (err) {
        audioError.value = `${audioError.value ? audioError.value + '; ' : ''}recording: ${err.message}`
      }
    }
  } catch (err) {
    error.value = err.message || 'Failed to load call detail'
  } finally {
    detailLoading.value = false
  }
}

const markHeard = async () => {
  if (!selected.value) return
  try {
    await api.post(`/api/phone-com/calls/${selected.value.id}/mark-heard`)
  } catch (err) {
    error.value = err.message || 'Failed to mark heard'
  }
}

const closeDetail = () => {
  _revoke(recordingBlobUrl.value)
  _revoke(voicemailBlobUrl.value)
  recordingBlobUrl.value = null
  voicemailBlobUrl.value = null
  audioError.value = null
  selected.value = null
  detail.value = null
  jobPickerOpen.value = false
  jobPickerQuery.value = ''
  jobPickerResults.value = []
}

// Job picker
const jobPickerOpen = ref(false)
const jobPickerQuery = ref('')
const jobPickerResults = ref([])
const jobPickerError = ref(null)

const openJobPicker = () => {
  jobPickerOpen.value = true
  searchJobs()
}

const searchJobs = async () => {
  jobPickerError.value = null
  try {
    const q = jobPickerQuery.value
    const r = await api.get(`/api/jobs?per_page=20${q ? '&q=' + encodeURIComponent(q) : ''}`)
    jobPickerResults.value = r.items || r.jobs || (Array.isArray(r) ? r : [])
  } catch (err) {
    jobPickerError.value = err.message || 'Job search failed'
    jobPickerResults.value = []
  }
}

const linkJob = async (jobId) => {
  if (!selected.value) return
  try {
    await api.patch(`/api/phone-com/calls/${selected.value.id}/job`, { job_id: jobId })
    if (detail.value) detail.value.job_id = jobId
    jobPickerOpen.value = false
    jobPickerQuery.value = ''
    jobPickerResults.value = []
  } catch (err) {
    jobPickerError.value = err.message || 'Failed to link'
  }
}

const unlinkJob = async () => {
  if (!selected.value) return
  try {
    await api.patch(`/api/phone-com/calls/${selected.value.id}/job`, { job_id: null })
    if (detail.value) {
      detail.value.job_id = null
      detail.value.job_title = null
    }
  } catch (err) {
    error.value = err.message || 'Failed to unlink'
  }
}

// Click-to-call: Phone.com rings the dispatcher's own extension, then bridges
// the customer. The number to dial is the customer's side of the call.
function customerNumber(call) {
  return call.direction === 'out' ? call.to_number : call.from_number
}

const originateCall = async (call) => {
  const to = customerNumber(call)
  if (!to) return
  try {
    await api.post('/api/phone-com/calls/originate', {
      to,
      customer_id: call.customer_id || undefined,
      job_id: call.job_id || undefined,
    })
    toast.add({
      severity: 'success',
      summary: 'Calling…',
      detail: `Your Phone.com extension will ring, then connect to ${to}.`,
      life: 4000,
    })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Call failed',
      detail: err.message || 'Could not start call — check Settings → Phone.com.',
      life: 5000,
    })
  }
}

const blockCallerNumber = async (call) => {
  if (!call?.from_number) return
  const label = call.customer_name || call.caller_name || 'Spam caller'
  if (!(await confirmAsync({ header: 'Confirm', message: `Block ${call.from_number} from calling in?` }))) return
  try {
    await api.post('/api/phone-com/blocked-calls', {
      name: label,
      number: call.from_number,
      direction: 'in',
      action: 'block',
    })
    error.value = null
    toast.add({ severity: 'success', summary: `Blocked ${call.from_number}`, detail: 'Number added to Phone.com block list.', life: 3000 })
  } catch (err) {
    error.value = err.message || 'Failed to block number'
  }
}

watch(page, fetchCalls)

onMounted(() => {
  fetchCalls()
  fetchOwnNumbers()
})
</script>

<style scoped>
.phone-com-calls {
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
.action-icon {
  margin-right: 0.4rem;
  text-decoration: none;
  color: inherit;
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
.audio-block {
  margin-top: 1rem;
}
.audio-block h3 {
  margin: 0 0 0.4rem;
  font-size: 0.95rem;
}
.audio-block audio {
  width: 100%;
}
.transcript {
  font-style: italic;
  margin-top: 0.4rem;
  color: var(--p-text-muted-color);
}
.job-linker {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--p-content-border-color);
}
.job-picker {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-top: 0.5rem;
}
.picker-input {
  width: 100%;
}
.job-list {
  list-style: none;
  padding: 0;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
}
.job-row {
  padding: 0.5rem 0.75rem;
  cursor: pointer;
  border-bottom: 1px solid var(--p-content-border-color);
}
.job-row:last-child {
  border-bottom: none;
}
.job-row:hover {
  background: var(--p-content-hover-background);
}

/* Mobile: collapse the wide DataTable into vertical cards. PrimeVue's
   responsiveLayout="scroll" still allows pinch-zoom, but on a 390px
   phone the columns crunch unreadably. Stacking each column under the
   "When" cell keeps the data legible. */
@media (max-width: 768px) {
  .filter-select { min-width: 0; flex: 1; }
  :deep(.p-datatable .p-datatable-thead) { display: none; }
  :deep(.p-datatable .p-datatable-tbody > tr) {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--p-content-border-color, #e5e7eb);
    border-radius: 0.55rem;
    margin-bottom: 0.45rem;
    padding: 0.6rem 0.75rem;
    gap: 0.2rem;
  }
  :deep(.p-datatable .p-datatable-tbody > tr > td) {
    border: 0;
    padding: 0.1rem 0;
    width: 100% !important;
    text-align: left;
  }
  :deep(.p-datatable .p-datatable-tbody > tr > td:nth-child(1))::before { content: '🕐 '; }
  :deep(.p-datatable .p-datatable-tbody > tr > td:nth-child(3))::before { content: 'From: '; font-weight: 600; }
  :deep(.p-datatable .p-datatable-tbody > tr > td:nth-child(4))::before { content: 'To: '; font-weight: 600; }
  :deep(.p-datatable .p-datatable-tbody > tr > td:nth-child(6))::before { content: 'Duration: '; }
  :deep(.p-paginator) { flex-wrap: wrap; }
}
</style>
