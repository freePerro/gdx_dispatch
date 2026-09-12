<template>
    <section class="view-card segments-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Customer Segments</h2>
        </template>
        <template #end>
          <Button
            label="+ Segment"
            icon="pi pi-plus"
            severity="primary"
            @click="openSegmentDialog()"
            data-testid="segments-open-dialog"
          />
        </template>
      </Toolbar>

      <Tabs
        v-model:value="activeTab"
        class="view-tabs"
        data-testid="segments-tabs"
      >
        <TabList>
          <Tab v-for="tab in tabDefinitions" :key="tab.key" :value="tab.key">
            {{ buildTabHeader(tab) }}
          </Tab>
        </TabList>
        <TabPanels>
          <TabPanel v-for="tab in tabDefinitions" :key="tab.key" :value="tab.key">
            <p class="tab-note">{{ tab.note }}</p>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredSegments"
        paginator
        :rows="15"
        striped-rows
        
        @row-click="onRowClick($event)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-users"
            title="No segments yet"
            message="Group customers by criteria to target campaigns and bulk actions."
            action-label="New Segment"
            @action="openSegmentDialog()"
          />
        </template>
        <Column field="name" header="Name" />
        <Column header="Criteria">
          <template #body="{ data }">{{ describeRules(data.rules) }}</template>
        </Column>
        <Column field="matching_customer_count" header="Customers" style="width: 110px">
          <template #body="{ data }">
            {{ data.matching_customer_count ?? '—' }}
          </template>
        </Column>
        <Column header="Type" style="width: 110px">
          <template #body="{ data }">
            <span :class="data.is_builtin ? 'type-badge builtin' : 'type-badge custom'">
              {{ data.is_builtin ? 'Built-in' : 'Custom' }}
            </span>
          </template>
        </Column>
        <Column field="created_at" header="Created" style="width: 120px">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width: 150px">
          <template #body="{ data }">
            <!-- Built-ins live in code, not the segments table: the API
                 answers 400 to an edit or delete of one, so offer neither. -->
            <span v-if="data.is_builtin" class="builtin-note">—</span>
            <template v-else>
              <Button
                icon="pi pi-pencil" aria-label="Edit"
                text
                size="small"
                label="Edit"
                @click.stop="openSegmentDialog(data)"
                data-testid="segments-edit-row"
              />
              <Button
                icon="pi pi-trash" aria-label="Delete"
                text
                size="small"
                severity="danger"
                :loading="deletingId === data.id"
                @click.stop="deleteSegment(data)"
                data-testid="segments-delete-row"
              />
            </template>
          </template>
        </Column>
      </DataTable>

      <div class="customer-segments-panel" data-testid="segments-customers-panel">
        <div class="customer-section-header">
          <div>
            <h3>Customers</h3>
            <p class="customer-section-note">Review and take bulk actions on segment audiences.</p>
          </div>
        </div>

        <div class="segment-chips" data-testid="segments-chip-row">
          <button
            v-for="chip in customerSegmentChips"
            :key="chip.key"
            type="button"
            class="segment-chip"
            :class="{ active: chip.key === activeCustomerChipKey }"
            @click="selectCustomerChip(chip)"
            :data-testid="`segment-chip-${chip.key}`"
          >
            <span>{{ chip.count === null ? chip.label : `${chip.label} (${chip.count})` }}</span>
          </button>
        </div>

        <div
          v-if="bulkToolbarVisible"
          class="bulk-toolbar"
          data-testid="segments-customer-bulk-toolbar"
        >
          <span class="bulk-sel-label" data-testid="segments-bulk-selected-label">
            {{ selectedCustomers.length }} selected
          </span>
          <Button
            plain
            size="small"
            label="Add Tag"
            icon="pi pi-tag"
            @click="openBulkTagDialog"
            data-testid="segments-bulk-add-tag"
          />
          <Button
            plain
            size="small"
            label="Export CSV"
            icon="pi pi-file"
            @click="exportSelectedCustomers"
            data-testid="segments-bulk-export"
          />
          <Button
            plain
            size="small"
            label="Deselect"
            icon="pi pi-times" aria-label="Remove"
            @click="clearCustomerSelection"
            data-testid="segments-bulk-deselect"
          />
        </div>

        <div v-if="customerLoading" class="spinner-wrap" data-testid="segments-customers-loading">
          <ProgressSpinner />
        </div>

        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="paginatedCustomers"
          selectionMode="multiple"
          dataKey="id"
          v-model:selection="selectedCustomers"
          striped-rows
          class="customers-table"
          data-testid="segments-customers-table"
          :sortField="customerSortField"
          :sortOrder="customerSortOrder"
          @sort="onCustomerSort"
        >
          <template #empty>
            <div class="empty-message">No customers match this segment.</div>
          </template>
          <Column field="name" header="Name" sortable>
            <template #body="{ data }">
              <router-link
                :to="`/customers/${data.id}`"
                class="customer-link"
                data-testid="segments-customer-link"
                @click.stop
              >
                {{ data.name || 'Untitled' }}
              </router-link>
            </template>
          </Column>
          <Column field="phone" header="Phone" />
          <Column field="email" header="Email" />
          <Column field="customer_type" header="Type">
            <template #body="{ data }">
              {{ data.customer_type || 'Residential' }}
            </template>
          </Column>
          <Column field="created_at" header="Created">
            <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
          </Column>
          <Column header="Actions">
            <template #body="{ data }">
              <Button
                label="View"
                text
                size="small"
                icon="pi pi-external-link"
                :data-testid="`segments-customer-view-${data.id}`"
                @click.stop="viewCustomer(data.id)"
              />
            </template>
          </Column>
        </DataTable>

        <div class="customer-pagination" data-testid="segments-customer-pagination">
          <Button
            label="Prev"
            icon="pi pi-angle-left"
            text
            size="small"
            :disabled="customerPage <= 1"
            @click="goCustomerPage(-1)"
          />
          <span>{{ customerPage }} / {{ customerPageCount }}</span>
          <Button
            label="Next"
            icon="pi pi-angle-right"
            iconPos="right"
            text
            size="small"
            :disabled="customerPage >= customerPageCount"
            @click="goCustomerPage(1)"
          />
        </div>
      </div>

      <Dialog
        v-model:visible="showDialog"
        :header="editingSegment ? `Edit ${editingSegment.name}` : 'New segment'"
        modal
        :style="{ width: 'min(680px, 94vw)' }"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label for="segment-name">Name</label>
            <InputText
              id="segment-name"
              v-model="segmentForm.name"
              placeholder="Segment name"
              class="w-full"
              data-testid="segments-dialog-name"
            />
          </div>
          <div class="form-field full-width">
            <label for="segment-match">Match</label>
            <Select
              id="segment-match"
              v-model="segmentForm.match"
              :options="MATCH_OPTIONS"
              option-label="label"
              option-value="value"
              class="w-full"
              data-testid="segments-dialog-match"
            />
          </div>

          <div class="form-field full-width">
            <label>Rules</label>
            <div
              v-for="(rule, index) in segmentForm.rules"
              :key="index"
              class="rule-row"
              :data-testid="`segments-rule-${index}`"
            >
              <Select
                v-model="rule.field"
                :options="FIELD_OPTIONS"
                option-label="label"
                option-value="value"
                class="rule-field"
                aria-label="Field"
                @change="onRuleFieldChange(rule)"
                :data-testid="`segments-rule-field-${index}`"
              />
              <Select
                v-model="rule.operator"
                :options="operatorsFor(rule.field)"
                option-label="label"
                option-value="value"
                class="rule-operator"
                aria-label="Operator"
                :data-testid="`segments-rule-operator-${index}`"
              />
              <InputText
                v-model="rule.value"
                :placeholder="valuePlaceholder(rule)"
                class="rule-value"
                aria-label="Value"
                :data-testid="`segments-rule-value-${index}`"
              />
              <Button
                icon="pi pi-times"
                text
                severity="danger"
                aria-label="Remove rule"
                :disabled="segmentForm.rules.length <= 1"
                @click="removeRule(index)"
                :data-testid="`segments-rule-remove-${index}`"
              />
            </div>
            <Button
              label="Add rule"
              icon="pi pi-plus"
              text
              size="small"
              @click="addRule"
              data-testid="segments-rule-add"
            />
            <p v-if="formInvalidReason" class="rule-hint invalid" data-testid="segments-dialog-invalid">
              {{ formInvalidReason }}
            </p>
            <p v-else class="rule-hint">Matches customers where {{ describeRules(formRulesPayload) }}.</p>
          </div>
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="closeSegmentDialog"
            data-testid="segments-dialog-cancel"
          />
          <Button
            label="Save"
            icon="pi pi-check"
            class="primary"
            :disabled="!!formInvalidReason"
            @click="saveSegment"
            :loading="savingSegment"
            data-testid="segments-dialog-save"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showBulkTagDialog"
        header="Add tag to selected customers"
        modal
        :style="{ width: '460px' }"
        data-testid="segments-bulk-tag-dialog"
      >
        <div class="form-field">
          <label>Tag</label>
          <InputText
            v-model="bulkTagValue"
            placeholder="Tag name"
            class="w-full"
            data-testid="segments-bulk-tag-input"
          />
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="showBulkTagDialog = false"
            data-testid="segments-bulk-tag-cancel"
          />
          <Button
            label="Save"
            icon="pi pi-check"
            class="primary"
            :loading="bulkTagging"
            @click="saveBulkTag"
            data-testid="segments-bulk-tag-save"
          />
        </template>
      </Dialog>

    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useToast } from 'primevue/usetoast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { useRouter } from 'vue-router';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';

