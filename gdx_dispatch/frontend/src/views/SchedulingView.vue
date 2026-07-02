<template>
    <section class="view-card scheduling-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Team Scheduling</h2>
          <p class="page-subtitle">Reassign and reschedule jobs across technicians</p>
        </template>
        <template #end>
          <Button
            label="+ Reschedule / Assign"
            icon="pi pi-calendar"
            severity="success"
            class="new-schedule"
            @click="openDialog()"
            data-testid="scheduling-open-dialog"
          />
        </template>
      </Toolbar>

      <div class="week-controls" data-testid="scheduling-week-nav">
        <Button
          v-tooltip="'Previous week'"
          aria-label="Previous week"
          icon="pi pi-angle-left"
          class="p-button-text"
          severity="secondary"
          size="small"
          data-testid="scheduling-prev-week"
          @click="prevWeek"
        />
        <span class="week-controls__label">{{ weekLabel }}</span>
        <Button
          v-tooltip="'Next week'"
          aria-label="Next week"
          icon="pi pi-angle-right"
          class="p-button-text"
          severity="secondary"
          size="small"
          data-testid="scheduling-next-week"
          @click="nextWeek"
        />
      </div>

      <div class="filter-row">
        <Select
          v-model="techFilter"
          :options="technicianOptions"
          option-label="label"
          option-value="value"
          placeholder="Filter by technician"
          class="w-full"
          show-clear
          data-testid="scheduling-tech-filter"
        />
        <DatePicker
          v-model="dateRange"
          selection-mode="range"
          date-format="yy-mm-dd"
          placeholder="Date range"
          show-icon
          class="w-full"
          data-testid="scheduling-date-filter"
        />
        <div class="toggle-field">
          <label class="toggle-label" for="unassigned-only">Unassigned only</label>
          <ToggleSwitch
            id="unassigned-only"
            v-model="unassignedOnly"
            on-label="Yes"
            off-label="No"
            class="toggle-control"
            data-testid="scheduling-unassigned-toggle"
          />
        </div>
      </div>

      <Tabs
        v-model:value="activeTab"
        class="view-tabs"
        data-testid="scheduling-tabs"
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
        :value="filteredEntries"
        paginator
        :rows="15"
        striped-rows
        
        @row-click="openDialog($event.data)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-calendar"
            title="No schedule entries"
            message="Nothing is scheduled for this week and filter — assign a job to get started."
            action-label="Reschedule / Assign"
            @action="openDialog()"
          />
        </template>
        <Column field="technician_name" header="Technician" />
        <Column field="date" header="Date">
          <template #body="{ data }">
            <span v-if="data.date">{{ formatDate(data.date) }}</span>
            <Tag v-else value="Needs date" severity="warn" />
          </template>
        </Column>
        <Column field="start" header="Start" />
        <Column field="end" header="End" />
        <Column field="job_id" header="Job ID" />
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Actions" style="width: 120px">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              label="Edit"
              @click.stop="openDialog(data)"
              data-testid="scheduling-edit-row"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="selectedEntry ? `Reschedule ${selectedEntry.job_id || ''}` : 'Assign Schedule'"
        modal
        :style="{ width: '520px' }"
        :closable="true"
        close-icon="pi pi-times" aria-label="Remove"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Technician</label>
            <Select
              v-model="form.technician_id"
              :options="technicianOptions"
              option-label="label"
              option-value="value"
              placeholder="Select technician"
              class="w-full"
              data-testid="scheduling-dialog-tech"
            />
          </div>
          <div class="form-field">
            <label>Date</label>
            <DatePicker
              v-model="form.date"
              date-format="yy-mm-dd"
              placeholder="Select date"
              class="w-full"
              data-testid="scheduling-dialog-date"
            />
          </div>
          <div class="form-field">
            <label>Start</label>
            <InputText
              v-model="form.start"
              placeholder="08:00"
              class="w-full"
              data-testid="scheduling-dialog-start"
            />
          </div>
          <div class="form-field">
            <label>End</label>
            <InputText
              v-model="form.end"
              placeholder="17:00"
              class="w-full"
              data-testid="scheduling-dialog-end"
            />
          </div>
          <div class="form-field">
            <label>Job ID</label>
            <InputText v-model="form.job_id" placeholder="Job ID" class="w-full" data-testid="scheduling-dialog-job" />
          </div>
          <div class="form-field">
            <label>Status</label>
        <Select
          v-model="form.status"
          :options="statusOptionItems"
          option-label="label"
          option-value="value"
          class="w-full"
          data-testid="scheduling-dialog-status"
        />
          </div>
          <div class="form-field toggle-field">
            <label class="toggle-label">Requires dispatch rule</label>
            <ToggleSwitch
              v-model="form.requires_dispatch"
              on-label="Yes"
              off-label="No"
              class="toggle-control"
              data-testid="scheduling-dialog-dispatch"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" data-testid="scheduling-dialog-cancel" />
          <Button
            label="Save"
            icon="pi pi-check"
            class="primary"
            @click="saveSchedule"
            :loading="saving"
            data-testid="scheduling-dialog-save"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate as formatShortDate } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();

