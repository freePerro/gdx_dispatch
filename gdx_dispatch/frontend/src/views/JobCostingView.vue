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
                <i class="pi pi-chart-line" style="font-size:3rem; color:#64748b"></i>
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
                <i class="pi pi-sliders-h" style="font-size:3rem; color:#64748b"></i>
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
            <div class="job-detail-min-margin">
              <label for="minMargin">Min Margin %</label>
              <InputNumber
                id="minMargin"
                v-model.number="minMarginThreshold"
                suffix="%"
                mode="decimal"
                step="0.1"
                :min="0"
                :max="99"
                class="w-full"
                data-testid="min-margin-input"
              />
            </div>
          </header>

          <div class="job-detail-summary">
            <div class="summary-card" data-testid="your-cost-card">
              <span class="summary-label">Your Cost ($)</span>
              <strong class="summary-value">{{ formatCurrency(yourCost) }}</strong>
              <small>Parts + items</small>
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

          <div v-if="minMarginWarningText" class="min-margin-warning" data-testid="min-margin-warning">
            <span>{{ minMarginWarningText }}</span>
          </div>

          <section class="job-detail-section">
            <div class="section-head">
              <h4>Parts</h4>
              <div class="section-actions">
                <div class="filter-group">
                  <label>Filter by Category</label>
                  <Select
                    v-model="selectedCatalogCategory"
                    :options="catalogCategoryOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="All categories"
                    filter
                    showClear
                    class="w-full"
                    data-testid="parts-category-filter"
                  />
                </div>
                <Button
                  label="+ Add Part"
                  icon="pi pi-plus"
                  severity="primary"
                  data-testid="add-part-btn"
                  @click="showAddPartDialog = true"
                />
              </div>
            </div>
            <DataTable
      responsiveLayout="scroll"
              :value="jobParts"
              :loading="partsLoading"
              dataKey="id"
              striped-rows
              data-testid="parts-table"
              class="job-detail-table"
            >
              <Column header="Catalog Item" style="min-width:180px">
                <template #body="{ data: part }">
                  <Select
                    v-model="part.catalog_item_id"
                    :options="filteredCatalogOptions"
                    optionLabel="label"
                    optionValue="value"
                    filter
                    showClear
                    class="w-full"
                    placeholder="Select item"
                    :disabled="!filteredCatalogOptions.length"
                    @change="() => applyCatalogDefaults(part)"
                    :data-testid="`part-catalog-${part.id || part.catalog_item_id}`"
                  />
                </template>
              </Column>
              <Column header="Description" style="min-width:200px">
                <template #body="{ data: part }">
                  <InputText
                    v-model="part.description"
                    placeholder="Description"
                    class="w-full"
                    :data-testid="`part-description-${part.id || part.catalog_item_id}`"
                    @blur="persistPart(part)"
                  />
                </template>
              </Column>
              <Column header="Qty" style="width:110px">
                <template #body="{ data: part }">
                  <InputNumber
                    v-model.number="part.qty"
                    mode="decimal"
                    step="0.001"
                    :min="0"
                    class="w-full"
                    :data-testid="`part-qty-${part.id}`"
                    @blur="persistPart(part)"
                  />
                </template>
              </Column>
              <Column header="Unit Cost" style="width:140px">
                <template #body="{ data: part }">
                  <InputNumber
                    v-model.number="part.unit_cost"
                    mode="currency"
                    currency="USD"
                    locale="en-US"
                    :min="0"
                    class="w-full"
                    :data-testid="`part-unit-cost-${part.id}`"
                    @blur="persistPart(part)"
                  />
                </template>
              </Column>
              <Column header="Unit Price" style="width:140px">
                <template #body="{ data: part }">
                  <InputNumber
                    v-model.number="part.unit_price"
                    mode="currency"
                    currency="USD"
                    locale="en-US"
                    :min="0"
                    class="w-full"
                    :data-testid="`part-unit-price-${part.id}`"
                    @blur="persistPart(part)"
                  />
                </template>
              </Column>
              <Column header="Line Total" style="width:140px">
                <template #body="{ data: part }">
                  <span>{{ formatCurrency(partLineTotal(part)) }}</span>
                </template>
              </Column>
              <Column header="" style="width:100px">
                <template #body="{ data: part }">
                  <Button
                    v-tooltip="'Delete part'"
                    icon="pi pi-trash"
                    severity="danger"
                    text
                    size="small"
                    aria-label="Delete part"
                    :data-testid="`delete-part-${part.id}`"
                    @click="deletePart(part)"
                  />
                </template>
              </Column>
            </DataTable>
          </section>

          <section class="job-detail-section">
            <div class="section-head">
              <h4>Items</h4>
              <Button
                label="+ Add Item"
                icon="pi pi-plus"
                severity="primary"
                data-testid="add-item-btn"
                @click="showAddItemDialog = true"
              />
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
              <Column header="Name" style="min-width:170px">
                <template #body="{ data: item }">
                  <InputText
                    v-model="item.name"
                    placeholder="Name"
                    class="w-full"
                    :data-testid="`item-name-${item.id}`"
                  />
                </template>
              </Column>
              <Column header="Description" style="min-width:200px">
                <template #body="{ data: item }">
                  <InputText
                    v-model="item.description"
                    placeholder="Description"
                    class="w-full"
                    :data-testid="`item-description-${item.id}`"
                  />
                </template>
              </Column>
              <Column header="Qty" style="width:110px">
                <template #body="{ data: item }">
                  <InputNumber
                    v-model.number="item.qty"
                    mode="decimal"
                    step="0.001"
                    :min="0"
                    class="w-full"
                    :data-testid="`item-qty-${item.id}`"
                  />
                </template>
              </Column>
              <Column header="Unit Price" style="width:140px">
                <template #body="{ data: item }">
                  <InputNumber
                    v-model.number="item.unit_price"
                    mode="currency"
                    currency="USD"
                    locale="en-US"
                    :min="0"
                    class="w-full"
                    :data-testid="`item-unit-price-${item.id}`"
                  />
                </template>
              </Column>
              <Column header="Total" style="width:140px">
                <template #body="{ data: item }">
                  <span>{{ formatCurrency(itemLineTotal(item)) }}</span>
                </template>
              </Column>
            </DataTable>
          </section>
        </div>
        <div v-else class="spinner-wrap">
          <p class="muted">Unable to load job details.</p>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="closeJobDetail" />
          <Button
            label="Save Changes"
            data-testid="save-costing-btn"
            icon="pi pi-save"
            severity="primary"
            :loading="savingCosting"
            @click="saveCosting"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showAddPartDialog"
        header="Add Part"
        modal
        :style="{ width: '540px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Catalog Item</label>
            <Select
              v-model="newPartForm.catalog_item_id"
              :options="filteredCatalogOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select catalog item"
              class="w-full"
              data-testid="new-part-catalog"
              showClear
            />
          </div>
          <div class="form-field">
            <label>Description *</label>
            <InputText
              v-model="newPartForm.description"
              placeholder="Description"
              class="w-full"
              data-testid="new-part-description"
            />
          </div>
          <div class="form-field">
            <label>Qty</label>
            <InputNumber
              v-model.number="newPartForm.qty"
              mode="decimal"
              step="0.001"
              :min="0"
              class="w-full"
              data-testid="new-part-qty"
            />
          </div>
          <div class="form-field">
            <label>Unit Cost ($)</label>
            <InputNumber
              v-model.number="newPartForm.unit_cost"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0"
              class="w-full"
              data-testid="new-part-unit-cost"
            />
          </div>
          <div class="form-field">
            <label>Unit Price ($)</label>
            <InputNumber
              v-model.number="newPartForm.unit_price"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0"
              class="w-full"
              data-testid="new-part-unit-price"
            />
          </div>
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="showAddPartDialog = false"
          />
          <Button
            label="Add Part"
            icon="pi pi-check"
            data-testid="confirm-add-part"
            :loading="addingPart"
            @click="addPart"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showAddItemDialog"
        header="Add Item"
        modal
        :style="{ width: '540px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name *</label>
            <InputText
              v-model="newItemForm.name"
              placeholder="Name"
              class="w-full"
              data-testid="new-item-name"
            />
          </div>
          <div class="form-field">
            <label>Description</label>
            <InputText
              v-model="newItemForm.description"
              placeholder="Description"
              class="w-full"
              data-testid="new-item-description"
            />
          </div>
          <div class="form-field">
            <label>Qty</label>
            <InputNumber
              v-model.number="newItemForm.qty"
              mode="decimal"
              step="0.001"
              :min="0"
              class="w-full"
              data-testid="new-item-qty"
            />
          </div>
          <div class="form-field">
            <label>Unit Price ($)</label>
            <InputNumber
              v-model.number="newItemForm.unit_price"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0"
              class="w-full"
              data-testid="new-item-unit-price"
            />
          </div>
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="showAddItemDialog = false"
          />
          <Button
            label="Add Item"
            icon="pi pi-check"
            data-testid="confirm-add-item"
            :loading="addingItem"
            @click="addItem"
          />
        </template>
      </Dialog>

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

