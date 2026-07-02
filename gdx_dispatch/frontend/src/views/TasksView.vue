<template>
    <section class="tasks-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Tasks</h2>
        </template>
        <template #end>
          <Button icon="pi pi-download" label="Export" aria-label="Export CSV" text size="small" @click="exportRows" />
          <Button label="+ New Task" icon="pi pi-plus" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="tab in filterTabs"
          :key="tab"
          :label="tabLabelWithCount(tab)"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          @click="statusFilter = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-if="!loading"
        :value="filteredTasks"
        paginator
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50, 100]"
        striped-rows
        responsive-layout="scroll"
        @row-click="openEdit($event.data)"

      >
        <template #empty>
          <EmptyState icon="pi pi-tasks" title="No tasks yet" message="Capture task work so you can track status, owners, and due dates." actionLabel="+ Create First Task" @action="openCreate" />
        </template>
        <Column field="title" header="Title" sortable />
        <Column field="priority" header="Priority" sortable style="width:140px">
          <template #body="{ data }">
            <Tag :value="priorityLabel(data.priority)" :severity="prioritySeverity(data.priority)" />
          </template>
        </Column>
        <Column field="status" header="Status" sortable style="width:150px">
          <template #body="{ data }">
            <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="assigned_to" header="Assigned To" sortable style="width:150px" />
        <Column field="due_date" header="Due Date" sortable style="width:130px">
          <template #body="{ data }">{{ formatDate(data.due_date) }}</template>
        </Column>
        <Column field="related_job_id" header="Job" sortable style="width:100px" />
        <Column field="related_customer_id" header="Customer" sortable style="width:120px" />
        <Column header="Actions" style="width:180px">
          <template #body="{ data }">
            <Button
              v-if="data.status !== 'completed'"
              icon="pi pi-check"
              severity="success"
              text
              size="small"
              v-tooltip="'Mark complete'"
              aria-label="Mark complete"
              @click.stop="quickStatus(data, 'completed')"
            />
            <Button
              v-else
              icon="pi pi-undo"
              severity="warn"
              text
              size="small"
              v-tooltip="'Reopen'"
              aria-label="Reopen"
              @click.stop="quickStatus(data, 'open')"
            />
            <Button
              v-tooltip="'Edit'"
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              @click.stop="openEdit(data)"
            />
            <Button
              v-tooltip="'Delete'"
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              @click.stop="confirmDelete(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="dialogTitle"
        :style="{ width: '620px' }"
        modal
        :closable="!isDirty"
        :close-on-escape="!isDirty"
      >
        <div class="form-grid">
          <FormField v-model="form.title" label="Title" required class="full-width" />
          <FormField v-model="form.description" label="Description" as="textarea" :rows="3" class="full-width" />
          <FormField v-model="form.priority" label="Priority" as="select" :options="priorityOptions" />
          <FormField v-model="form.status" label="Status" as="select" :options="statusOptions" />
          <FormField v-model="form.assigned_to" label="Assigned To" />
          <div class="form-field">
            <label>Due Date</label>
            <DatePicker v-model="form.due_date" class="w-full" showIcon />
          </div>
          <div class="form-field">
            <label>Related Job ID</label>
            <InputNumber v-model="form.related_job_id" mode="decimal" min="0" class="w-full" showButtons />
          </div>
          <div class="form-field">
            <label>Related Customer ID</label>
            <InputNumber v-model="form.related_customer_id" mode="decimal" min="0" class="w-full" showButtons />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="cancelDialog" />
          <Button :label="editingTask ? 'Save' : 'Create'" icon="pi pi-check" @click="saveTask" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import FormField from '../components/FormField.vue';
import { useDirtyDialog } from '../composables/useDirtyDialog';
import { useListPrefs } from '../composables/useListPrefs';
import { useTableExport } from '../composables/useTableExport';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const tasks = ref([]);
const loading = ref(true);
const statusFilter = ref('open');
const filterTabs = ['open', 'in_progress', 'completed', 'all'];
const showDialog = ref(false);
const editingTask = ref(null);
const saving = ref(false);
const form = ref(emptyForm());

