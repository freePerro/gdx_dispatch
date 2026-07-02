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

      <div class="filter-row">
        <Select
          v-model="tagFilter"
          :options="tagOptions"
          option-label="label"
          option-value="value"
          placeholder="Filter by tag"
          class="w-full"
          show-clear
          data-testid="segments-tag-filter"
        />
        <DatePicker
          v-model="updatedRange"
          selection-mode="range"
          date-format="yy-mm-dd"
          placeholder="Updated range"
          show-icon
          class="w-full"
          data-testid="segments-date-filter"
        />
        <div class="toggle-field">
          <label class="toggle-label" for="recent-only">Recent updates</label>
          <ToggleSwitch
            id="recent-only"
            v-model="recentOnly"
            on-label="On"
            off-label="Off"
            class="toggle-control"
            data-testid="segments-recent-toggle"
          />
        </div>
      </div>

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
        
        @row-click="openSegmentDialog($event.data)"
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
        <Column field="criteria_summary" header="Criteria">
          <template #body="{ data }">
            {{ data.criteria_summary || truncateCriteria(data.criteria) }}
          </template>
        </Column>
        <Column field="customer_count" header="Customers" />
        <Column field="updated_at" header="Updated">
          <template #body="{ data }">{{ formatDate(data.updated_at) }}</template>
        </Column>
        <Column header="Tags">
          <template #body="{ data }">{{ data.tags?.join(', ') || '—' }}</template>
        </Column>
        <Column header="Actions" style="width: 120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              label="Edit"
              @click.stop="openSegmentDialog(data)"
              data-testid="segments-edit-row"
            />
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
            <span>{{ chip.label }} ({{ chip.count }})</span>
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
            label="Send SMS"
            icon="pi pi-mobile"
            @click="openBulkSmsDialog"
            data-testid="segments-bulk-send-sms"
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
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name</label>
            <InputText
              v-model="segmentForm.name"
              placeholder="Segment name"
              class="w-full"
              data-testid="segments-dialog-name"
            />
          </div>
          <div class="form-field full-width">
            <label>Criteria (JSON)</label>
            <Textarea
              v-model="segmentForm.criteria"
              rows="4"
              class="w-full"
              data-testid="segments-dialog-criteria"
            />
          </div>
          <div class="form-field full-width">
            <label>Tags</label>
            <Chips
              v-model="segmentForm.tags"
              class="w-full"
              placeholder="Add tags"
              data-testid="segments-dialog-tags"
            />
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

      <Dialog
        v-model:visible="showBulkSmsDialog"
        header="Send SMS to selected customers"
        modal
        :style="{ width: '520px' }"
        data-testid="segments-bulk-sms-dialog"
      >
        <div class="form-field full-width">
          <label>Message</label>
          <Textarea
            v-model="bulkSmsMessage"
            rows="4"
            class="w-full"
            placeholder="Type your message..."
            data-testid="segments-bulk-sms-input"
          />
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            @click="showBulkSmsDialog = false"
            data-testid="segments-bulk-sms-cancel"
          />
          <Button
            label="Send"
            icon="pi pi-send"
            class="primary"
            :loading="bulkSmsSending"
            @click="sendBulkSms"
            data-testid="segments-bulk-sms-send"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useRouter } from 'vue-router';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Chips from 'primevue/chips';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Textarea from 'primevue/textarea';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();
const router = useRouter();

const segments = ref([]);
const loading = ref(true);
const loadError = ref(null);
const activeTab = ref('all');
const tagFilter = ref(null);
const updatedRange = ref(null);
const recentOnly = ref(false);
const showDialog = ref(false);
const editingSegment = ref(null);
const savingSegment = ref(false);

const segmentForm = ref({
  name: '',
  criteria: '',
  tags: [],
});

const CUSTOMER_PAGE_SIZE = 25;
// 2026-04-29: was 250, capping the embedded customers list at ~250 rows
// even on tenants with 500+ records. Bumped to 1000 (server hard cap) to
// cover all GDX customers and similar mid-size tenants in one fetch.
const CUSTOMER_FETCH_LIMIT = 1000;
const customers = ref([]);
const customerLoading = ref(false);
const customerPage = ref(1);
const selectedCustomers = ref([]);
const activeCustomerSegment = ref(null);
const activeCustomerChipKey = ref('all');
const allCustomersCount = ref(0);
const showBulkTagDialog = ref(false);
const showBulkSmsDialog = ref(false);
const bulkTagValue = ref('');
const bulkSmsMessage = ref('');
const bulkTagging = ref(false);
const bulkSmsSending = ref(false);