const defaultPartForm = () => ({
  description: '',
  catalog_item_id: null,
  qty: 1,
  unit_cost: 0,
  unit_price: 0,
});

const defaultItemForm = () => ({
  name: '',
  description: '',
  qty: 1,
  unit_price: 0,
});

const jobParts = ref([]);
const jobLineItems = ref([]);
const partsLoading = ref(false);
const lineItemsLoading = ref(false);
const catalogItems = ref([]);
const catalogLoading = ref(false);
const selectedCatalogCategory = ref('');
const minMarginThreshold = ref(15);
const showAddPartDialog = ref(false);
const showAddItemDialog = ref(false);
const addingPart = ref(false);
const addingItem = ref(false);
const savingCosting = ref(false);
const newPartForm = ref(defaultPartForm());
const newItemForm = ref(defaultItemForm());
const currentJobId = computed(() => jobDetail.value?.job_id || jobDetail.value?.id || null);

function toNumber(value) {
  return Number(value ?? 0);
}

function partLineTotal(part) {
  return toNumber(part?.qty) * toNumber(part?.unit_price);
}

function itemLineTotal(item) {
  return toNumber(item?.qty) * toNumber(item?.unit_price);
}

function computeMargin(cost, price) {
  const costNum = toNumber(cost);
  const priceNum = toNumber(price);
  if (priceNum <= 0) return -100;
  return ((priceNum - costNum) / priceNum) * 100;
}

