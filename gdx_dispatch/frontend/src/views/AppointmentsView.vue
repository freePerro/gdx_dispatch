<template>
    <section class="appointments-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Appointments</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <DatePicker
              v-model="dateRange"
              selection-mode="range"
              date-format="mm/dd/yy"
              placeholder="Filter by date range"
              show-icon
              class="range-picker"
            />
            <Button
              v-if="hasRange"
              v-tooltip="'Clear range'"
              icon="pi pi-times" aria-label="Remove"
              size="small"
              severity="secondary"
              class="range-clear"
              @click="clearDateRange"
            />
            <Button label="+ New Appointment" icon="pi pi-plus" @click="openCreate" />
          </div>
        </template>
      </Toolbar>

      <div v-if="unconfirmedCount > 0" class="unconfirmed-banner">
        <div>
          <strong>{{ unconfirmedCount }} unconfirmed appointment{{ unconfirmedCount === 1 ? '' : 's' }} in the next 48 hours</strong>
          <p>Prompt your team to confirm visits so customers know when to expect a technician.</p>
        </div>
        <Button
          icon="pi pi-sync"
          label="Refresh"
          text
          size="small"
          @click="fetchUnconfirmed"
        />
      </div>

      <Tabs v-model:value="statusFilter" class="status-tabs">
        <TabList>
          <Tab v-for="tab in statusTabs" :key="tab.key" :value="tab.key">
            <div class="tab-label">
              <span>{{ tab.label }}</span>
              <Badge v-if="tab.count > 0" :value="tab.count" severity="info" />
            </div>
          </Tab>
        </TabList>
      </Tabs>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows appointments-table"
      responsiveLayout="scroll"
        v-else
        :value="appointments"
        data-key="id"
        paginator
        :rows="20"
        striped-rows
        responsive-layout="scroll"
        @row-click="openEdit"
        
      >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-calendar" aria-hidden="true"></i>
            <h3>No appointments found</h3>
            <p>Schedule a visit to keep jobs moving and customers in the loop.</p>
            <Button label="+ Schedule First" icon="pi pi-plus" @click="openCreate" />
          </div>
        </template>

        <Column field="title" header="Title" />
        <Column header="Tech">
          <template #body="{ data }">{{ techLabel(data) }}</template>
        </Column>
        <Column field="start_at" header="Start">
          <template #body="{ data }">{{ formatDateTime(data.start_at) }}</template>
        </Column>
        <Column field="end_at" header="End">
          <template #body="{ data }">{{ formatDateTime(data.end_at) }}</template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Badge :value="formatStatusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="address" header="Address" />
        <Column header="Actions" style="width:220px">
          <template #body="{ data }">
            <div class="row-actions">
              <Button
                v-if="stateTransitions[data.status]"
                :label="stateTransitions[data.status].label"
                size="small"
                :loading="actionLoadingId === data.id"
                :disabled="actionLoadingId === data.id"
                severity="success"
                @click.stop="triggerTransition(data, stateTransitions[data.status])"
              />
              <Button
                v-if="data.status !== 'completed' && data.status !== 'cancelled'"
                v-tooltip="'Cancel'"
                icon="pi pi-ban"
                severity="danger"
                text
                size="small"
                :loading="actionLoadingId === data.id"
                :disabled="actionLoadingId === data.id"
                @click.stop="cancelAppointment(data)"
              />
            </div>
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" :header="dialogTitle" modal :style="{ width: '520px' }">
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Title *</label>
            <InputText v-model="form.title" class="w-full" />
          </div>
          <div class="form-field">
            <label>Linked Job</label>
            <Select
              v-model="form.job_id"
              :options="jobOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select job"
              filter
              showClear
              class="w-full"
              @change="onJobSelect"
            />
          </div>
          <div class="form-field">
            <label>Customer</label>
            <Select
              v-model="form.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select customer"
              filter
              showClear
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Technician</label>
            <Select
              v-model="form.tech_id"
              :options="techOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select tech"
              filter
              showClear
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Address</label>
            <InputText v-model="form.address" class="w-full" />
          </div>
          <div class="form-field">
            <label>Start</label>
            <DatePicker
              v-model="form.start_at"
              show-time
              hour-format="12"
              date-format="mm/dd/yy"
              show-icon
              time-only="false"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>End</label>
            <DatePicker
              v-model="form.end_at"
              show-time
              hour-format="12"
              date-format="mm/dd/yy"
              show-icon
              time-only="false"
              class="w-full"
            />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea v-model="form.notes" rows="3" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button
            :label="editingAppointment ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveAppointment"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();

const appointments = ref([]);
const loading = ref(false);
const unconfirmedCount = ref(0);
const showDialog = ref(false);
const saving = ref(false);
const actionLoadingId = ref(null);
const editingAppointment = ref(null);
const statusFilter = ref('all');
const dateRange = ref([]);

const customers = ref([]);
const jobs = ref([]);
const technicians = ref([]);

const form = ref({
  title: '',
  tech_id: null,
  customer_id: null,
  job_id: null,
  address: '',
  start_at: null,
  end_at: null,
  notes: '',
});

