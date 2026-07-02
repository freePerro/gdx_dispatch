<template>
    <section class="vendor-statement-detail-view view-card">
      <Toolbar>
        <template #start>
          <Button
            v-tooltip="'Back'"
            aria-label="Back"
            icon="pi pi-arrow-left"
            severity="secondary"
            text
            @click="$router.push('/vendor-statements')"
          />
          <h1 class="view-heading">
            {{ statement?.vendor_name || 'Vendor Statement' }}
            <span v-if="statement?.statement_date" class="muted">
              · {{ formatDate(statement.statement_date) }}
            </span>
          </h1>
        </template>
        <template #end>
          <Tag
            v-if="statement"
            :value="statement.status"
            :severity="statusSeverity(statement.status)"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <template v-else-if="statement">
        <div class="summary-grid">
          <div class="summary-tile">
            <div class="tile-label">Lines</div>
            <div class="tile-value">{{ statement.line_count }}</div>
          </div>
          <div class="summary-tile">
            <div class="tile-label">Total</div>
            <div class="tile-value">{{ formatCurrency(statement.raw_total) }}</div>
          </div>
          <div class="summary-tile">
            <div class="tile-label">Vendor Code</div>
            <div class="tile-value">{{ statement.vendor_code || '—' }}</div>
          </div>
          <div class="summary-tile">
            <div class="tile-label">Parser</div>
            <div class="tile-value">{{ statement.parser_name }} v{{ statement.parser_version }}</div>
          </div>
        </div>

        <DataTable
          :value="statement.lines"
          stripedRows
          responsiveLayout="scroll"
          data-testid="vendor-statement-lines-table"
        >
          <template #empty>
            <div class="empty-message">No line items.</div>
          </template>

          <Column header="#" style="width: 60px">
            <template #body="{ data }">{{ data.line_no + 1 }}</template>
          </Column>
          <Column header="Date" style="width: 120px">
            <template #body="{ data }">{{ formatDate(data.line_date) }}</template>
          </Column>
          <Column field="vendor_invoice_no" header="Vendor Inv #" style="width: 130px" />
          <Column field="vendor_job_no" header="Job #" style="width: 110px" />
          <Column field="po_ref" header="PO" style="width: 140px" />
          <Column field="description" header="Description" />
          <Column header="Amount" style="width: 130px; text-align: right">
            <template #body="{ data }">{{ formatCurrency(data.amount) }}</template>
          </Column>
          <Column header="Balance" style="width: 130px; text-align: right">
            <template #body="{ data }">{{ formatCurrency(data.balance) }}</template>
          </Column>
          <Column header="Aging" style="width: 110px">
            <template #body="{ data }">
              <Tag
                v-if="data.aging_bucket"
                :value="data.aging_bucket"
                :severity="agingSeverity(data.aging_bucket)"
              />
            </template>
          </Column>
          <Column header="Classification" style="width: 240px">
            <template #body="{ data }">
              <SelectButton
                :modelValue="data.classification || 'unknown'"
                :options="classificationOptions"
                optionLabel="label"
                optionValue="value"
                :allowEmpty="false"
                :disabled="savingLineId === data.id"
                class="classification-override"
                @update:modelValue="(value) => updateLine(data, { classification: value })"
              />
            </template>
          </Column>
          <Column header="Notes" style="min-width: 240px">
            <template #body="{ data }">
              <Textarea
                :modelValue="data.notes || ''"
                rows="1"
                autoResize
                placeholder="Add a note…"
                class="notes-input"
                :disabled="savingLineId === data.id"
                @blur="(event) => onNotesBlur(data, event.target.value)"
              />
            </template>
          </Column>
        </DataTable>
      </template>
    </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useApi } from '../composables/useApi'
import { formatDate } from '../utils/dates'
import { formatMoney as formatCurrency } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import SelectButton from 'primevue/selectbutton'
import Textarea from 'primevue/textarea'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const route = useRoute()

const statement = ref(null)
const loading = ref(false)
const error = ref(null)
const savingLineId = ref(null)

const classificationOptions = [
  { label: 'Job', value: 'job' },
  { label: 'Inventory', value: 'inventory' },
  { label: 'Unknown', value: 'unknown' },
]

function statusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (k === 'reconciled') return 'success'
  if (k === 'review') return 'warning'
  if (k === 'parsed') return 'info'
  return 'secondary'
}

function classificationSeverity(value) {
  switch ((value || 'unknown').toLowerCase()) {
    case 'job': return 'info'
    case 'inventory': return 'warning'
    default: return 'secondary'
  }
}

async function updateLine(line, payload) {
  if (!statement.value) return
  savingLineId.value = line.id
  try {
    const updated = await api.patch(
      `/api/vendor-statements/${statement.value.id}/lines/${line.id}`,
      payload,
    )
    Object.assign(line, updated)
  } catch (err) {
    error.value = err.message || 'Failed to update line'
  } finally {
    savingLineId.value = null
  }
}

function onNotesBlur(line, value) {
  const cleaned = (value || '').trim()
  const current = (line.notes || '').trim()
  if (cleaned === current) return
  updateLine(line, { notes: cleaned })
}

function agingSeverity(bucket) {
  switch (bucket) {
    case 'current':
    case '0-29': return 'success'
    case '30-59': return 'info'
    case '60-89': return 'warning'
    case '90-119':
    case '120+':
    case 'retainage': return 'danger'
    default: return 'secondary'
  }
}

const fetchDetail = async () => {
  loading.value = true
  error.value = null
  try {
    statement.value = await api.get(`/api/vendor-statements/${route.params.id}`)
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

onMounted(fetchDetail)
</script>

<style scoped>
.vendor-statement-detail-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.muted { color: var(--p-text-muted-color); font-weight: 400; }
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.75rem;
}
.summary-tile {
  background: var(--p-surface-50, #f8fafc);
  border: 1px solid var(--p-surface-200, #e2e8f0);
  border-radius: 8px;
  padding: 0.75rem 1rem;
}
.tile-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--p-text-muted-color);
}
.tile-value {
  font-size: 1.125rem;
  font-weight: 600;
  margin-top: 0.25rem;
}
.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message {
  text-align: center;
  padding: 1.5rem;
  color: var(--p-text-muted-color);
}
.classification-cell {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.classification-override :deep(.p-button) {
  padding: 0.25rem 0.5rem;
  font-size: 0.75rem;
}
.notes-input {
  width: 100%;
  min-height: 2rem;
  font-size: 0.85rem;
}
</style>
