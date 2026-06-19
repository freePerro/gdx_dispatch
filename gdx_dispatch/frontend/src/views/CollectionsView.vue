<template>
    <section class="co-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Collections</h2>
        </template>
        <template #end>
          <Button
            data-testid="collections-refresh-btn"
            label="Refresh"
            icon="pi pi-sync"
            @click="loadCollections"
          />
        </template>
      </Toolbar>

      <div class="aging-summary" data-testid="collections-aging-summary">
        <Card
          v-for="bucket in agingBuckets"
          :key="bucket.key"
          class="aging-card"
          :data-testid="`aging-bucket-${bucket.key}`"
        >
          <template #title>{{ bucket.label }}</template>
          <template #content>
            <p class="aging-value">{{ formatCurrency(bucket.total) }}</p>
            <p class="aging-subtext">{{ bucket.description }}</p>
          </template>
        </Card>
      </div>

      <div class="collection-filters" data-testid="collections-date-range">
        <DatePicker
          v-model="agingFilterStart"
          dateFormat="yy-mm-dd"
          showIcon
          placeholder="Start date"
          class="filter-datepicker"
          data-testid="collections-start-date"
        />
        <DatePicker
          v-model="agingFilterEnd"
          dateFormat="yy-mm-dd"
          showIcon
          placeholder="End date"
          class="filter-datepicker"
          data-testid="collections-end-date"
        />
        <Button
          label="Export CSV"
          icon="pi pi-download"
          severity="secondary"
          size="small"
          data-testid="collections-export-btn"
          @click="exportCollectionsCsv"
        />
      </div>

      <div class="filter-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab"
          :label="`${tab} (${counts[tab] || 0})`"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          :data-testid="`collections-tab-${tab}`"
          @click="statusFilter = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filtered"
        paginator
        :rows="20"
        striped-rows
        :empty-message="emptyMessage"
        @row-click="openEdit($event.data)"
        
      >
        <Column header="Customer">
          <template #body="{ data }">{{ data.customer || data.customer_name || '—' }}</template>
        </Column>
        <Column field="invoice_id" header="Invoice #" />
        <Column header="Amount Due" style="width: 140px">
          <template #body="{ data }">{{ formatCurrency(data.amount_due) }}</template>
        </Column>
        <Column field="days_overdue" header="Days Overdue" style="width: 130px" />
        <Column field="last_contact" header="Last Contact" style="width: 130px">
          <template #body="{ data }">{{ formatDate(data.last_contact) }}</template>
        </Column>
        <Column field="status" header="Status" style="width: 140px">
          <template #body="{ data }">
            <Tag :value="normalizeStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Actions" style="width: 300px">
          <template #body="{ data }">
            <Button
              :data-testid="`collections-contacted-${data.id}`"
              label="Mark Contacted"
              text
              size="small"
              @click.stop="markContacted(data)"
            />
            <Button
              :data-testid="`collections-add-note-${data.id}`"
              label="Add Note"
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              @click.stop="openEdit(data)"
            />
            <Button
              :data-testid="`collections-pause-${data.id}`"
              label="Pause"
              text
              size="small"
              @click.stop="pauseEntry(data)"
            />
            <Button
              :data-testid="`collections-writeoff-${data.id}`"
              label="Write-off"
              severity="danger"
              text
              size="small"
              @click.stop="writeOff(data)"
            />
          </template>
        </Column>
      </DataTable>

      <div class="bulk-actions" style="margin-top: 1rem;">
        <Button
          label="Send Reminder to Selected"
          icon="pi pi-send"
          severity="info"
          data-testid="collections-send-reminder-selected"
          @click="sendReminderToSelected"
        />
      </div>

      <Dialog
        v-model:visible="showDialog"
        :header="editingEntry ? `Edit ${editingEntry.customer || 'Record'}` : 'Edit collection'"
        modal
        :style="{ width: '540px' }"
        @hide="closeDialog"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Status</label>
            <Select
              data-testid="collections-status-select"
              v-model="form.status"
              :options="statusTabs"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Last contact</label>
            <DatePicker
              data-testid="collections-last-contact"
              v-model="form.last_contact"
              class="w-full"
              :show-icon="true"
            />
          </div>
          <div class="form-field">
            <label>Contact type</label>
            <Select
              data-testid="collections-contact-type"
              v-model="form.contact_type"
              :options="['phone', 'email', 'sms', 'letter']"
              class="w-full"
              placeholder="Select contact type"
            />
          </div>
          <div class="form-field full-width">
            <label>Note</label>
            <Textarea
              data-testid="collections-note-input"
              v-model="form.note"
              rows="4"
              class="w-full"
            />
          </div>
        </div>
        <template #footer>
          <Button
            data-testid="collections-dialog-cancel"
            label="Cancel"
            severity="secondary"
            @click="showDialog = false"
          />
          <Button
            data-testid="collections-dialog-save"
            label="Save"
            icon="pi pi-check"
            :loading="saving"
            @click="saveEntry"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import { useToast } from 'primevue/usetoast';

const api = useApiWithToast();
const toast = useToast();
const collections = ref([]);
const loading = ref(true);
const statusFilter = ref('active');
const showDialog = ref(false);
const editingEntry = ref(null);
const saving = ref(false);
const agingFilterStart = ref(null);
const agingFilterEnd = ref(null);

const statusTabs = ['active', 'paused', 'resolved'];

const agingBucketConfig = [
  { key: '0-30', label: '0-30 days', description: 'Current or up to 30 days overdue', min: 0, max: 30 },
  { key: '31-60', label: '31-60 days', description: 'Moderately overdue', min: 31, max: 60 },
  { key: '61-90', label: '61-90 days', description: 'High overdue', min: 61, max: 90 },
  { key: '90plus', label: '90+ days', description: 'Critical overdue', min: 91, max: Number.POSITIVE_INFINITY },
];