const catalogById = computed(() =>
  new Map((catalogItems.value || []).map((item) => [item.id, item]))
);

const catalogCategoryOptions = computed(() => {
  const categories = new Set(
    (catalogItems.value || [])
      .map((item) => (item.category || '').trim())
      .filter(Boolean)
  );
  const base = [{ label: 'All categories', value: '' }];
  return base.concat(Array.from(categories).sort().map((category) => ({ label: category, value: category })));
});

const filteredCatalogOptions = computed(() => {
  const categoryFilter = (selectedCatalogCategory.value || '').trim().toLowerCase();
  return (catalogItems.value || [])
    .filter((item) => {
      if (!categoryFilter) return true;
      return (item.category || '').trim().toLowerCase() === categoryFilter;
    })
    .map((item) => ({
      label: item.part_name || item.name || item.sku || 'Catalog item',
      value: item.id,
    }));
});

const partsTotalCost = computed(() =>
  jobParts.value.reduce((sum, part) => sum + partLineTotal(part), 0)
);

const itemsTotalCost = computed(() =>
  jobLineItems.value.reduce((sum, item) => sum + itemLineTotal(item), 0)
);

const yourCost = computed(() => partsTotalCost.value + itemsTotalCost.value);

const linesWithLowMargin = computed(() => {
  const threshold = Number(minMarginThreshold.value || 0);
  if (threshold <= 0) return [];
  const rows = [];
  jobParts.value.forEach((part) => {
    const margin = computeMargin(part.unit_cost, part.unit_price);
    if (margin < threshold) {
      rows.push({ label: part.description || 'Part', margin });
    }
  });
  jobLineItems.value.forEach((item) => {
    const margin = computeMargin(item.unit_cost ?? 0, item.unit_price);
    if (margin < threshold) {
      rows.push({ label: item.name || item.description || 'Item', margin });
    }
  });
  return rows;
});

const minMarginWarningText = computed(() => {
  if (!linesWithLowMargin.value.length) return '';
  const labels = linesWithLowMargin.value.map((line) => line.label);
  const preview = labels.slice(0, 3).join(', ');
  const more = labels.length > 3 ? ` and ${labels.length - 3} more` : '';
  return `Margin below ${minMarginThreshold.value}% for ${preview}${more}.`;
});

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
  if (value >= 0) return 'warning';
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
    jobParts.value = [];
    jobLineItems.value = [];
    return;
  }
  loadJobParts(jobId);
  loadJobLineItems(jobId);
});

watch(showJobDetail, (visible) => {
  if (!visible) {
    showAddPartDialog.value = false;
    showAddItemDialog.value = false;
  }
});