const tabDefinitions = [
  { key: 'all', label: 'All segments', note: 'Every saved segment in the library.' },
  { key: 'recent', label: 'Recently updated', note: 'Touched in the last 14 days.' },
  { key: 'large', label: 'Large audiences', note: 'Segments with 100+ customers.' },
];

const tabMatchers = {
  all: () => true,
  recent: (segment) => isWithinDays(segment.updated_at, 14),
  large: (segment) => (segment.customer_count || 0) >= 100,
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
  let list = segments.value.slice().sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));

  if (tagFilter.value) {
    list = list.filter((segment) => segment.tags?.includes(tagFilter.value));
  }

  if (recentOnly.value) {
    list = list.filter((segment) => isWithinDays(segment.updated_at, 14));
  }

  if (updatedRange.value?.length) {
    const [start, end] = updatedRange.value;
    if (start) {
      const startTime = new Date(start).setHours(0, 0, 0, 0);
      list = list.filter((segment) => {
        if (!segment.updated_at) return false;
        const entryTime = new Date(segment.updated_at).getTime();
        if (end) {
          const endTime = new Date(end).setHours(23, 59, 59, 999);
          return entryTime >= startTime && entryTime <= endTime;
        }
        return entryTime >= startTime;
      });
    }
    if (end) {
      const endTime = new Date(end).setHours(23, 59, 59, 999);
      list = list.filter((segment) => {
        if (!segment.updated_at) return false;
        const entryTime = new Date(segment.updated_at).getTime();
        return entryTime <= endTime;
      });
    }
  }

  const matcher = tabMatchers[currentTabKey.value] || tabMatchers.all;
  return list.filter(matcher);
});

const tagOptions = computed(() => {
  const tags = new Set();
  segments.value.forEach((segment) => {
    (segment.tags || []).forEach((tag) => tags.add(tag));
  });
  return Array.from(tags).map((tag) => ({ label: tag, value: tag }));
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
    const count =
      typeof segment.count === 'number'
        ? segment.count
        : typeof segment.customer_count === 'number'
        ? segment.customer_count
        : 0;
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

function truncateCriteria(value) {
  if (!value) return '—';
  return value.length > 60 ? `${value.slice(0, 57)}…` : value;
}

function formatDate(value) {
  return value ? value.split('T')[0] : '—';
}

function isWithinDays(value, days) {
  if (!value) return false;
  const now = new Date();
  const target = new Date(value);
  const diffDays = (now.getTime() - target.getTime()) / (1000 * 60 * 60 * 24);
  return diffDays <= days;
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
    const params = new URLSearchParams();
    params.set('page_size', `${CUSTOMER_FETCH_LIMIT}`);
    if (segmentId) {
      params.set('segment_id', segmentId);
    }
    const query = params.toString();
    const endpoint = query ? `/api/customers?${query}` : '/api/customers';
    const data = await api.get(endpoint);
    const list = Array.isArray(data) ? data : data?.items || data?.data || [];
    customers.value = list;
    if (!segmentId) {
      allCustomersCount.value = list.length;
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
    await api.post(
      '/api/customers/bulk-tag',
      { customer_ids: ids, tag },
      { successMessage: 'Tag applied to selected customers' }
    );
    showBulkTagDialog.value = false;
    bulkTagValue.value = '';
    clearCustomerSelection();
  } finally {
    bulkTagging.value = false;
  }
}

function openBulkSmsDialog() {
  bulkSmsMessage.value = '';
  showBulkSmsDialog.value = true;
}

async function sendBulkSms() {
  const ids = selectedCustomerIds.value;
  const message = bulkSmsMessage.value.trim();
  if (!ids.length || !message) return;
  bulkSmsSending.value = true;
  try {
    await api.post(
      '/api/communications/bulk-sms',
      { customer_ids: ids, message },
      { successMessage: 'SMS queued to selected customers' }
    );
    showBulkSmsDialog.value = false;
    bulkSmsMessage.value = '';
    clearCustomerSelection();
  } finally {
    bulkSmsSending.value = false;
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

function openSegmentDialog(segment = null) {
  editingSegment.value = segment;
  segmentForm.value = {
    name: segment?.name || '',
    criteria: segment?.criteria || '',
    tags: segment?.tags ? [...segment.tags] : [],
  };
  showDialog.value = true;
}

function closeSegmentDialog() {
  showDialog.value = false;
  editingSegment.value = null;
}

async function saveSegment() {
  if (!segmentForm.value.name.trim()) return;
  savingSegment.value = true;
  try {
    const payload = {
      name: segmentForm.value.name.trim(),
      criteria: segmentForm.value.criteria,
      tags: segmentForm.value.tags || [],
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

.filter-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.75rem;
}

.toggle-field {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.toggle-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
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