const agingBuckets = computed(() => {
  const buckets = agingBucketConfig.map((bucket) => ({ ...bucket, total: 0 }));
  collections.value.forEach((entry) => {
    const days = toNum(entry.days_overdue ?? entry.daysPastDue ?? entry.days_past_due ?? 0);
    const amount = toNum(
      entry.amount_due ??
        entry.balance_due ??
        entry.due_amount ??
        entry.total ??
        entry.invoice_total ??
        0,
    );
    const match = buckets.find((range) => days >= range.min && days <= range.max);
    if (match) {
      match.total += amount;
    }
  });
  return buckets;
});

const emptyForm = () => ({
  status: statusTabs[0],
  note: '',
  last_contact: null,
  contact_type: 'phone',
});
const form = ref(emptyForm());

const counts = computed(() => {
  const result = {};
  statusTabs.forEach((tab) => (result[tab] = 0));
  collections.value.forEach((entry) => {
    const status = normalizeStatus(entry.status);
    if (result[status] !== undefined) {
      result[status] += 1;
    }
  });
  return result;
});

const filtered = computed(() => {
  let list = collections.value.filter((entry) => normalizeStatus(entry.status) === statusFilter.value);
  const start = agingFilterStart.value ? startOfDay(agingFilterStart.value) : null;
  const end = agingFilterEnd.value ? endOfDay(agingFilterEnd.value) : null;
  if (start || end) {
    list = list.filter((entry) => {
      const dueDate = parseDate(entry.due_date);
      if (!dueDate) return true;
      if (start && dueDate < start) return false;
      if (end && dueDate > end) return false;
      return true;
    });
  }
  return list;
});

function normalizeStatus(value) {
  return typeof value === 'string' ? value.toLowerCase() : 'active';
}

function statusSeverity(value) {
  const normalized = normalizeStatus(value);
  return {
    active: 'success',
    paused: 'warning',
    resolved: 'info',
  }[normalized] || 'secondary';
}

function formatCurrency(value) {
  if (typeof value !== 'number') return '—';
  return `$${value.toFixed(2)}`;
}

function toNum(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function parseDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

function startOfDay(date) {
  const result = new Date(date);
  result.setHours(0, 0, 0, 0);
  return result;
}

function endOfDay(date) {
  const result = new Date(date);
  result.setHours(23, 59, 59, 999);
  return result;
}

function isoDate(value) {
  const parsed = parseDate(value);
  return parsed ? parsed.toISOString().split('T')[0] : null;
}

function exportCollectionsCsv() {
  const params = new URLSearchParams();
  const start = isoDate(agingFilterStart.value);
  const end = isoDate(agingFilterEnd.value);
  if (start) params.set('start_date', start);
  if (end) params.set('end_date', end);
  const query = params.toString();
  const url = `/api/collections/export${query ? `?${query}` : ''}`;
  if (typeof window !== 'undefined') {
    window.open(url, '_blank');
    toast.add({
      severity: 'info',
      summary: 'Export started',
      detail: 'Your CSV will download shortly.',
      life: 3000,
    });
  }
}

function formatDate(value) {
  if (!value) return '—';
  return value.split('T')[0];
}

async function loadCollections() {
  loading.value = true;
  try {
    const data = await api.get('/api/collections');
    collections.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openEdit(entry) {
  editingEntry.value = entry;
  form.value = {
    status: normalizeStatus(entry.status),
    note: entry.note || '',
    last_contact: entry.last_contact ? new Date(entry.last_contact) : null,
  };
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
  editingEntry.value = null;
  form.value = emptyForm();
}

function payloadFromForm(overrides = {}) {
  const lastContact = form.value.last_contact
    ? form.value.last_contact.toISOString().split('T')[0]
    : null;
  return {
    status: form.value.status,
    note: form.value.note,
    last_contact: lastContact,
    ...overrides,
  };
}

async function saveEntry() {
  if (!editingEntry.value) return;
  saving.value = true;
  try {
    await api.patch(`/api/collections/${editingEntry.value.id}`, payloadFromForm(), {
      successMessage: 'Collection updated',
    });
    await loadCollections();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

async function markContacted(entry) {
  await api.patch(`/api/collections/${entry.id}`, {
    last_contact: new Date().toISOString().split('T')[0],
    status: 'active',
  }, { successMessage: 'Marked as contacted' });
  await loadCollections();
}

async function pauseEntry(entry) {
  await api.patch(`/api/collections/${entry.id}`, { status: 'paused' }, { successMessage: 'Paused queue' });
  await loadCollections();
}

async function writeOff(entry) {
  await api.patch(`/api/collections/${entry.id}`, { status: 'resolved' }, { successMessage: 'Marked as resolved' });
  await loadCollections();
}

async function sendReminderToSelected() {
  try {
    await api.post('/api/collections/send-reminders', { status_filter: statusFilter.value }, {
      successMessage: 'Reminders queued for delivery',
    });
  } catch (err) {
    // toast handled by useApiWithToast
  }
}

const emptyMessage = 'No collections items';

onMounted(loadCollections);
</script>

<style scoped>
.aging-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}
.aging-card {
  border: 1px solid var(--surface-border, #d0d7de);
  border-radius: 0.75rem;
  padding: 0.85rem;
}
.aging-value {
  margin: 0.4rem 0 0;
  font-size: 1.5rem;
  font-weight: 700;
}
.aging-subtext {
  margin: 0;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.collection-filters {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  align-items: flex-end;
  margin-bottom: 1rem;
}
.filter-datepicker {
  min-width: 170px;
}
</style>
