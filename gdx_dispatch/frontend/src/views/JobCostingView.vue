<template>
    <section class="job-costing-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Job Costing</h2>
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="tab-bar">
        <TabList>
          <Tab value="profitability">Profitability</Tab>
          <Tab value="markup">Markup Rules</Tab>
          <Tab value="calculator">Price Calculator</Tab>
        </TabList>
      </Tabs>

      <div class="tab-panel">
        <div v-if="activeTab === 'profitability'">
          <Toolbar class="tab-toolbar">
            <template #start>
              <div class="date-range">
                <label>
                  From
                  <input type="date" v-model="startDate" />
                </label>
                <label>
                  To
                  <input type="date" v-model="endDate" />
                </label>
              </div>
            </template>
            <template #end>
              <span class="days-label">Showing {{ rangeDays }} day<span v-if="rangeDays !== 1">s</span></span>
            </template>
          </Toolbar>

          <div v-if="profitabilityLoading" class="spinner-wrap small">
            <ProgressSpinner />
          </div>

          <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
            v-else
            :value="profitabilityRows"
            dataKey="job_id"
            paginator
            :rows="20"
            striped-rows
            @row-click="openJobDetail($event.data)"
            
          >
            <template #empty>
              <div class="empty-state">
                <i class="pi pi-chart-line" style="font-size:3rem; color:var(--p-text-muted-color)"></i>
                <h3>No profitability data</h3>
                <p>Adjust the date range above to load more jobs.</p>
              </div>
            </template>
            <Column header="Job">
              <template #body="{ data }">
                <div class="job-cell">
                  <span class="job-title">{{ data.job_number || data.job_title || `Job ${String(data.job_id).slice(0, 8)}` }}</span>
                  <span v-if="data.customer_name" class="job-customer">{{ data.customer_name }}</span>
                </div>
              </template>
            </Column>
            <Column field="invoice_total" header="Invoice Total" style="width:140px">
              <template #body="{ data }">{{ formatCurrency(data.invoice_total) }}</template>
            </Column>
            <Column field="cost_estimate" header="Cost Estimate" style="width:140px">
              <template #body="{ data }">{{ formatCurrency(data.cost_estimate) }}</template>
            </Column>
            <Column field="profit" header="Profit" style="width:140px">
              <template #body="{ data }">{{ formatCurrency(data.profit) }}</template>
            </Column>
            <Column field="margin_percent" header="Margin" style="width:140px">
              <template #body="{ data }">
                <Badge :value="formatPercent(data.margin_percent)" :severity="marginSeverity(data.margin_percent)" />
              </template>
            </Column>
            <Column header="Actions" style="width:120px">
              <template #body="{ data }">
                <Button text size="small" icon="pi pi-eye" label="Details" @click.stop="openJobDetail(data)" />
              </template>
            </Column>
          </DataTable>
        </div>

        <div v-else-if="activeTab === 'markup'">
          <Toolbar>
            <template #start>
              <h3>Markup Rules</h3>
            </template>
            <template #end>
              <Button label="+ New Rule" icon="pi pi-plus" severity="primary" @click="openRuleDialog()" />
            </template>
          </Toolbar>

          <div v-if="markupLoading" class="spinner-wrap small">
            <ProgressSpinner />
          </div>

          <DataTable
      responsiveLayout="scroll"
            v-else
            :value="markupRules"
            dataKey="id"
            paginator
            :rows="10"
            striped-rows
          >
            <template #empty>
              <div class="empty-state">
                <i class="pi pi-sliders-h" style="font-size:3rem; color:var(--p-text-muted-color)"></i>
                <h3>No markup rules</h3>
                <p>Create a rule to keep estimates profitable.</p>
              </div>
            </template>
            <Column field="category" header="Category" />
            <Column field="markup_percent" header="Markup" style="width:140px">
              <template #body="{ data }">{{ formatPercent(data.markup_percent) }}</template>
            </Column>
            <Column field="minimum_margin_percent" header="Min Margin" style="width:140px">
              <template #body="{ data }">{{ formatPercent(data.minimum_margin_percent) }}</template>
            </Column>
            <Column field="active" header="Status" style="width:120px">
              <template #body="{ data }">
                <Badge :value="data.active ? 'Active' : 'Inactive'" :severity="data.active ? 'success' : 'danger'" />
              </template>
            </Column>
            <Column header="Actions" style="width:160px">
              <template #body="{ data }">
                <Button text size="small" icon="pi pi-pencil" aria-label="Edit" label="Edit" @click.stop="openRuleDialog(data)" />
                <Button
                  text
                  size="small"
                  icon="pi pi-trash"
                  severity="danger"
                  label="Delete"
                  @click.stop="deleteRule(data)"
                />
              </template>
            </Column>
          </DataTable>
        </div>

        <div v-else-if="activeTab === 'calculator'">
          <section class="calculator-section">
            <header>
              <h3>Price Calculator</h3>
              <p>Calculate a suggested sell price that respects your markup rules.</p>
            </header>
            <form class="calculator-form" @submit.prevent="calculatePrice">
              <div class="form-grid">
                <div class="form-field">
                  <label>Category</label>
                  <Select
                    v-model="calculatorCategory"
                    :options="categoryOptions"
                    optionLabel="label"
                    optionValue="value"
                    filter
                    showClear
                    placeholder="Select category"
                    class="w-full"
                  />
                </div>
                <div class="form-field">
                  <label>Cost</label>
                  <InputNumber v-model="calculatorCost" mode="currency" currency="USD" :min="0" class="w-full" />
                </div>
              </div>
              <Button type="submit" label="Calculate" icon="pi pi-calculator" :loading="calculatorLoading" />
            </form>

            <div v-if="calculatorResult" class="calculator-result">
              <div class="result-row">
                <span>Suggested price</span>
                <strong>{{ formatCurrency(calculatorResult.suggested_price) }}</strong>
              </div>
              <div v-if="calculatorResult.margin_percent !== undefined" class="result-row">
                <span>Margin</span>
                <Badge
                  :value="formatPercent(calculatorResult.margin_percent)"
                  :severity="marginSeverity(calculatorResult.margin_percent)"
                />
              </div>
              <div v-if="calculatorResult.minimum_margin_percent !== undefined" class="result-row">
                <span>Minimum margin</span>
                <span>{{ formatPercent(calculatorResult.minimum_margin_percent) }}</span>
              </div>
              <div v-if="calculatorResult.margin_breakdown" class="breakdown">
                <div v-for="(value, label) in calculatorResult.margin_breakdown" :key="label" class="breakdown-row">
                  <span>{{ label }}</span>
                  <span>{{ formatPercent(value) }}</span>
                </div>
              </div>
            </div>
          </section>
        </div>
      </div>

      <Dialog v-model:visible="showJobDetail" header="Cost breakdown" modal :style="{ width: '900px' }">
        <div v-if="jobLoading" class="spinner-wrap">
          <ProgressSpinner />
        </div>
        <div v-else-if="jobDetail" class="job-detail-body">
          <header class="job-detail-header">
            <div>
              <label class="job-id-label">Job ID:</label>
              <h3>Job {{ jobDetail.job_id || jobDetail.id }}</h3>
              <p class="job-detail-subtitle">Margin {{ formatPercent(jobDetail.margin_percent) }}</p>
            </div>
            <!-- Min Margin % removed. It filtered rows whose margin fell below
                 a threshold, and it could never fire: the only rows it iterated
                 came from GET /api/jobs/{id}/line-items, which returns
                 description/quantity/unit_price/line_total and NO unit_cost. So
                 computeMargin(0, price) returned 100 for every positive price,
                 and the one input that produced a warning was a $0 line — which
                 it then mislabelled as "margin below 15%". A control that cannot
                 compute is the same defect as a button that cannot save. Job
                 margin is on the card below, from the server. -->
          </header>

          <div class="job-detail-summary">
            <div class="summary-card" data-testid="your-cost-card">
              <span class="summary-label">Your Cost ($)</span>
              <strong class="summary-value">{{ formatCurrency(yourCost) }}</strong>
              <!-- Names what the number IS. It used to say "Parts + items" over
                   a client sum of a parts grid that never loaded plus invoice
                   lines — so it was neither. This is the engine's total_cost:
                   labour, parts and overhead. -->
              <small data-testid="your-cost-basis">Labor + parts + overhead</small>
            </div>
            <div class="summary-card">
              <span class="summary-label">Invoice Margin</span>
              <div class="summary-value">
                <Badge
                  :value="formatPercent(jobDetail.margin_percent)"
                  :severity="marginSeverity(jobDetail.margin_percent)"
                />
              </div>
              <small>Based on invoices</small>
            </div>
            <div class="summary-card">
              <span class="summary-label">Selling Price</span>
              <strong class="summary-value">{{ formatCurrency(jobDetail.invoiced_amount) }}</strong>
              <small>Invoice / quote total</small>
            </div>
          </div>

          <!-- READ-ONLY, deliberately. Every write control that used to live in
               this dialog was wired to an endpoint that does not exist: GET and
               POST /api/jobs/{id}/parts 404, DELETE 405, PATCH 501, and
               PATCH /api/jobs/{id}/costing 405. Nothing here has ever saved.
               The rows below come from /api/costing/jobs/{id}, which resolves
               each part against the confirmed vendor bill first and the
               estimator's catalog second — better data than the editable grid
               ever held, and the number this page exists to show. Parts are
               edited on the job itself (parts-needed), which is where the
               capture paths already write. -->
          <!-- NO parts table here, deliberately. JobDetailView's "Parts Used"
               card (shipped #477) already renders this same
               /api/costing/jobs/{id} payload, and renders it BETTER: it handles
               the ambiguous case, where unattributed supplier lines mean the
               catalog estimates might be the same spend and the engine EXCLUDES
               them from the total rather than double-counting. A second table
               here was a weaker copy of a panel one click away — the parallel-fake
               pattern this whole change is about. The dialog's job is the
               profitability summary; parts detail and parts editing both live on
               the job. -->
          <section class="job-detail-section">
            <div class="section-head">
              <h4>Parts</h4>
              <Button
                label="Parts and costs on the job"
                icon="pi pi-external-link"
                severity="secondary"
                text
                size="small"
                data-testid="jc-edit-parts-on-job"
                @click="openJobPage"
              />
            </div>
            <p class="section-note" data-testid="jc-parts-pointer">
              This job's parts, what each one cost and where that cost came from
              are on the job's Parts Used card. Parts are captured there too.
            </p>
          </section>

          <section class="job-detail-section">
            <div class="section-head">
              <h4>Invoice lines</h4>
              <span class="section-note">
                Billed to the customer. Edited on the invoice.
              </span>
            </div>
            <DataTable
              responsiveLayout="scroll"
              :value="jobLineItems"
              :loading="lineItemsLoading"
              dataKey="id"
              striped-rows
              data-testid="items-table"
              class="job-detail-table"
            >
              <template #empty>
                <span class="muted" data-testid="jc-items-empty">
                  Nothing invoiced on this job yet.
                </span>
              </template>
              <Column field="description" header="Description" style="min-width:220px" />
              <Column header="Qty" style="width:100px">
                <template #body="{ data: item }">{{ item.quantity }}</template>
              </Column>
              <Column header="Unit price" style="width:140px">
                <template #body="{ data: item }">
                  {{ formatCurrency(item.unit_price) }}
                </template>
              </Column>
              <Column header="Total" style="width:140px">
                <template #body="{ data: item }">
                  {{ formatCurrency(item.line_total) }}
                </template>
              </Column>
            </DataTable>
          </section>
        </div>
        <div v-else class="spinner-wrap">
          <p class="muted">Unable to load job details.</p>
        </div>
        <!-- No Save. Nothing in this dialog is editable any more, and the
             button that used to be here POSTed to PATCH /api/jobs/{id}/costing,
             which is registered for GET only — it 405'd on every click since it
             was written. A button that cannot save is worse than no button. -->
        <template #footer>
          <Button label="Close" severity="secondary" @click="closeJobDetail" />
        </template>
      </Dialog>

      <!-- The Add Part and Add Item dialogs were removed with the writes they
           fed. Add Part POSTed to /api/jobs/{id}/parts, which no router has
           served since the inventory module router was deleted (#655) — and
           before that it took {part_id, qty_used} while this form sent
           {description, catalog_item_id, qty, unit_cost, unit_price}, so it
           422'd rather than saving. Add Item POSTed to
           /api/jobs/{id}/line-items without the invoice_id that endpoint
           requires, so it 400'd. Parts are captured on the job; invoice
           lines are edited on the invoice. -->

      <Dialog
        v-model:visible="showRuleDialog"
        :header="editingRule ? `Edit ${editingRule.category}` : 'New markup rule'"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Category *</label>
            <InputText v-model="ruleForm.category" class="w-full" />
          </div>
          <div class="form-field">
            <label>Markup %</label>
            <InputNumber v-model="ruleForm.markup_percent" suffix="%" mode="decimal" step="0.1" :min="0" class="w-full" />
          </div>
          <div class="form-field">
            <label>Minimum margin %</label>
            <InputNumber
              v-model="ruleForm.minimum_margin_percent"
              suffix="%"
              mode="decimal"
              step="0.1"
              :min="0"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Active</label>
            <Select
              v-model="ruleForm.active"
              :options="[{ label: 'Active', value: true }, { label: 'Paused', value: false }]"
              class="w-full"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeRuleDialog" />
          <Button label="Save rule" icon="pi pi-check" :loading="savingRule" @click="saveRule" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatMoney as formatCurrency, formatPercent as fmtPercent } from '../composables/useFormatters';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Toolbar from 'primevue/toolbar';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const today = new Date();
const defaultEnd = today.toISOString().slice(0, 10);
const defaultStartDate = new Date(today);
defaultStartDate.setDate(defaultStartDate.getDate() - 30);
const defaultStart = defaultStartDate.toISOString().slice(0, 10);

const activeTab = ref('profitability');
const startDate = ref(defaultStart);
const endDate = ref(defaultEnd);
const profitabilityRows = ref([]);
const profitabilityLoading = ref(false);

const markupRules = ref([]);
const markupLoading = ref(false);
const showRuleDialog = ref(false);
const emptyRule = () => ({
  category: '',
  markup_percent: 0,
  minimum_margin_percent: 0,
  active: true,
});
const ruleForm = ref(emptyRule());
const editingRule = ref(null);
const savingRule = ref(false);

const showJobDetail = ref(false);
const jobLoading = ref(false);
const jobDetail = ref(null);



// The editable parts grid is gone; `jobLineItems` stays because its GET is
// real. Everything the Parts table shows now comes off `jobDetail`, which is
// already fetched for the summary cards — no second request, and no client
// arithmetic over rows that never loaded.
const jobLineItems = ref([]);
const lineItemsLoading = ref(false);
const router = useRouter();

const currentJobId = computed(() => jobDetail.value?.job_id || jobDetail.value?.id || null);

// Parts are edited where the capture paths already write them — on the job.
// The costing dialog reports; it does not own the data.
function openJobPage() {
  if (!currentJobId.value) return;
  router.push(`/jobs/${currentJobId.value}`);
}

function toNumber(value) {
  return Number(value ?? 0);
}






// Parts rows as the costing engine resolved them. `rowKey` exists because the
// engine returns a flat list with no ids — two lines can legitimately share a
// name (the same part billed on two invoices), so the index is the only stable
// key and DataTable needs one.

// The SERVER's total, not a client sum. "Your Cost" used to add a parts grid
// that never loaded to a line-items grid, so it silently reported invoice lines
// only — the one number this dialog exists to show, understated by every part
// on the job.
const yourCost = computed(() => toNumber(jobDetail.value?.total_cost));



const calculatorCategory = ref(null);
const calculatorCost = ref(null);
const calculatorLoading = ref(false);
const calculatorResult = ref(null);

const msInDay = 1000 * 60 * 60 * 24;

const rangeDays = computed(() => {
  if (!startDate.value || !endDate.value) return 30;
  const start = new Date(startDate.value);
  const end = new Date(endDate.value);
  const diff = Math.max(1, Math.ceil(Math.abs(end - start) / msInDay));
  return diff;
});

const categoryOptions = computed(() => {
  const seen = new Set();
  return markupRules.value
    .map((rule) => rule.category)
    .filter(Boolean)
    .filter((value) => {
      if (seen.has(value)) return false;
      seen.add(value);
      return true;
    })
    .map((value) => ({ label: value, value }));
});

function formatPercent(value) {
  return fmtPercent(value, { whole: true, digits: 2 });
}

function marginSeverity(value) {
  if (value === undefined || value === null) return 'secondary';
  if (value >= 20) return 'success';
  if (value >= 10) return 'info';
  if (value >= 0) return 'warn';
  return 'danger';
}

async function loadProfitability() {
  profitabilityLoading.value = true;
  try {
    const days = rangeDays.value;
    const data = await api.get(`/api/costing/profitability?days=${days}`);
    profitabilityRows.value = Array.isArray(data) ? data : data?.items || data?.rows || [];
  } finally {
    profitabilityLoading.value = false;
  }
}

async function loadMarkupRules() {
  markupLoading.value = true;
  try {
    const data = await api.get('/api/costing/markup-rules');
    markupRules.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    markupLoading.value = false;
  }
}

function openRuleDialog(rule = null) {
  editingRule.value = rule;
  ruleForm.value = rule ? { ...rule } : emptyRule();
  showRuleDialog.value = true;
}

function closeRuleDialog() {
  showRuleDialog.value = false;
  editingRule.value = null;
  ruleForm.value = emptyRule();
}

async function saveRule() {
  if (!ruleForm.value.category?.trim()) return;
  savingRule.value = true;
  try {
    if (editingRule.value?.id) {
      await api.patch(`/api/costing/markup-rules/${editingRule.value.id}`, ruleForm.value, {
        successMessage: 'Markup rule updated',
      });
    } else {
      await api.post('/api/costing/markup-rules', ruleForm.value, {
        successMessage: 'Markup rule created',
      });
    }
    await loadMarkupRules();
    closeRuleDialog();
  } finally {
    savingRule.value = false;
  }
}

async function deleteRule(rule) {
  if (!rule?.id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: 'Delete this markup rule?' }))) return;
  try {
    await api.del(`/api/costing/markup-rules/${rule.id}`, { successMessage: 'Markup rule deleted' });
    await loadMarkupRules();
  } catch {
    // errors surfaced by useApiWithToast
  }
}