const api = useApiWithToast();
// Bulk tag reports SERVER counts, so it raises its own toast instead of the
// composable's fixed successMessage — see saveBulkTag.
const toast = useToast();
const router = useRouter();
const { confirmAsync } = useDestructiveConfirm();

const segments = ref([]);
const loading = ref(true);
const loadError = ref(null);
const activeTab = ref('all');
const showDialog = ref(false);
const editingSegment = ref(null);
const savingSegment = ref(false);
const deletingId = ref(null);

// The rule vocabulary the API actually evaluates, mirroring _rule_match /
// _validate_rules in routers/segments.py. Anything outside it is refused on
// write, so the editor must not be able to build it.
const MATCH_OPTIONS = [
  { label: 'Match all rules', value: 'all' },
  { label: 'Match any rule', value: 'any' },
];

const DATE_OPERATORS = [
  { label: 'is older than', value: 'older_than' },
  { label: 'is within the last', value: 'within_last' },
];
const NUMBER_OPERATORS = [
  { label: 'is greater than', value: 'greater_than' },
  { label: 'is less than', value: 'less_than' },
  { label: 'equals', value: 'equals' },
];
// Only fields the API can evaluate reliably. `customer_type` was a
// candidate and was left out on purpose: its values are tenant data and the
// backend compares them exactly and case-sensitively, so a free text box
// would silently build a segment matching nobody.
const FIELD_OPTIONS = [
  { label: 'Last job date', value: 'last_job_date', kind: 'date' },
  { label: 'Customer created', value: 'created_at', kind: 'date' },
  { label: 'Lifetime value', value: 'lifetime_value', kind: 'number' },
];