const entries = ref([]);
const technicianOptions = ref([]);
const loading = ref(true);
const activeTab = ref('all');
const techFilter = ref(null);
const weekStart = ref(getWeekStart(new Date()));
const dateRange = ref([weekStart.value, addDays(weekStart.value, 6)]);
const syncingWeekRange = ref(false);
const unassignedOnly = ref(false);
const showDialog = ref(false);
const selectedEntry = ref(null);
const saving = ref(false);

const statusOptions = ['scheduled', 'pending', 'completed', 'cancelled'];
const statusOptionItems = computed(() =>
  statusOptions.map((status) => ({ label: statusLabel(status), value: status }))
);

const emptyForm = () => ({
  technician_id: null,
  date: null,
  start: '',
  end: '',
  job_id: '',
  status: 'scheduled',
  requires_dispatch: false,
});
const form = ref(emptyForm());

const tabDefinitions = [
  { key: 'all', label: 'All entries', note: 'Every scheduling entry filtered by your selections.' },
  { key: 'assigned', label: 'Assigned', note: 'Entries with technicians already assigned.' },
  { key: 'unassigned', label: 'Needs assignment', note: 'Unassigned visits that need action.' },
  { key: 'pending', label: 'Pending', note: 'Entries awaiting dispatch confirmation.' },
];

const tabFilters = {
  all: () => true,
  assigned: (entry) => Boolean(entry.technician_id || entry.technician),
  unassigned: (entry) => !entry.technician_id && !entry.technician,
  pending: (entry) => entry.status === 'pending',
};

const weekLabel = computed(() => {
  const start = weekStart.value;
  const end = addDays(start, 6);
  return `Week of ${formatMonthDay(start)}  –  ${formatMonthDay(end)}`;
});

const currentTabKey = computed(() => activeTab.value || 'all');

const tabCounts = computed(() =>
  tabDefinitions.reduce((acc, tab) => {
    const matcher = tabFilters[tab.key] || tabFilters.all;
    acc[tab.key] = entries.value.filter(matcher).length;
    return acc;
  }, {})
);

const filteredEntries = computed(() => {
  let list = entries.value.slice();

  if (techFilter.value) {
    list = list.filter((entry) => String(entry.technician_id || entry.technician?.id) === String(techFilter.value));
  }

  if (dateRange.value?.length) {
    const [start, end] = dateRange.value;
    if (start) {
      const startTime = new Date(start).setHours(0, 0, 0, 0);
      list = list.filter((entry) => {
        // Keep entries without a date so the user can see the
        // lifecycle=Scheduled job-with-no-date and assign one.
        // They were invisible until 2026-04-29 audit fix.
        if (!entry.date) return true;
        const entryTime = new Date(entry.date).getTime();
        if (end) {
          const endTime = new Date(end).setHours(23, 59, 59, 999);
          return entryTime >= startTime && entryTime <= endTime;
        }
        return entryTime >= startTime;
      });
    }
    if (end) {
      const endTime = new Date(end).setHours(23, 59, 59, 999);
      list = list.filter((entry) => {
        if (!entry.date) return true;
        const entryTime = new Date(entry.date).getTime();
        return entryTime <= endTime;
      });
    }
  }

  if (unassignedOnly.value) {
    list = list.filter((entry) => !entry.technician_id && !entry.technician);
  }

  const matcher = tabFilters[currentTabKey.value] || tabFilters.all;
  return list.filter(matcher);
});