async function openJobDetail(row) {
  const id = row.job_id || row.id;
  if (!id) return;
  showJobDetail.value = true;
  jobLoading.value = true;
  jobDetail.value = null;
  try {
    jobDetail.value = await api.get(`/api/costing/jobs/${id}`);
  } finally {
    jobLoading.value = false;
  }
}

function closeJobDetail() {
  showJobDetail.value = false;
  jobDetail.value = null;
}

watch(currentJobId, (jobId) => {
  if (!jobId) {
    jobLineItems.value = [];
    return;
  }
  // No parts fetch: the Parts table renders from `jobDetail`, which the dialog
  // already loaded. The old second request went to /api/jobs/{id}/parts, which
  // no router serves — and its `|| []` fallback made a 404 look identical to
  // "this job has no parts".
  loadJobLineItems(jobId);
});


async function loadJobLineItems(jobId) {
  if (!jobId) {
    jobLineItems.value = [];
    return;
  }
  lineItemsLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${jobId}/line-items`);
    jobLineItems.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    lineItemsLoading.value = false;
  }
}








async function calculatePrice() {
  if (!calculatorCategory.value || calculatorCost.value === null || calculatorCost.value === undefined) return;
  calculatorLoading.value = true;
  try {
    calculatorResult.value = await api.post('/api/costing/calculate-price', {
      category: calculatorCategory.value,
      cost: calculatorCost.value,
    });
  } finally {
    calculatorLoading.value = false;
  }
}

watch([startDate, endDate], loadProfitability, { immediate: true });

onMounted(() => {
  loadMarkupRules();
});
</script>

<style scoped>
.job-cell { display: flex; flex-direction: column; line-height: 1.3; }
.job-title { font-weight: 600; }
.job-customer { color: var(--p-text-muted-color); font-size: 0.85em; }

.tab-panel {
  margin-top: 16px;
}
.job-detail-body {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.job-detail-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  flex-wrap: wrap;
}
.job-detail-subtitle {
  margin: 0;
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
}
.job-detail-min-margin {
  min-width: 160px;
}
.job-detail-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0.75rem;
}
.summary-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 0.75rem 1rem;
}
.summary-label {
  font-size: 0.75rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.summary-value {
  display: block;
  font-size: 1.35rem;
  margin: 0.25rem 0;
}
.min-margin-warning {
  color: var(--color-danger-500);
  font-weight: 600;
  border: 1px solid rgba(185, 28, 28, 0.2);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
/* Secondary text that explains where something lives. Muted but not faint —
   it is the only pointer to the parts detail, so it has to be readable, and it
   carries the theme token rather than a hardcoded grey so dark mode follows. */
.section-note {
  margin: 0.35rem 0 0;
  font-size: 0.85rem;
  line-height: 1.45;
  color: var(--p-text-muted-color, #6b7280);
}

.job-detail-section {
  border-top: 1px solid var(--p-content-border-color);
  padding-top: 1rem;
}
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}
.section-actions {
  display: flex;
  align-items: flex-end;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.filter-group {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 180px;
}
.job-detail-table :global(.p-datatable-thead > tr > th) {
  background: transparent;
  border-color: var(--p-content-border-color);
}
.tab-toolbar .date-range {
  display: flex;
  gap: 0.75rem;
}
.date-range label {
  display: flex;
  flex-direction: column;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  gap: 0.25rem;
}
.date-range input {
  border: 1px solid var(--p-content-border-color);
  border-radius: 4px;
  padding: 0.3rem 0.5rem;
}
.days-label {
  font-size: 0.95rem;
  color: var(--p-text-color);
}
.spinner-wrap.small {
  padding: 1rem 0;
}
.calculator-section header h3 {
  margin: 0;
}
.calculator-section header p {
  margin: 0.25rem 0 1rem;
  color: var(--p-text-muted-color);
}
.calculator-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.calculator-result {
  margin-top: 1rem;
  padding: 1rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  background: var(--p-content-hover-background);
}
.result-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.4rem;
}
.breakdown {
  margin-top: 0.75rem;
  border-top: 1px solid var(--p-content-border-color);
  padding-top: 0.75rem;
}
.breakdown-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.9rem;
  color: var(--p-text-muted-color);
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.5rem 1.5rem;
}
.detail-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.9rem;
}
</style>