const OPERATORS_BY_KIND = {
  date: DATE_OPERATORS,
  number: NUMBER_OPERATORS,
};

function newRule() {
  return { field: 'last_job_date', operator: 'older_than', value: '90' };
}

const segmentForm = ref({
  name: '',
  match: 'all',
  rules: [newRule()],
});

const CUSTOMER_PAGE_SIZE = 25;
// 2026-04-29: was 250, capping the embedded customers list at ~250 rows
// even on installs with 500+ records. Bumped to 1000 (server hard cap) to
// cover a mid-size shop's whole customer list in one fetch.
const CUSTOMER_FETCH_LIMIT = 1000;
const customers = ref([]);
const customerLoading = ref(false);
const customerPage = ref(1);
const selectedCustomers = ref([]);
const activeCustomerSegment = ref(null);
const activeCustomerChipKey = ref('all');
const allCustomersCount = ref(0);
const showBulkTagDialog = ref(false);
const bulkTagValue = ref('');
const bulkTagging = ref(false);

// The old tabs keyed on `updated_at` and `customer_count`. The segments
// table is `id, name, rules, created_at, deleted_at` — neither column has
// ever existed, so "Recently updated" and "Large audiences" always counted
// zero. These three split on fields the API actually returns.
const tabDefinitions = [
  { key: 'all', label: 'All segments', note: 'Every segment in the library.' },
  { key: 'builtin', label: 'Built-in', note: 'Shipped with the app. Not editable.' },
  { key: 'custom', label: 'Custom', note: 'Segments this shop created.' },
];

