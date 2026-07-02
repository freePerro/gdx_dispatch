<template>
    <section class="job-templates-view view-card">
      <Toolbar data-testid="job-templates-toolbar">
        <template #start>
          <h2 class="page-title">Job Templates</h2>
        </template>
        <template #end>
          <Button label="+ New Template" icon="pi pi-plus" data-testid="new-template-btn" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="job-templates-tabs">
        <Button
          v-for="tab in templateTabs"
          :key="tab"
          :label="tab === 'all' ? 'All' : titleFromService(tab)"
          :severity="filterType === tab ? undefined : 'secondary'"
          size="small"
          @click="filterType = tab"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredTemplates"
        paginator
        :rows="15"
        striped-rows
        data-testid="job-templates-table"
        
        @row-click="($event) => openEdit($event.data)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-clone"
            title="No job templates yet"
            message="Templates pre-fill duration, price, and line items so new jobs take seconds to book."
            action-label="New Template"
            @action="openCreate"
          />
        </template>
        <Column field="name" header="Name" />
        <Column field="service_type" header="Service Type">
          <template #body="{ data }">{{ titleFromService(data.service_type) }}</template>
        </Column>
        <Column field="default_duration" header="Default Duration (mins)">
          <template #body="{ data }">{{ data.default_duration ?? '—' }}</template>
        </Column>
        <Column field="default_price" header="Default Price">
          <template #body="{ data }">{{ formatMoney(data.default_price || 0) }}</template>
        </Column>
        <Column header="Line Items" style="width:160px">
          <template #body="{ data }">{{ lineItemCount(data) }}</template>
        </Column>
        <Column field="updated_at" header="Updated">
          <template #body="{ data }">{{ formatDate(data.updated_at) }}</template>
        </Column>
      </DataTable>

      <Dialog
        header="Job Template"
        v-model:visible="showDialog"
        modal
        :style="{ width: '620px' }"
        data-testid="job-templates-dialog"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name</label>
            <InputText v-model="form.name" data-testid="template-name" />
          </div>
          <div class="form-field">
            <label>Service Type</label>
            <Select v-model="form.service_type" :options="serviceTypeOptions" optionLabel="label" optionValue="value" data-testid="template-service" />
          </div>
          <div class="form-field">
            <label>Default Duration (minutes)</label>
            <InputNumber v-model="form.default_duration" :min="0" show-buttons step="15" data-testid="template-duration" />
          </div>
          <div class="form-field">
            <label>Default Price</label>
            <InputNumber v-model="form.default_price" mode="currency" currency="USD" :min="0" data-testid="template-price" />
          </div>
          <div class="form-field full-width">
            <label>Description</label>
            <Textarea v-model="form.description" rows="3" data-testid="template-description" />
          </div>
          <div class="form-field full-width">
            <label>Line Items (JSON)</label>
            <Textarea v-model="form.line_items" rows="4" data-testid="template-line-items" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" />
          <Button
            :label="editingTemplate ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveTemplate"
            data-testid="template-save"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate, formatMoney } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Toolbar from 'primevue/toolbar';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Textarea from 'primevue/textarea';
import ProgressSpinner from 'primevue/progressspinner';

const api = useApiWithToast();

const templates = ref([]);
const loading = ref(false);
const saving = ref(false);
const showDialog = ref(false);
const editingTemplate = ref(null);
const filterType = ref('all');

const serviceTypeOptions = [
  { label: 'Installation', value: 'installation' },
  { label: 'Repair', value: 'repair' },
  { label: 'Maintenance', value: 'maintenance' },
  { label: 'Inspection', value: 'inspection' },
];

const templateTabs = ['all', ...serviceTypeOptions.map((option) => option.value)];

const formDefaults = () => ({
  name: '',
  service_type: serviceTypeOptions[0].value,
  default_duration: null,
  default_price: null,
  description: '',
  line_items: '[]',
});

const form = ref(formDefaults());

const filteredTemplates = computed(() => {
  if (filterType.value === 'all') return templates.value;
  return templates.value.filter((template) => template.service_type === filterType.value);
});

function titleFromService(value) {
  const type = serviceTypeOptions.find((option) => option.value === value);
  return type ? type.label : value;
}

function lineItemCount(template) {
  if (typeof template.line_items_count === 'number') return template.line_items_count;
  if (Array.isArray(template.line_items)) return template.line_items.length;
  return 0;
}

function resetForm() {
  form.value = formDefaults();
}

function closeDialog() {
  showDialog.value = false;
}

function openCreate() {
  editingTemplate.value = null;
  resetForm();
  showDialog.value = true;
}

function openEdit(template) {
  editingTemplate.value = template;
  form.value = {
    name: template.name || '',
    service_type: template.service_type || serviceTypeOptions[0].value,
    default_duration: template.default_duration ?? null,
    default_price: template.default_price ?? null,
    description: template.description || '',
    line_items: template.line_items
      ? JSON.stringify(template.line_items, null, 2)
      : template.line_items_text || '[]',
  };
  showDialog.value = true;
}

function parseLineItems(value) {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function loadTemplates() {
  loading.value = true;
  try {
    const data = await api.get('/api/job-templates');
    templates.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

async function saveTemplate() {
  if (!form.value.name.trim()) return;
  saving.value = true;
  const payload = {
    name: form.value.name,
    service_type: form.value.service_type,
    default_duration: form.value.default_duration,
    default_price: form.value.default_price,
    description: form.value.description,
    line_items: parseLineItems(form.value.line_items),
  };
  try {
    if (editingTemplate.value) {
      await api.patch(`/api/job-templates/${editingTemplate.value.id}`, payload, { successMessage: 'Template saved' });
    } else {
      await api.post('/api/job-templates', payload, { successMessage: 'Template created' });
    }
    await loadTemplates();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

onMounted(loadTemplates);
</script>