watch(dateRange, (value) => {
  if (syncingWeekRange.value) {
    syncingWeekRange.value = false;
    return;
  }
  if (Array.isArray(value) && value[0]) {
    weekStart.value = getWeekStart(value[0]);
  }
});

function buildTabHeader(tab) {
  const count = tabCounts.value[tab.key] ?? 0;
  return count ? `${tab.label} (${count})` : tab.label;
}

function statusLabel(value) {
  return value ? value.replace('_', ' ') : '—';
}

function statusSeverity(value) {
  return {
    scheduled: 'success',
    pending: 'warning',
    completed: 'info',
    cancelled: 'danger',
  }[value] || 'secondary';
}

function formatDate(value) {
  return value ? value.split('T')[0] : '—';
}

function formatMonthDay(date) {
  if (!date) return '';
  return formatShortDate(date, { options: { month: 'short', day: 'numeric' } });
}

function addDays(date, days) {
  const result = new Date(date);
  result.setDate(result.getDate() + days);
  return result;
}

function getWeekStart(date) {
  const dt = new Date(date);
  const day = dt.getDay();
  const diff = day === 0 ? -6 : 1 - day;
  dt.setDate(dt.getDate() + diff);
  dt.setHours(0, 0, 0, 0);
  return dt;
}

function setWeekRange(startDate) {
  const start = getWeekStart(startDate);
  weekStart.value = start;
  const end = addDays(start, 6);
  syncingWeekRange.value = true;
  dateRange.value = [start, end];
}

function prevWeek() {
  setWeekRange(addDays(weekStart.value, -7));
}

function nextWeek() {
  setWeekRange(addDays(weekStart.value, 7));
}

async function loadSchedulingEntries() {
  loading.value = true;
  try {
    const data = await api.get('/api/scheduling');
    entries.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function loadTechnicians() {
  try {
    const data = await api.get('/api/technicians?page_size=200');
    const list = Array.isArray(data) ? data : data?.items || [];
    technicianOptions.value = list.map((tech) => ({
      value: tech.id,
      label: tech.name || tech.display_name || `${tech.first_name || ''} ${tech.last_name || ''}`.trim() || 'Technician',
    }));
  } catch {
    technicianOptions.value = [];
  }
}

function openDialog(entry = null) {
  selectedEntry.value = entry;
  if (entry) {
    form.value = {
      technician_id: entry.technician_id || entry.technician?.id || null,
      date: entry.date ? new Date(entry.date) : null,
      start: entry.start || '',
      end: entry.end || '',
      job_id: entry.job_id || entry.job?.id || '',
      status: entry.status || 'scheduled',
      requires_dispatch: Boolean(entry.requires_dispatch),
    };
  } else {
    form.value = emptyForm();
  }
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
}

async function saveSchedule() {
  if (!form.value.date) return;
  saving.value = true;
  try {
  const payload = {
    ...form.value,
    date: form.value.date ? form.value.date.toISOString().split('T')[0] : null,
  };
    if (selectedEntry.value?.id) {
      await api.patch(`/api/scheduling/${selectedEntry.value.id}`, payload);
    } else {
      await api.post('/api/scheduling', payload);
    }
    await loadSchedulingEntries();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadSchedulingEntries();
  loadTechnicians();
});
</script>

<style scoped>
.page-subtitle {
  margin: 0.25rem 0 0;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
}

.scheduling-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.week-controls {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.week-controls__label {
  font-weight: 600;
  color: var(--p-text-muted-color);
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
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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

.primary {
  min-width: 96px;
}
</style>