const tabMatchers = {
  all: () => true,
  builtin: (segment) => segment.is_builtin === true,
  custom: (segment) => segment.is_builtin !== true,
};

const currentTabKey = computed(() => activeTab.value || 'all');

const tabCounts = computed(() =>
  tabDefinitions.reduce((acc, tab) => {
    const matcher = tabMatchers[tab.key] || tabMatchers.all;
    acc[tab.key] = segments.value.filter(matcher).length;
    return acc;
  }, {})
);

const filteredSegments = computed(() => {
  // Built-ins first (they have no created_at), then newest custom first.
  const list = segments.value.slice().sort((a, b) => {
    if (a.is_builtin !== b.is_builtin) return a.is_builtin ? -1 : 1;
    return new Date(b.created_at || 0) - new Date(a.created_at || 0);
  });
  const matcher = tabMatchers[currentTabKey.value] || tabMatchers.all;
  return list.filter(matcher);
});

const customerPageCount = computed(() => {
  const total = customers.value.length;
  const pages = Math.ceil(total / CUSTOMER_PAGE_SIZE);
  return Math.max(1, pages);
});

// Sort state, owned externally so sort applies to the FULL customer list
// before pagination slices it. Same shape as BillingView's fix
// (2026-05-11): without this, PrimeVue's `sortable` only sorts the visible
// 25-row slice of `paginatedCustomers`.
const customerSortField = ref(null);
const customerSortOrder = ref(null);
function onCustomerSort(event) {
  customerSortField.value = event.sortField || null;
  customerSortOrder.value = event.sortOrder || null;
  customerPage.value = 1;
}

const sortedCustomers = computed(() => {
  if (!customerSortField.value) return customers.value;
  const field = customerSortField.value;
  const dir = customerSortOrder.value || 1;
  return [...customers.value].sort((a, b) => {
    const av = a?.[field];
    const bv = b?.[field];
    const an = av == null || av === "";
    const bn = bv == null || bv === "";
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return (av - bv) * dir;
    }
    return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" }) * dir;
  });
});

const paginatedCustomers = computed(() => {
  const start = (customerPage.value - 1) * CUSTOMER_PAGE_SIZE;
  return sortedCustomers.value.slice(start, start + CUSTOMER_PAGE_SIZE);
});

const bulkToolbarVisible = computed(() => selectedCustomers.value.length > 0);

const selectedCustomerIds = computed(() =>
  selectedCustomers.value.map((customer) => customer.id).filter(Boolean)
);

const customerSegmentChips = computed(() => {
  const baseCount = allCustomersCount.value || customers.value.length;
  const baseChip = {
    key: 'all',
    id: null,
    label: 'All customers',
    count: baseCount,
  };
  const segmentChips = segments.value.map((segment, index) => {
    const keySuffix = segment.id ?? segment.name ?? `segment-${index}`;
    const key = segment.id ? `segment-${segment.id}` : `segment-${keySuffix}`;
    // matching_customer_count is what the API returns; `count` and
    // `customer_count` never existed, so every chip used to read 0.
    const count =
      typeof segment.matching_customer_count === 'number'
        ? segment.matching_customer_count
        : null;
    return {
      key,
      id: segment.id ?? null,
      label: segment.label || segment.name || `Segment ${index + 1}`,
      count,
    };
  });
  return [baseChip, ...segmentChips];
});