async function loadJobParts(jobId) {
  if (!jobId) {
    jobParts.value = [];
    return;
  }
  partsLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${jobId}/parts`);
    jobParts.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    partsLoading.value = false;
  }
}

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

async function loadCatalogItems() {
  catalogLoading.value = true;
  try {
    const data = await api.get('/api/inventory/parts');
    catalogItems.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    catalogLoading.value = false;
  }
}

async function persistPart(part) {
  if (!currentJobId.value || !part?.id) return;
  try {
    const payload = {
      catalog_item_id: part.catalog_item_id || null,
      description: part.description,
      qty: part.qty,
      unit_cost: part.unit_cost,
      unit_price: part.unit_price,
    };
    const updated = await api.patch(`/api/jobs/${currentJobId.value}/parts/${part.id}`, payload);
    const idx = jobParts.value.findIndex((p) => p.id === updated.id);
    if (idx >= 0) {
      jobParts.value[idx] = { ...jobParts.value[idx], ...updated };
    }
  } catch {
    // errors surfaced by useApiWithToast
  }
}

async function deletePart(part) {
  if (!currentJobId.value || !part?.id) return;
  try {
    await api.del(`/api/jobs/${currentJobId.value}/parts/${part.id}`, {
      successMessage: 'Part removed',
    });
    jobParts.value = jobParts.value.filter((p) => p.id !== part.id);
  } catch {
    // handled by hook
  }
}

function applyCatalogDefaults(part) {
  if (!part?.catalog_item_id) return;
  const catalogItem = catalogById.value.get(part.catalog_item_id);
  if (!catalogItem) return;
  if (!part.description) {
    part.description = catalogItem.part_name || catalogItem.name || '';
  }
  if (!part.unit_cost) {
    part.unit_cost = catalogItem.unit_cost ?? 0;
  }
  if (!part.unit_price) {
    part.unit_price = catalogItem.unit_price ?? catalogItem.price ?? catalogItem.cost ?? 0;
  }
  persistPart(part);
}

async function addPart() {
  if (!currentJobId.value) return;
  const description = (newPartForm.value.description || '').trim();
  if (!description) return;
  addingPart.value = true;
  try {
    const payload = {
      description,
      catalog_item_id: newPartForm.value.catalog_item_id || null,
      qty: newPartForm.value.qty,
      unit_cost: newPartForm.value.unit_cost,
      unit_price: newPartForm.value.unit_price,
    };
    const created = await api.post(`/api/jobs/${currentJobId.value}/parts`, payload, {
      successMessage: 'Part added',
    });
    jobParts.value = [...jobParts.value, created];
    newPartForm.value = defaultPartForm();
    showAddPartDialog.value = false;
  } finally {
    addingPart.value = false;
  }
}

async function addItem() {
  if (!currentJobId.value) return;
  const name = (newItemForm.value.name || '').trim();
  if (!name) return;
  addingItem.value = true;
  try {
    const payload = {
      name,
      description: newItemForm.value.description,
      qty: newItemForm.value.qty,
      unit_price: newItemForm.value.unit_price,
    };
    const created = await api.post(`/api/jobs/${currentJobId.value}/line-items`, payload, {
      successMessage: 'Item added',
    });
    jobLineItems.value = [...jobLineItems.value, created];
    newItemForm.value = defaultItemForm();
    showAddItemDialog.value = false;
  } finally {
    addingItem.value = false;
  }
}

async function saveCosting() {
  if (!currentJobId.value) return;
  savingCosting.value = true;
  try {
    await api.patch(
      `/api/jobs/${currentJobId.value}/costing`,
      {
        min_margin_percent: Number(minMarginThreshold.value || 0),
        parts: jobParts.value.map((part) => ({
          id: part.id,
          catalog_item_id: part.catalog_item_id || null,
          description: part.description,
          qty: part.qty,
          unit_cost: part.unit_cost,
          unit_price: part.unit_price,
        })),
        line_items: jobLineItems.value.map((item) => ({
          id: item.id,
          name: item.name,
          description: item.description,
          qty: item.qty,
          unit_price: item.unit_price,
        })),
      },
      { successMessage: 'Job costing saved' }
    );
    if (currentJobId.value) {
      await loadJobParts(currentJobId.value);
      await loadJobLineItems(currentJobId.value);
    }
  } finally {
    savingCosting.value = false;
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
  loadCatalogItems();
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
  color: #475569;
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
  color: #64748b;
}
.summary-value {
  display: block;
  font-size: 1.35rem;
  margin: 0.25rem 0;
}
.min-margin-warning {
  color: #b91c1c;
  font-weight: 600;
  border: 1px solid rgba(185, 28, 28, 0.2);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
.job-detail-section {
  border-top: 1px solid #e5e7eb;
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
  border-color: #e5e7eb;
}
.tab-toolbar .date-range {
  display: flex;
  gap: 0.75rem;
}
.date-range label {
  display: flex;
  flex-direction: column;
  font-size: 0.85rem;
  color: #475569;
  gap: 0.25rem;
}
.date-range input {
  border: 1px solid #d1d5db;
  border-radius: 4px;
  padding: 0.3rem 0.5rem;
}
.days-label {
  font-size: 0.95rem;
  color: #334155;
}
.spinner-wrap.small {
  padding: 1rem 0;
}
.calculator-section header h3 {
  margin: 0;
}
.calculator-section header p {
  margin: 0.25rem 0 1rem;
  color: #475569;
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
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #f8fafc;
}
.result-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.4rem;
}
.breakdown {
  margin-top: 0.75rem;
  border-top: 1px solid #e5e7eb;
  padding-top: 0.75rem;
}
.breakdown-row {
  display: flex;
  justify-content: space-between;
  font-size: 0.9rem;
  color: #475569;
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