useListPrefs(
  'tasks',
  { statusFilter },
  { statusFilter: { default: 'open', valid: (v) => filterTabs.includes(v) } },
);

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value);
const { exportCsv } = useTableExport();

function exportRows() {
  exportCsv(filteredTasks.value, [
    { field: 'title', header: 'Title' },
    { field: 'priority', header: 'Priority' },
    { field: 'status', header: 'Status' },
    { field: 'assigned_to', header: 'Assigned To' },
    { field: 'due_date', header: 'Due Date' },
    { field: 'related_job_id', header: 'Job' },
    { field: 'related_customer_id', header: 'Customer' },
  ], 'tasks');
}

// {label,value} shape for FormField's Select wrapper.
const priorityOptions = ['low', 'normal', 'high', 'urgent'].map((v) => ({ label: v, value: v }));
const statusOptions = ['open', 'in_progress', 'completed', 'cancelled'].map((v) => ({ label: v.replace('_', ' '), value: v }));

const dialogTitle = computed(() => (editingTask.value ? 'Edit Task' : 'New Task'));

const counts = computed(() => {
  const c = { all: tasks.value.length };
  tasks.value.forEach((task) => {
    const key = task.status || 'open';
    c[key] = (c[key] || 0) + 1;
  });
  return c;
});

const filteredTasks = computed(() => {
  if (statusFilter.value === 'all') return tasks.value;
  return tasks.value.filter((task) => task.status === statusFilter.value);
});

function emptyForm() {
  return {
    title: '',
    description: '',
    priority: 'normal',
    status: 'open',
    assigned_to: '',
    due_date: null,
    related_job_id: null,
    related_customer_id: null,
  };
}

function tabLabel(tab) {
  if (tab === 'all') return 'All';
  return tab.replace('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function tabLabelWithCount(tab) {
  const count = counts.value[tab] ?? 0;
  return `${tabLabel(tab)}${count ? ` (${count})` : ''}`;
}

function priorityLabel(value) {
  return value ? value.replace('_', ' ') : 'Normal';
}

function prioritySeverity(value) {
  return {
    low: 'info',
    normal: 'success',
    high: 'warning',
    urgent: 'danger',
  }[value] || 'info';
}

function statusLabel(value) {
  if (!value) return 'Open';
  return value.replace('_', ' ');
}

function statusSeverity(value) {
  return {
    open: 'info',
    in_progress: 'warning',
    completed: 'success',
    cancelled: 'danger',
  }[value] || 'info';
}

async function loadTasks() {
  loading.value = true;
  try {
    const data = await api.get('/api/tasks');
    const list = Array.isArray(data) ? data : data?.items || [];
    tasks.value = list;
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingTask.value = null;
  form.value = emptyForm();
  snapshot();
  showDialog.value = true;
}

function openEdit(task) {
  editingTask.value = task;
  form.value = {
    ...task,
    due_date: task.due_date ? new Date(task.due_date) : null,
  };
  snapshot();
  showDialog.value = true;
}

function cancelDialog() {
  if (confirmDiscard()) showDialog.value = false;
}

async function saveTask() {
  if (!form.value.title.trim()) return;
  saving.value = true;
  const payload = {
    ...form.value,
    due_date: form.value.due_date ? form.value.due_date.toISOString() : null,
  };
  try {
    if (editingTask.value) {
      await api.patch(`/api/tasks/${editingTask.value.id}`, payload, { successMessage: 'Task updated' });
    } else {
      await api.post('/api/tasks', payload, { successMessage: 'Task created' });
    }
    showDialog.value = false;
    await loadTasks();
  } finally {
    saving.value = false;
  }
}

async function quickStatus(task, status) {
  await api.patch(`/api/tasks/${task.id}`, { status }, { successMessage: status === 'completed' ? 'Task completed' : 'Task reopened' });
  await loadTasks();
}

async function confirmDelete(task) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete task "${task.title}"?` }))) return;
  await api.del(`/api/tasks/${task.id}`, { successMessage: 'Task deleted' });
  await loadTasks();
}

onMounted(() => {
  loadTasks();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}
.filter-tabs {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 1rem 0;
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
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
.clickable-row {
  cursor: pointer;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem;
}
</style>
