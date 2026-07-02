<template>
    <section class="equipment-tracking-view view-card">
      <Toolbar data-testid="equipment-toolbar">
        <template #start>
          <h2 class="page-title">Equipment Tracking</h2>
        </template>
        <template #end>
          <Button label="+ Add Equipment" icon="pi pi-plus" data-testid="add-equipment-btn" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="equipment-status-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab"
          :label="tab === 'all' ? 'All' : statusLabel(tab)"
          :severity="statusFilter === tab ? undefined : 'secondary'"
          size="small"
          @click="statusFilter = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredEquipment"
        paginator
        :rows="15"
        striped-rows
        data-testid="equipment-table"
        
        @row-click="($event) => openEdit($event.data)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-box"
            title="No equipment tracked yet"
            message="Add company tools and equipment to track assignments, locations, and service dates."
            action-label="Add Equipment"
            @action="openCreate"
          />
        </template>
        <Column field="name" header="Name" />
        <Column field="serial" header="Serial" />
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="assigned_to" header="Assigned" />
        <Column field="location" header="Location" />
        <Column field="last_serviced" header="Last Serviced">
          <template #body="{ data }">{{ formatDate(data.last_serviced) }}</template>
        </Column>
      </DataTable>

      <Dialog
        header="Equipment"
        v-model:visible="showDialog"
        modal
        :style="{ width: '520px' }"
        @hide="closeDialog"
        data-testid="equipment-dialog"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name</label>
            <InputText v-model="form.name" data-testid="equipment-name" />
          </div>
          <div class="form-field">
            <label>Serial</label>
            <InputText v-model="form.serial" data-testid="equipment-serial" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="form.status" :options="statusOptions" optionLabel="label" optionValue="value" data-testid="equipment-status" />
          </div>
          <div class="form-field">
            <label>Assigned To</label>
            <InputText v-model="form.assigned_to" data-testid="equipment-assigned" />
          </div>
          <div class="form-field">
            <label>Location</label>
            <InputText v-model="form.location" data-testid="equipment-location" />
          </div>
          <div class="form-field">
            <label>Last Serviced</label>
            <DatePicker v-model="form.last_serviced" date-format="yy-mm-dd" show-icon data-testid="equipment-last-serviced" />
          </div>
          <div class="form-field">
            <label>Requires Inspection</label>
            <ToggleSwitch v-model="form.requires_inspection" data-testid="equipment-inspection" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" />
          <Button
            :label="editingEquipment ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveEquipment"
            data-testid="equipment-save"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import InputText from 'primevue/inputtext';
import ToggleSwitch from 'primevue/toggleswitch';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import DatePicker from 'primevue/datepicker';

const api = useApiWithToast();

const equipment = ref([]);
const loading = ref(false);
const saving = ref(false);
const showDialog = ref(false);
const editingEquipment = ref(null);
const statusFilter = ref('all');

const statusOptions = [
  { label: 'In Stock', value: 'in_stock' },
  { label: 'Assigned', value: 'assigned' },
  { label: 'Repair', value: 'repair' },
  { label: 'Retired', value: 'retired' },
];

const statusTabs = ['all', ...statusOptions.map((option) => option.value)];

const formDefaults = () => ({
  name: '',
  serial: '',
  status: 'in_stock',
  assigned_to: '',
  location: '',
  last_serviced: null,
  requires_inspection: false,
});

const form = ref(formDefaults());

const filteredEquipment = computed(() => {
  if (statusFilter.value === 'all') return equipment.value;
  return equipment.value.filter((item) => item.status === statusFilter.value);
});

function statusLabel(status) {
  const option = statusOptions.find((o) => o.value === status);
  return option?.label || status;
}

function statusSeverity(status) {
  if (status === 'repair' || status === 'retired') return 'warning';
  if (status === 'assigned') return 'info';
  return 'success';
}

function formatDate(value) {
  if (!value) return '—';
  try {
    return new Date(value).toISOString().split('T')[0];
  } catch {
    return value;
  }
}

function resetForm() {
  form.value = formDefaults();
}

function openCreate() {
  editingEquipment.value = null;
  resetForm();
  showDialog.value = true;
}

function openEdit(model) {
  editingEquipment.value = model;
  form.value = {
    name: model.name || '',
    serial: model.serial || '',
    status: model.status || 'in_stock',
    assigned_to: model.assigned_to || '',
    location: model.location || '',
    last_serviced: model.last_serviced ? new Date(model.last_serviced) : null,
    requires_inspection: !!model.requires_inspection,
  };
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
}

async function loadEquipment() {
  loading.value = true;
  try {
    const data = await api.get('/api/equipment-tracking');
    equipment.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function saveEquipment() {
  if (!form.value.name.trim()) return;
  saving.value = true;
  const payload = {
    ...form.value,
    last_serviced: form.value.last_serviced ? form.value.last_serviced.toISOString() : null,
  };
  try {
    if (editingEquipment.value) {
      await api.patch(`/api/equipment-tracking/${editingEquipment.value.id}`, payload, { successMessage: 'Equipment updated' });
    } else {
      await api.post('/api/equipment-tracking', payload, { successMessage: 'Equipment added' });
    }
    await loadEquipment();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

onMounted(loadEquipment);
</script>