const hasRange = computed(() => dateRange.value && dateRange.value.length > 0);

const jobOptions = computed(() =>
  jobs.value.map((job) => ({
    value: job.id,
    label: job.job_number ? `${job.job_number} — ${job.title || 'Job'}` : job.title || `Job ${job.id}`,
  }))
);

const customerOptions = computed(() =>
  customers.value.map((customer) => ({
    value: customer.id,
    label: customer.name || `${customer.first_name || ''} ${customer.last_name || ''}`.trim() || 'Customer',
  }))
);

const techOptions = computed(() =>
  technicians.value.map((tech) => ({
    value: tech.id,
    label:
      tech.name || tech.display_name || `${tech.first_name || ''} ${tech.last_name || ''}`.trim() || 'Tech',
  }))
);

const techMap = computed(() =>
  Object.fromEntries(
    technicians.value.map((tech) => [
      String(tech.id),
      tech.name || tech.display_name || `${tech.first_name || ''} ${tech.last_name || ''}`.trim() || 'Tech',
    ])
  )
);

const normalizedStatusList = ['scheduled', 'confirmed', 'en_route', 'arrived', 'completed', 'cancelled'];

const statusDefinitions = [
  { key: 'all', label: 'All' },
  { key: 'scheduled', label: 'Scheduled' },
  { key: 'confirmed', label: 'Confirmed' },
  { key: 'en_route', label: 'En Route' },
  { key: 'arrived', label: 'Arrived' },
  { key: 'completed', label: 'Completed' },
  { key: 'cancelled', label: 'Cancelled' },
];

const statusCounts = computed(() => {
  const counts = { all: appointments.value.length };
  normalizedStatusList.forEach((key) => {
    counts[key] = appointments.value.filter((appointment) => appointment.status === key).length;
  });
  return counts;
});

const statusTabs = computed(() =>
  statusDefinitions.map((status) => ({
    ...status,
    count: statusCounts.value[status.key] ?? 0,
  }))
);

const stateTransitions = {
  scheduled: { endpoint: 'confirm', label: 'Confirm', message: 'Appointment confirmed' },
  confirmed: { endpoint: 'on-my-way', label: 'On My Way', message: 'Tech is en route' },
  en_route: { endpoint: 'arrived', label: 'Arrived', message: 'Technician arrived' },
  arrived: { endpoint: 'complete', label: 'Complete', message: 'Appointment completed' },
};

const rangeKey = computed(() => {
  const [start, end] = dateRange.value || [];
  return `${start?.toISOString() || ''}|${end?.toISOString() || ''}`;
});

const dialogTitle = computed(() => (editingAppointment.value ? 'Edit Appointment' : 'New Appointment'));

function formatStatusLabel(status) {
  if (!status) return '—';
  return status
    .split('_')
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(' ');
}

function statusSeverity(status) {
  const severityMap = {
    scheduled: 'warning',
    confirmed: 'info',
    en_route: 'info',
    arrived: 'success',
    completed: 'success',
    cancelled: 'danger',
  };
  return severityMap[status] || 'secondary';
}

function formatDateTime(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return value;
  }
}

function techLabel(appointment) {
  if (appointment?.tech?.name) return appointment.tech.name;
  const mapped = techMap.value[String(appointment.tech_id)];
  return mapped || 'Unassigned';
}

function onJobSelect() {
  const job = jobs.value.find((item) => item.id === form.value.job_id);
  if (job) {
    if (job.customer_id) {
      form.value.customer_id = job.customer_id;
    }
    if (job.address && !form.value.address) {
      form.value.address = job.address;
    }
  }
}

function openCreate() {
  editingAppointment.value = null;
  form.value = {
    title: '',
    tech_id: null,
    customer_id: null,
    job_id: null,
    address: '',
    start_at: null,
    end_at: null,
    notes: '',
  };
  showDialog.value = true;
}

function openEdit(eventOrAppt) {
  // PrimeVue DataTable's @row-click hands us a DataTableRowClickEvent
  // ({ originalEvent, data, index }), not the row itself. Direct callers
  // pass the row. Accept both shapes; reading `.data` off a plain
  // appointment row is undefined → fall through.
  const appointment = eventOrAppt?.data ?? eventOrAppt;
  if (!appointment) return;
  editingAppointment.value = appointment;
  form.value = {
    title: appointment.title || '',
    tech_id: appointment.tech_id ?? null,
    customer_id: appointment.customer_id ?? null,
    job_id: appointment.job_id ?? null,
    address: appointment.address || '',
    start_at: appointment.start_at ? new Date(appointment.start_at) : null,
    end_at: appointment.end_at ? new Date(appointment.end_at) : null,
    notes: appointment.notes || '',
  };
  showDialog.value = true;
}

function clearDateRange() {
  dateRange.value = [];
}

