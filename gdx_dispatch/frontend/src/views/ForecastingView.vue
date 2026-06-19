<template>
  <section class="forecasting-view view-card">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Forecasting</h1>
      </template>
      <template #end>
        <Select
          v-model="windowDays"
          :options="windowOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          @change="onWindowChange"
        />
        <Button
          label="Recurring Payments"
          icon="pi pi-refresh"
          severity="secondary"
          @click="$router.push('/forecasting/recurring')"
        />
        <Button
          label="Settings"
          icon="pi pi-cog"
          severity="secondary"
          @click="settingsOpen = true"
        />
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          @click="refreshAll"
        />
      </template>
    </Toolbar>

    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">Expected revenue · next {{ windowDays }} days</div>
        <div class="kpi-value">{{ money(projection?.expected_total) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Open AR (face value)</div>
        <div class="kpi-value">{{ money(projection?.open_ar?.open_total) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Scheduled jobs in window</div>
        <div class="kpi-value">{{ projection?.scheduled_jobs?.job_count ?? 0 }}</div>
      </div>
      <div class="kpi kpi-clickable" @click="$router.push('/forecasting/recurring')" role="link" tabindex="0">
        <div class="kpi-label">Recurring in window (observed + QBO)</div>
        <div class="kpi-value">{{ projection?.recurring?.count ?? 0 }}</div>
        <div class="kpi-sub" v-if="projection?.recurring?.sources">
          {{ projection.recurring.sources.observed?.count ?? 0 }} observed ·
          {{ projection.recurring.sources.qbo_templates?.count ?? 0 }} QBO templates
          <span v-if="projection.recurring.qbo_overridden">
            · {{ projection.recurring.qbo_overridden }} dup'd
          </span>
        </div>
      </div>
    </div>

    <div class="panel-grid">
      <div class="panel">
        <h2 class="panel-heading">AR Aging → Expected Cash</h2>
        <div v-if="projectionError" class="error-banner">{{ projectionError }}</div>
        <div v-if="projectionLoading" class="spinner-wrap">
          <ProgressSpinner />
        </div>
        <DataTable
          v-else-if="arBucketRows.length"
          :value="arBucketRows"
          stripedRows
          responsiveLayout="scroll"
        >
          <Column field="label" header="Aging bucket" />
          <Column header="Invoices" style="width: 110px">
            <template #body="{ data }">{{ data.invoice_count }}</template>
          </Column>
          <Column header="Open" style="width: 140px">
            <template #body="{ data }">{{ money(data.open_total) }}</template>
          </Column>
          <Column header="Rate" style="width: 90px">
            <template #body="{ data }">{{ (data.rate * 100).toFixed(0) }}%</template>
          </Column>
          <Column header="Expected" style="width: 140px">
            <template #body="{ data }">{{ money(data.expected_total) }}</template>
          </Column>
        </DataTable>
        <div v-else class="empty-message">No open invoices.</div>
      </div>

      <div class="panel">
        <h2 class="panel-heading">Scheduled Jobs in Window</h2>
        <DataTable
          v-if="(projection?.scheduled_jobs?.jobs || []).length"
          :value="projection.scheduled_jobs.jobs"
          :paginator="false"
          stripedRows
          responsiveLayout="scroll"
        >
          <Column field="job_number" header="Job #" />
          <Column field="title" header="Title" />
          <Column header="Scheduled">
            <template #body="{ data }">{{ formatDate(data.scheduled_at) }}</template>
          </Column>
          <Column header="Est. value" style="width: 140px">
            <template #body="{ data }">{{ money(data.estimated_value) }}</template>
          </Column>
        </DataTable>
        <div v-else class="empty-message">No scheduled jobs in window.</div>
      </div>

      <div class="panel panel-wide">
        <div class="panel-header">
          <h2 class="panel-heading">QuickBooks Recurring Transactions</h2>
          <Button
            label="Sync from QuickBooks"
            icon="pi pi-cloud-download"
            severity="secondary"
            :loading="recurringLoading"
            @click="syncRecurring"
          />
        </div>
        <div v-if="recurringError" class="error-banner">{{ recurringError }}</div>
        <DataTable
          v-if="recurring.length"
          :value="recurring"
          stripedRows
          responsiveLayout="scroll"
        >
          <Column field="name" header="Name" />
          <Column field="txn_type" header="Type" style="width: 120px" />
          <Column field="customer_name" header="Customer" />
          <Column header="Amount" style="width: 130px">
            <template #body="{ data }">{{ money(data.amount) }}</template>
          </Column>
          <Column header="Next" style="width: 130px">
            <template #body="{ data }">{{ formatDate(data.next_date) }}</template>
          </Column>
          <Column header="Frequency" style="width: 140px">
            <template #body="{ data }">{{ frequencyLabel(data) }}</template>
          </Column>
          <Column header="Active" style="width: 90px">
            <template #body="{ data }">
              <Tag :value="data.active ? 'Active' : 'Inactive'" :severity="data.active ? 'success' : 'secondary'" />
            </template>
          </Column>
        </DataTable>
        <div v-else class="empty-message">
          No recurring transactions cached. Click <strong>Sync from QuickBooks</strong> to pull them.
        </div>
      </div>

      <div class="panel panel-wide">
        <div class="panel-header">
          <div>
            <h2 class="panel-heading">Recurring Payments (Observed + Manual)</h2>
            <p class="panel-sub">
              Detected from bank activity — the ground truth counterpart to QBO templates above.
              Confirm suggestions to count them in your forecast.
            </p>
          </div>
          <div class="panel-actions">
            <Button
              label="Detect Now"
              icon="pi pi-search"
              severity="secondary"
              :loading="recurringStreamsLoading"
              @click="onDetectStreams"
            />
            <Button
              label="Manage"
              icon="pi pi-external-link"
              severity="secondary"
              @click="$router.push('/forecasting/recurring')"
            />
          </div>
        </div>
        <div v-if="recurringStreamsError" class="error-banner">{{ recurringStreamsError }}</div>
        <DataTable
          v-if="recurringStreams.length"
          :value="recurringStreams"
          stripedRows
          responsiveLayout="scroll"
          :paginator="recurringStreams.length > 10"
          :rows="10"
        >
          <Column header="Payment">
            <template #body="{ data }">
              <div class="cell-label">
                <div class="label-text">{{ data.label }}</div>
                <div class="label-meta">{{ data.payee_pattern }}</div>
              </div>
            </template>
          </Column>
          <Column header="Amount" style="width: 160px">
            <template #body="{ data }">{{ formatAmountRange(data) }}</template>
          </Column>
          <Column field="cadence" header="Cadence" style="width: 110px">
            <template #body="{ data }"><span class="cap">{{ data.cadence }}</span></template>
          </Column>
          <Column header="Next" style="width: 110px">
            <template #body="{ data }">{{ formatDate(data.next_expected_date) }}</template>
          </Column>
          <Column header="Status" style="width: 110px">
            <template #body="{ data }">
              <Tag :value="data.status" :severity="statusSeverity(data.status)" />
            </template>
          </Column>
          <Column header="" style="width: 120px">
            <template #body="{ data }">
              <Button
                v-if="data.status === 'suggested'"
                label="Confirm"
                icon="pi pi-check"
                size="small"
                @click="onConfirmStream(data.id)"
              />
            </template>
          </Column>
        </DataTable>
        <div v-else class="empty-message">
          No recurring patterns detected yet. Click <strong>Detect Now</strong> after a bank sync,
          or <strong>Manage</strong> to mark transactions as recurring manually.
        </div>
      </div>
    </div>

    <Dialog
      v-model:visible="settingsOpen"
      modal
      header="Forecast settings"
      :style="{ width: '520px' }"
    >
      <div v-if="settingsLoading" class="spinner-wrap">
        <ProgressSpinner />
      </div>
      <div v-else-if="settings" class="settings-grid">
        <label class="settings-row">
          <span>Default window (days)</span>
          <InputNumber v-model="draft.default_window_days" :min="1" :max="365" />
        </label>
        <label class="settings-row">
          <span>Collection rate · 0–30 days</span>
          <InputNumber v-model="draft.collect_rate_0_30" :min="0" :max="1" :minFractionDigits="2" :maxFractionDigits="4" />
        </label>
        <label class="settings-row">
          <span>Collection rate · 31–60 days</span>
          <InputNumber v-model="draft.collect_rate_31_60" :min="0" :max="1" :minFractionDigits="2" :maxFractionDigits="4" />
        </label>
        <label class="settings-row">
          <span>Collection rate · 61–90 days</span>
          <InputNumber v-model="draft.collect_rate_61_90" :min="0" :max="1" :minFractionDigits="2" :maxFractionDigits="4" />
        </label>
        <label class="settings-row">
          <span>Collection rate · 90+ days</span>
          <InputNumber v-model="draft.collect_rate_90_plus" :min="0" :max="1" :minFractionDigits="2" :maxFractionDigits="4" />
        </label>
        <label class="settings-row">
          <span>Scheduled job realization rate</span>
          <InputNumber v-model="draft.scheduled_realization_rate" :min="0" :max="1" :minFractionDigits="2" :maxFractionDigits="4" />
        </label>
        <label class="settings-row">
          <span>Include QB recurring transactions</span>
          <ToggleSwitch v-model="draft.include_recurring" />
        </label>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="settingsOpen = false" />
        <Button label="Save" :loading="settingsSaving" @click="onSaveSettings" />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useForecasting } from '../composables/useForecasting'
import { useRecurringStreams } from '../composables/useRecurringStreams'

import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import Select from 'primevue/select'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import InputNumber from 'primevue/inputnumber'
import ToggleSwitch from 'primevue/toggleswitch'
import ProgressSpinner from 'primevue/progressspinner'

const windowOptions = [
  { label: 'Next 30 days', value: 30 },
  { label: 'Next 60 days', value: 60 },
  { label: 'Next 90 days', value: 90 },
]

const f = useForecasting()
const rs = useRecurringStreams()
const recurringStreams = rs.streams
const recurringStreamsLoading = rs.loading
const recurringStreamsError = rs.error

const windowDays = ref(30)

const projection = f.projection
const projectionLoading = f.projectionLoading
const projectionError = f.projectionError
const recurring = f.recurring
const recurringLoading = f.recurringLoading
const recurringError = f.recurringError
const settings = f.settings
const settingsLoading = f.settingsLoading
const settingsSaving = f.settingsSaving

const settingsOpen = ref(false)
const draft = ref({})

watch(settings, (s) => {
  if (s) draft.value = { ...s }
}, { immediate: true })

watch(settingsOpen, (open) => {
  if (open && settings.value) draft.value = { ...settings.value }
})

const arBucketRows = computed(() => {
  const buckets = projection.value?.open_ar?.by_bucket
  const s = settings.value
  if (!buckets) return []
  const labelMap = {
    '0_30': '0–30 days',
    '31_60': '31–60 days',
    '61_90': '61–90 days',
    '90_plus': '90+ days',
  }
  const rateMap = {
    '0_30': s?.collect_rate_0_30 ?? 0,
    '31_60': s?.collect_rate_31_60 ?? 0,
    '61_90': s?.collect_rate_61_90 ?? 0,
    '90_plus': s?.collect_rate_90_plus ?? 0,
  }
  return Object.keys(labelMap).map((key) => ({
    key,
    label: labelMap[key],
    rate: rateMap[key],
    ...buckets[key],
  }))
})

function money(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(n))
}

function formatDate(s) {
  if (!s) return ''
  const t = Date.parse(s)
  if (Number.isNaN(t)) return s
  return new Date(t).toLocaleDateString()
}

function frequencyLabel(row) {
  if (!row?.interval_type) return ''
  const n = row?.num_interval || 1
  return n > 1 ? `Every ${n} ${row.interval_type.toLowerCase()}` : row.interval_type
}

async function onWindowChange() {
  await f.setWindow(windowDays.value)
}

async function refreshAll() {
  await Promise.all([f.loadProjection(), f.loadRecurring(), f.loadSettings()])
}

async function syncRecurring() {
  await f.syncRecurring()
  await f.loadProjection()
}

async function onSaveSettings() {
  await f.saveSettings(draft.value)
  settingsOpen.value = false
}

function formatAmountRange(s) {
  const lo = Number(s.amount_min);
  const hi = Number(s.amount_max);
  if (Math.abs(hi - lo) < 0.01) return money(lo);
  return `${money(lo)} – ${money(hi)}`;
}

function statusSeverity(status) {
  if (status === 'active') return 'success';
  if (status === 'suggested') return 'info';
  if (status === 'paid_off') return 'success';
  if (status === 'cancelled' || status === 'expired') return 'secondary';
  return 'secondary';
}

async function onDetectStreams() {
  await rs.detectNow();
  await rs.list();
}

async function onConfirmStream(id) {
  await rs.confirm(id);
  await rs.list();
  await f.loadProjection();
}

onMounted(async () => {
  await Promise.all([f.loadSettings(), f.loadProjection(), f.loadRecurring(), rs.list()])
  if (settings.value?.default_window_days) {
    windowDays.value = Number(settings.value.default_window_days)
    if (windowDays.value !== 30) await f.setWindow(windowDays.value)
  }
})
</script>

<style scoped>
.forecasting-view {
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
  min-width: 12rem;
}

.kpi-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.kpi {
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 1rem;
}

.kpi-label {
  color: var(--p-text-muted-color);
  font-size: 0.8125rem;
  margin-bottom: 0.25rem;
}

.kpi-value {
  font-size: 1.5rem;
  font-weight: 600;
}

.kpi-sub {
  color: var(--p-text-muted-color);
  font-size: 0.75rem;
  margin-top: 0.25rem;
}

.kpi-clickable {
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.kpi-clickable:hover,
.kpi-clickable:focus-visible {
  border-color: var(--p-primary-color);
  outline: none;
}

.panel-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 1rem;
}

.panel {
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.panel-wide {
  grid-column: 1 / -1;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.panel-heading {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
}

.panel-sub {
  margin: 0.25rem 0 0;
  color: var(--p-text-muted-color);
  font-size: 0.8125rem;
}

.panel-actions {
  display: flex;
  gap: 0.5rem;
}

.cell-label .label-text { font-weight: 600; }
.cell-label .label-meta {
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
  margin-top: 0.125rem;
}
.cap { text-transform: capitalize; }

.settings-grid {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.settings-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.settings-row > span {
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
  padding: 1.5rem;
}

.empty-message {
  text-align: center;
  padding: 1.25rem;
  color: var(--p-text-muted-color);
}
</style>