function buildTabHeader(tab) {
  const count = tabCounts.value[tab.key] ?? 0;
  return count ? `${tab.label} (${count})` : tab.label;
}

function formatDate(value) {
  return value ? String(value).split('T')[0] : '—';
}

function fieldMeta(field) {
  return FIELD_OPTIONS.find((f) => f.value === field) || FIELD_OPTIONS[0];
}

function operatorsFor(field) {
  return OPERATORS_BY_KIND[fieldMeta(field).kind] || NUMBER_OPERATORS;
}

function onRuleFieldChange(rule) {
  // Keep the operator legal for the new field — the API refuses e.g.
  // older_than on lifetime_value.
  const allowed = operatorsFor(rule.field).map((o) => o.value);
  if (!allowed.includes(rule.operator)) rule.operator = allowed[0];
}

function valuePlaceholder(rule) {
  const kind = fieldMeta(rule.field).kind;
  if (kind === 'date') return 'days, e.g. 90';
  return 'amount, e.g. 5000';
}

function addRule() {
  segmentForm.value.rules.push(newRule());
}

function removeRule(index) {
  if (segmentForm.value.rules.length <= 1) return;
  segmentForm.value.rules.splice(index, 1);
}

/** Render a rules object — either shape the API accepts — as a sentence. */
function describeRules(rules) {
  if (!rules || typeof rules !== 'object') return '—';
  const list = Array.isArray(rules.rules) ? rules.rules : [rules];
  const parts = list
    .filter((r) => r && r.field)
    .map((r) => {
      const field = FIELD_OPTIONS.find((f) => f.value === r.field);
      const label = field?.label || r.field;
      const op = [...DATE_OPERATORS, ...NUMBER_OPERATORS].find(
        (o) => o.value === r.operator
      );
      const opLabel = op?.label || r.operator;
      const kind = field?.kind;
      const value = kind === 'date' ? `${String(r.value).replace(/\D/g, '')} days` : r.value;
      return `${label} ${opLabel} ${value}`;
    });
  if (!parts.length) return '—';
  const joiner = String(rules.match || 'all').toLowerCase() === 'any' ? ' or ' : ' and ';
  return parts.join(joiner);
}

/** Turn a stored rules object back into editor rows. */
function rulesToForm(rules) {
  const list = Array.isArray(rules?.rules) ? rules.rules : rules?.field ? [rules] : [];
  const rows = list
    .filter((r) => r && r.field)
    .map((r) => ({
      field: r.field,
      operator: r.operator,
      // Dates are stored as "180 days"; the editor edits the number.
      value:
        fieldMeta(r.field).kind === 'date'
          ? String(r.value ?? '').replace(/\D/g, '')
          : String(r.value ?? ''),
    }));
  return {
    match: String(rules?.match || 'all').toLowerCase() === 'any' ? 'any' : 'all',
    rules: rows.length ? rows : [newRule()],
  };
}

function selectCustomerChip(chip) {
  activeCustomerChipKey.value = chip.key;
  activeCustomerSegment.value = chip.id;
  loadCustomers(chip.id);
}

function goCustomerPage(delta) {
  const targetPage = Math.min(
    Math.max(1, customerPage.value + delta),
    customerPageCount.value
  );
  customerPage.value = targetPage;
}

async function loadCustomers(segmentId = null) {
  customerLoading.value = true;
  try {
    // Two dropped query params used to live here. A segment chip sent
    // /api/customers?segment_id=... — nothing serves that, so FastAPI
    // discarded it and the panel silently re-loaded every customer. And the
    // page size was sent as `page_size`; the route's parameter is `per_page`
    // (default 50), so the list was capped at 50 and `All customers` counted
    // 50. With real segment counts beside it that reads as a subset larger
    // than the whole.
    const endpoint = segmentId
      ? `/api/segments/${segmentId}/customers`
      : `/api/customers?per_page=${CUSTOMER_FETCH_LIMIT}`;
    const data = await api.get(endpoint);
    const list = Array.isArray(data) ? data : data?.items || data?.data || [];
    customers.value = list;
    if (!segmentId) {
      // `total` is the table count; list.length is only this page of it.
      allCustomersCount.value =
        typeof data?.total === 'number' ? data.total : list.length;
    }
    const available = new Set(list.map((customer) => customer.id));
    selectedCustomers.value = selectedCustomers.value.filter((customer) =>
      available.has(customer.id)
    );
    customerPage.value = 1;
  } finally {
    customerLoading.value = false;
  }
}