async function fetchAppointments() {
  loading.value = true;
  try {
    const params = new URLSearchParams();
    if (statusFilter.value && statusFilter.value !== 'all') {
      params.append('status', statusFilter.value);
    }
    const [start, end] = dateRange.value || [];
    if (start) {
      params.append('start', start.toISOString());
    }
    if (end) {
      params.append('end', end.toISOString());
    }
    params.append('limit', '200');

    const query = params.toString();
    const url = query ? `/api/appointments?${query}` : '/api/appointments';
    const payload = await api.get(url);
    const rawList = Array.isArray(payload)
      ? payload
      : payload?.items || payload?.data || [];

    appointments.value = rawList.map((item) => ({
      ...item,
      status: (item.status || '').toLowerCase(),
    }));
  } catch {
    // Errors surfaced via useApiWithToast
  } finally {
    loading.value = false;
  }
}

async function fetchSupportingData() {
  try {
    const [jobsResult, customersResult, techniciansResult] = await Promise.all([
      api.get('/api/jobs?page_size=200').catch(() => []),
      api.get('/api/customers?per_page=500').catch(() => []),
      api.get('/api/technicians').catch(() => []),
    ]);

    jobs.value = Array.isArray(jobsResult) ? jobsResult : jobsResult?.items || jobsResult?.data || [];
    customers.value = Array.isArray(customersResult)
      ? customersResult
      : customersResult?.items || customersResult?.data || [];
    technicians.value = Array.isArray(techniciansResult)
      ? techniciansResult
      : techniciansResult?.items || techniciansResult?.data || [];
  } catch (error) {
    // Errors already surfaced via useApiWithToast
  }
}

async function fetchUnconfirmed() {
  try {
    const data = await api.get('/api/appointments/unconfirmed?hours=48');
    if (Array.isArray(data)) {
      unconfirmedCount.value = data.length;
    } else if (typeof data?.count === 'number') {
      unconfirmedCount.value = data.count;
    } else if (Array.isArray(data?.items)) {
      unconfirmedCount.value = data.items.length;
    } else {
      unconfirmedCount.value = Number(data?.total || 0) || 0;
    }
  } catch {
    unconfirmedCount.value = 0;
  }
}

async function saveAppointment() {
  if (!form.value.title?.trim()) {
    return;
  }
  saving.value = true;
  try {
    const payload = {
      title: form.value.title.trim(),
      tech_id: form.value.tech_id || null,
      customer_id: form.value.customer_id || null,
      job_id: form.value.job_id || null,
      address: form.value.address || '',
      start_at: form.value.start_at ? form.value.start_at.toISOString() : null,
      end_at: form.value.end_at ? form.value.end_at.toISOString() : null,
      notes: form.value.notes || '',
    };

    if (editingAppointment.value) {
      await api.patch(`/api/appointments/${editingAppointment.value.id}`, payload, {
        successMessage: 'Appointment updated',
      });
    } else {
      await api.post('/api/appointments', payload, { successMessage: 'Appointment created' });
    }

    showDialog.value = false;
    await fetchAppointments();
    await fetchUnconfirmed();
  } catch {
    // Error details already handled above
  } finally {
    saving.value = false;
  }
}

async function triggerTransition(appointment, config) {
  if (!appointment?.id) return;
  actionLoadingId.value = appointment.id;
  try {
    await api.post(`/api/appointments/${appointment.id}/${config.endpoint}`, config.payload || null, {
      successMessage: config.message,
    });
    await fetchAppointments();
    await fetchUnconfirmed();
  } catch {
    // error toast rendered inside useApiWithToast
  } finally {
    actionLoadingId.value = null;
  }
}

async function cancelAppointment(appointment) {
  if (!appointment?.id) return;
  const reason = window.prompt('Reason for cancellation', 'Customer request');
  if (!reason?.trim()) return;
  await triggerTransition(appointment, {
    endpoint: 'cancel',
    message: 'Appointment cancelled',
    payload: { reason: reason.trim() },
  });
}

watch([statusFilter, rangeKey], () => {
  fetchAppointments();
}, { immediate: true });

onMounted(() => {
  fetchSupportingData();
  fetchUnconfirmed();
});
</script>

<style scoped>
.appointments-view {
  max-width: 1300px;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.range-picker {
  min-width: 220px;
}

.range-clear {
  margin-top: 0.25rem;
}

.unconfirmed-banner {
  margin: 1rem 0;
  padding: 1rem;
  border-radius: 8px;
  background: #fff9e6;
  border: 1px solid #ffe29a;
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}

.unconfirmed-banner p {
  margin: 0.35rem 0 0;
  color: #4b3b1b;
}

.status-tabs {
  margin: 1rem 0;
}

.tab-label {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.appointments-table {
  cursor: pointer;
}

.row-actions {
  display: flex;
  gap: 0.25rem;
  justify-content: flex-end;
}

.empty-state {
  text-align: center;
  padding: 2rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  align-items: center;
  color: #475569;
}

.empty-state i {
  font-size: 3rem;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.form-field label {
  font-weight: 600;
  margin-bottom: 0.25rem;
  display: block;
}

.form-field.full-width {
  grid-column: 1 / -1;
}
</style>
