<template>
    <section class="custom-fields view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Custom Fields</h2>
        </template>
        <template #end>
          <Button label="+ New Field" icon="pi pi-plus" @click="openCreate" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="definitions"
        dataKey="id"
        rowGroupMode="subheader"
        groupRowsBy="entity_type"
        sortField="entity_type"
        :sortOrder="1"
        striped-rows
        class="custom-fields-table"
      >
        <template #groupheader="slotProps">
          <div class="group-header">
            <strong>{{ slotProps.data.entity_type === 'job' ? 'Job Fields' : 'Customer Fields' }}</strong>
          </div>
        </template>
        <Column field="field_key" header="Field Key" />
        <Column field="label" header="Label" />
        <Column field="field_type" header="Type" />
        <Column field="required" header="Required" style="width:120px">
          <template #body="slotProps">
            <Tag :value="slotProps.data.required ? 'Required' : 'Optional'" :severity="slotProps.data.required ? 'danger' : 'info'" />
          </template>
        </Column>
        <Column field="options" header="Options">
          <template #body="slotProps">
            <span>{{ (slotProps.data.options || []).join(', ') || '—' }}</span>
          </template>
        </Column>
        <Column field="sort_order" header="Sort Order" style="width:120px" />
        <Column header="Actions" style="width:160px">
          <template #body="slotProps">
            <Button icon="pi pi-pencil" aria-label="Edit" text size="small" class="mr-1" @click.stop="openEdit(slotProps.data)" />
            <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(slotProps.data)" />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingDefinition ? 'Edit Custom Field' : 'New Custom Field'"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Entity Type</label>
            <Select v-model="form.entity_type" :options="entityOptions" class="w-full" />
          </div>
          <div class="form-field">
            <label>Field Key</label>
            <InputText v-model="form.field_key" placeholder="ex: referral_source" class="w-full" />
            <small class="help-text">Field key must be snake_case</small>
          </div>
          <div class="form-field">
            <label>Label</label>
            <InputText v-model="form.label" placeholder="Referral Source" class="w-full" />
          </div>
          <div class="form-field">
            <label>Field Type</label>
            <Select v-model="form.field_type" :options="fieldTypeOptions" class="w-full" />
          </div>
          <div class="form-field" v-if="form.field_type === 'select'">
            <label>Options</label>
            <Textarea v-model="optionsInput" rows="3" class="w-full" placeholder="One option per line" />
          </div>
          <div class="form-field">
            <label>Required</label>
            <ToggleSwitch v-model="form.required" />
          </div>
          <div class="form-field">
            <label>Sort Order</label>
            <InputNumber v-model="form.sort_order" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button label="Save" icon="pi pi-check" :loading="saving" @click="saveDefinition" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import ToggleSwitch from 'primevue/toggleswitch';

const api = useApiWithToast();

const definitions = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const saving = ref(false);
const editingDefinition = ref(null);
const optionsInput = ref('');

const entityOptions = [
  { label: 'Customer', value: 'customer' },
  { label: 'Job', value: 'job' },
];

const fieldTypeOptions = [
  { label: 'Text', value: 'text' },
  { label: 'Number', value: 'number' },
  { label: 'Date', value: 'date' },
  { label: 'Select', value: 'select' },
  { label: 'Boolean', value: 'boolean' },
];

const form = ref({
  entity_type: 'customer',
  field_key: '',
  label: '',
  field_type: 'text',
  options: [],
  required: false,
  sort_order: 0,
});

function normalizeFormPayload() {
  const payload = { ...form.value };
  payload.field_key = payload.field_key?.trim();
  payload.label = payload.label?.trim();
  payload.sort_order = Number(payload.sort_order) || 0;
  if (form.value.field_type === 'select') {
    payload.options = optionsInput.value
n      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
  } else {
    payload.options = [];
  }
  return payload;
}

async function loadDefinitions() {
  loading.value = true;
  try {
    const data = await api.get('/api/custom-fields');
    definitions.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingDefinition.value = null;
  form.value = {
    entity_type: 'customer',
    field_key: '',
    label: '',
    field_type: 'text',
    options: [],
    required: false,
    sort_order: 0,
  };
  optionsInput.value = '';
  showDialog.value = true;
}

function openEdit(definition) {
  editingDefinition.value = definition;
  form.value = {
    entity_type: definition.entity_type,
    field_key: definition.field_key,
    label: definition.label,
    field_type: definition.field_type,
    options: definition.options || [],
    required: Boolean(definition.required),
    sort_order: definition.sort_order || 0,
  };
  optionsInput.value = (definition.options || []).join('\n');
  showDialog.value = true;
}

async function saveDefinition() {
  if (!form.value.field_key.trim() || !form.value.label.trim()) {
    return;
  }
  saving.value = true;
  const payload = normalizeFormPayload();
  try {
    if (editingDefinition.value) {
      await api.patch(`/api/custom-fields/${editingDefinition.value.id}`, payload, {
        successMessage: 'Custom field updated',
      });
    } else {
      await api.post('/api/custom-fields', payload, { successMessage: 'Custom field created' });
    }
    await loadDefinitions();
    showDialog.value = false;
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(definition) {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Remove this custom field definition?' }))) return;
  await api.del(`/api/custom-fields/${definition.id}`, { successMessage: 'Custom field deleted' });
  await loadDefinitions();
}

watch(
  () => form.value.field_type,
  (newType) => {
    if (newType !== 'select') {
      optionsInput.value = '';
    }
  }
);

onMounted(loadDefinitions);
</script>