function openBulkTagDialog() {
  bulkTagValue.value = '';
  showBulkTagDialog.value = true;
}

async function saveBulkTag() {
  const ids = selectedCustomerIds.value;
  const tag = bulkTagValue.value.trim();
  if (!ids.length || !tag) return;
  bulkTagging.value = true;
  try {
    // Report what the server actually did, not what was asked for. The old
    // toast was a fixed string on a handler that wrote nothing, so "Tag
    // applied to selected customers" appeared whether or not anything landed.
    const res = await api.post('/api/customers/bulk-tag', { customer_ids: ids, tag });
    const tagged = res?.tagged ?? 0;
    const missing = res?.not_found?.length ?? 0;
    let detail = `Tag "${tag}" applied to ${tagged} customer${tagged === 1 ? '' : 's'}`;
    if (missing) detail += ` — ${missing} could not be found`;
    toast.add({
      severity: missing ? 'warn' : 'success',
      summary: missing ? 'Tagged with skips' : 'Tag applied',
      detail,
      life: 4000,
    });
    showBulkTagDialog.value = false;
    bulkTagValue.value = '';
    clearCustomerSelection();
  } finally {
    bulkTagging.value = false;
  }
}

function exportSelectedCustomers() {
  const ids = selectedCustomerIds.value;
  if (!ids.length) return;
  const params = new URLSearchParams();
  params.set('ids', ids.join(','));
  const link = document.createElement('a');
  link.href = `/api/customers/export?${params.toString()}`;
  link.target = '_blank';
  link.rel = 'noreferrer noopener';
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

function clearCustomerSelection() {
  selectedCustomers.value = [];
}

function viewCustomer(id) {
  if (!id) return;
  router.push(`/customers/${id}`);
}

async function loadSegments() {
  loading.value = true;
  try {
    const data = await api.get('/api/segments');
    segments.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    segments.value = [];
    loadError.value = err?.message || 'Unable to load segments';
  } finally {
    loading.value = false;
  }
}

function onRowClick(event) {
  const segment = event?.data;
  if (!segment || segment.is_builtin) return;
  openSegmentDialog(segment);
}

function openSegmentDialog(segment = null) {
  editingSegment.value = segment;
  const mapped = rulesToForm(segment?.rules);
  segmentForm.value = {
    name: segment?.name || '',
    match: mapped.match,
    rules: mapped.rules,
  };
  showDialog.value = true;
}

function closeSegmentDialog() {
  showDialog.value = false;
  editingSegment.value = null;
}

/**
 * The `rules` object the API stores and evaluates.
 *
 * This used to send `{name, criteria, tags}`: `criteria` and `tags` are not
 * columns on the segments table, and `rules` is required — so create was a
 * 422 and edit a 405 (the PATCH did not exist). Both now speak `rules`. #455
 */
const formRulesPayload = computed(() => ({
  match: segmentForm.value.match,
  rules: segmentForm.value.rules
    .filter((r) => r.field && r.operator)
    .map((r) => ({
      field: r.field,
      operator: r.operator,
      // Day-based operators are stored the way the built-ins store them.
      value:
        fieldMeta(r.field).kind === 'date'
          ? `${ruleNumber(r)} days`
          : ruleNumber(r),
    })),
}));

/** The rule's value as a number, or null when it isn't one. */
function ruleNumber(rule) {
  const raw = String(rule.value ?? '').replace(/[\s,$]/g, '');
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

/**
 * Why the form cannot be saved, or null.
 *
 * A blank or unparseable value is the dangerous case, not merely an
 * incomplete one: it used to become `0`, and `older_than 0 days` /
 * `lifetime_value > 0` both select EVERY customer. The API refuses these
 * too — this stops the user reaching that error at all.
 */
const formInvalidReason = computed(() => {
  if (!segmentForm.value.name.trim()) return 'Give the segment a name.';
  const rules = segmentForm.value.rules.filter((r) => r.field && r.operator);
  if (!rules.length) return 'Add at least one rule.';
  for (const rule of rules) {
    const n = ruleNumber(rule);
    if (n === null) return `Enter a number for ${fieldMeta(rule.field).label}.`;
    if (fieldMeta(rule.field).kind === 'date' && n < 1) {
      return `${fieldMeta(rule.field).label} needs a window of at least 1 day.`;
    }
    if (n < 0) return `${fieldMeta(rule.field).label} cannot be negative.`;
  }
  return null;
});

async function saveSegment() {
  if (formInvalidReason.value) return;
  savingSegment.value = true;
  try {
    const payload = {
      name: segmentForm.value.name.trim(),
      rules: formRulesPayload.value,
    };
    if (editingSegment.value?.id) {
      await api.patch(`/api/segments/${editingSegment.value.id}`, payload, { successMessage: 'Segment updated' });
    } else {
      await api.post('/api/segments', payload, { successMessage: 'Segment created' });
    }
    await loadSegments();
    closeSegmentDialog();
  } finally {
    savingSegment.value = false;
  }
}

async function deleteSegment(segment) {
  if (!segment?.id || segment.is_builtin) return;
  const ok = await confirmAsync({
    header: 'Delete segment',
    message: `Delete "${segment.name}"? Customers are not affected.`,
  });
  if (!ok) return;
  deletingId.value = segment.id;
  try {
    await api.del(`/api/segments/${segment.id}`, { successMessage: 'Segment deleted' });
    await loadSegments();
  } finally {
    deletingId.value = null;
  }
}

onMounted(() => {
  loadSegments();
  loadCustomers();
});
</script>

<style scoped>
.segments-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.view-tabs {
  --p-tabview-content-padding: 0;
}

.tab-note {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.75rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.full-width {
  grid-column: 1 / -1;
}

.rule-row {
  display: grid;
  /* Field and operator carry the long labels ("Last job date", "is within
     the last"); the value is a short number. Sized off the widest label
     rather than evenly, because at 520px/1.2fr both truncated to
     "Last job ..." and "is older t..." — visible only in a browser. */
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1.5fr) minmax(0, 1fr) auto;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.5rem;
}

/* One control per line once there is no room for four side by side —
   jsdom applies no media queries, so this is only ever proven in a browser. */
@media (max-width: 640px) {
  .rule-row {
    grid-template-columns: 1fr;
  }
}

.rule-hint {
  margin: 0.5rem 0 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.rule-hint.invalid {
  color: var(--p-red-500, #b91c1c);
}

.type-badge {
  display: inline-block;
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  border: 1px solid var(--border);
  color: var(--p-text-muted-color);
}

.type-badge.custom {
  border-color: var(--p-primary-color, var(--border));
  color: var(--p-primary-color, inherit);
}

.builtin-note {
  color: var(--p-text-muted-color);
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.clickable-row .p-datatable-tbody > tr {
  cursor: pointer;
}

.customer-segments-panel {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1rem;
  background: var(--card-bg);
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.customer-section-header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}

.customer-section-header h3 {
  margin: 0;
  font-size: 1.1rem;
}

.customer-section-note {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.segment-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.segment-chip {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.25rem 0.9rem;
  font-size: 0.85rem;
  background: transparent;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s, color 0.2s;
}

.segment-chip.active {
  background: var(--accent-b);
  border-color: var(--accent-b);
  color: #fff;
}

.bulk-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.75rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  background: var(--p-content-background);
}

.bulk-sel-label {
  font-weight: 600;
  color: var(--p-text-muted-color);
}

.customers-table {
  margin-top: 0.5rem;
}

.customer-link {
  color: var(--accent-b);
  font-weight: 600;
  text-decoration: none;
}

.customer-link:hover {
  text-decoration: underline;
}

.customer-pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.75rem;
  font-size: 0.9rem;
  color: var(--p-text-muted-color);
}

.primary {
  min-width: 90px;
}
</style>
