<template>
    <section class="co-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Automations</h2>
        </template>
        <template #end>
          <Button
            data-testid="automations-new-btn"
            label="+ New Automation"
            icon="pi pi-plus"
            @click="openCreate"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab"
          :label="tab === 'all' ? `All (${counts.all || 0})` : `${tab} (${counts[tab] || 0})`"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          :data-testid="`automations-tab-${tab}`"
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
        <Column field="name" header="Name" />
        <Column field="trigger_type" header="Trigger" />
        <Column field="action_type" header="Action" />
        <Column field="status" header="Status" style="width: 160px">
          <template #body="{ data }">
            <Tag :value="statusLabel(data)" :severity="statusSeverity(statusLabel(data))" />
          </template>
        </Column>
        <Column field="updated_at" header="Updated" style="width: 140px">
          <template #body="{ data }">{{ formatDate(data.updated_at) }}</template>
        </Column>
        <Column header="Actions" style="width: 110px">
          <template #body="{ data }">
            <Button
              :data-testid="`automation-edit-${data.id}`"
              v-tooltip="'Edit'"
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              @click.stop="openEdit(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingAutomation ? `Edit ${editingAutomation.name || 'Automation'}` : 'New Automation'"
        modal
        :style="{ width: '600px' }"
        @hide="resetForm"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Name *</label>
            <InputText
              data-testid="automation-name-input"
              v-model="form.name"
              placeholder="Automated email when payment hits"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Trigger *</label>
            <Select
              data-testid="automation-trigger-select"
              v-model="form.trigger_type"
              :options="triggerOptions"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Action *</label>
            <Select
              data-testid="automation-action-select"
              v-model="form.action_type"
              :options="actionOptions"
              class="w-full"
            />
          </div>
          <div class="form-field full-width">
            <label>Config (JSON)</label>
            <Textarea
              data-testid="automation-config-input"
              v-model="form.config"
              rows="4"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Active</label>
            <ToggleSwitch
              data-testid="automation-active-toggle"
              v-model="form.active"
              class="p-switch-sm"
            />
          </div>
        </div>
        <template #footer>
          <Button
            data-testid="automation-cancel-btn"
            label="Cancel"
            severity="secondary"
            @click="showDialog = false"
          />
          <Button
            data-testid="automation-save-btn"
            :label="editingAutomation ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveAutomation"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate } from '../composables/useFormatters';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();
const automations = ref([]);
const loading = ref(true);
const statusFilter = ref('all');
const showDialog = ref(false);
const editingAutomation = ref(null);
const saving = ref(false);

const triggerOptions = ['job_created', 'payment_received', 'appointment_scheduled'];
const actionOptions = ['send_sms', 'send_email', 'create_task'];
const statusTabs = ['all', 'active', 'inactive', 'paused'];

const emptyForm = () => ({
  name: '',
  trigger_type: triggerOptions[0],
  action_type: actionOptions[1],
  config: '{\n  \n}',
  active: true,
});
const form = ref(emptyForm());

const counts = computed(() => {
  const result = {
    all: automations.value.length,
    active: 0,
    inactive: 0,
    paused: 0,
  };
  automations.value.forEach((item) => {
    const status = statusLabel(item);
    if (!result[status]) result[status] = 0;
    result[status] += 1;
  });
  return result;
});

const filtered = computed(() => {
  if (statusFilter.value === 'all') return automations.value;
  return automations.value.filter((item) => statusLabel(item) === statusFilter.value);
});

function statusLabel(item) {
  const status = item.status ?? (item.active ? 'active' : 'inactive');
  return typeof status === 'string' ? status.toLowerCase() : 'inactive';
}

function statusSeverity(value) {
  return {
    active: 'success',
    inactive: 'warning',
    paused: 'info',
  }[value] || 'secondary';
}

async function loadAutomations() {
  loading.value = true;
  try {
    const data = await api.get('/api/automations');
    automations.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingAutomation.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(entry) {
  editingAutomation.value = entry;
  form.value = {
    name: entry.name || '',
    trigger_type: entry.trigger_type || triggerOptions[0],
    action_type: entry.action_type || actionOptions[0],
    config: entry.config || '{\n  \n}',
    active: typeof entry.active === 'boolean' ? entry.active : true,
  };
  showDialog.value = true;
}

function resetForm() {
  form.value = emptyForm();
  editingAutomation.value = null;
}

async function saveAutomation() {
  if (!form.value.name.trim()) return;
  saving.value = true;
  try {
    const payload = {
      ...form.value,
      status: form.value.active ? 'active' : 'inactive',
    };
    if (editingAutomation.value) {
      await api.patch(`/api/automations/${editingAutomation.value.id}`, payload, {
        successMessage: 'Automation updated',
      });
    } else {
      await api.post('/api/automations', payload, { successMessage: 'Automation created' });
    }
    await loadAutomations();
    showDialog.value = false;
  } finally {
    saving.value = false;
  }
}

const emptyMessage = 'No automations yet';

onMounted(loadAutomations);
</script>
