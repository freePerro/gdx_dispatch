<template>
    <section class="surveys-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Survey Management</h2>
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="survey-tabs">
        <TabList>
          <Tab value="templates">Templates</Tab>
          <Tab value="responses">Responses</Tab>
          <Tab value="metrics">Metrics</Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'templates'" class="panel-section">
        <div class="tab-toolbar">
          <Button label="+ Template" icon="pi pi-plus" @click="openTemplateDialog" />
          <Button label="Refresh" icon="pi pi-sync" class="ml-2" @click="loadTemplates" />
        </div>

        <div v-if="templatesLoading" class="spinner-wrap"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll" v-else :value="templates" dataKey="id" class="survey-table" striped-rows>
          <template #empty>
            <EmptyState icon="pi pi-comments" title="No survey templates" message="Create a template to send NPS or post-job surveys." />
          </template>
          <Column field="name" header="Template" />
          <Column field="template_type" header="Type" />
          <Column field="send_channel" header="Channel" />
          <Column header="Last Sent">
            <template #body="slotProps">
              {{ formatDateValue(slotProps.data.last_sent_at) }}
            </template>
          </Column>
          <Column header="Actions" style="width:230px">
            <template #body="slotProps">
              <Button
                label="Send"
                icon="pi pi-paper-plane"
                size="small"
                class="mr-1"
                :loading="sendingTemplateId === slotProps.data.id"
                @click="sendTemplate(slotProps.data.id)"
              />
              <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" class="mr-1" @click.stop="openEditTemplate(slotProps.data)" />
              <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="deleteTemplate(slotProps.data)" />
            </template>
          </Column>
        </DataTable>
      </div>

      <div v-else-if="activeTab === 'responses'" class="panel-section">
        <div class="filter-row">
          <Select
            v-model="responsesFilter"
            :options="responseFilterOptions"
            optionLabel="label"
            optionValue="value"
            class="responses-select"
          />
          <Button label="Reload" icon="pi pi-sync" class="ml-2" @click="loadResponses" />
        </div>

        <div v-if="responsesLoading" class="spinner-wrap"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll" v-else :value="responses" dataKey="id" striped-rows>
          <Column header="Template">
            <template #body="slotProps">
              {{ slotProps.data.template_name || slotProps.data.template?.name || '—' }}
            </template>
          </Column>
          <Column header="Respondent">
            <template #body="slotProps">
              {{ slotProps.data.respondent_name || slotProps.data.customer_name || '—' }}
            </template>
          </Column>
          <Column field="score" header="Score" style="width:110px" />
          <Column header="Comment">
            <template #body="slotProps">
              {{ slotProps.data.comment || slotProps.data.response || '—' }}
            </template>
          </Column>
          <Column field="created_at" header="Received" />
        </DataTable>
      </div>

      <div v-else class="panel-section metrics-panel">
        <div v-if="metricsLoading" class="spinner-wrap"><ProgressSpinner /></div>
        <div v-else>
          <div class="metric-row">
            <Card class="metric-card">
              <div class="metric-label">NPS Score</div>
              <div class="metric-value">{{ metrics.nps_score ?? '—' }}</div>
            </Card>
            <Card class="metric-card">
              <div class="metric-label">CSAT Average</div>
              <div class="metric-value">{{ metrics.csat_score ?? '—' }}</div>
            </Card>
          </div>
          <div class="metric-row compact">
            <Card class="metric-card">
              <div class="metric-label">Total Sent</div>
              <div class="metric-value">{{ metrics.total_sent ?? '—' }}</div>
            </Card>
            <Card class="metric-card">
              <div class="metric-label">Total Responded</div>
              <div class="metric-value">{{ metrics.total_responded ?? '—' }}</div>
            </Card>
            <Card class="metric-card">
              <div class="metric-label">Response Rate</div>
              <div class="metric-value">{{ metrics.response_rate ? `${metrics.response_rate}%` : '—' }}</div>
            </Card>
          </div>
        </div>
      </div>

      <Dialog
        v-model:visible="templateDialogVisible"
        :header="editingTemplate ? 'Edit Template' : 'New Template'"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name</label>
            <InputText v-model="templateForm.name" class="w-full" />
          </div>
          <div class="form-field">
            <label>Survey Type</label>
            <Select v-model="templateForm.template_type" :options="templateTypeOptions" class="w-full" />
          </div>
          <div class="form-field">
            <label>Channel</label>
            <Select v-model="templateForm.send_channel" :options="channelOptions" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Message / Question</label>
            <Textarea v-model="templateForm.message" rows="4" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="templateDialogVisible = false" />
          <Button label="Save" icon="pi pi-check" :loading="templateSaving" @click="saveTemplate" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import EmptyState from '../components/EmptyState.vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
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

const activeTab = ref('templates');
const templates = ref([]);
const templatesLoading = ref(false);
const responses = ref([]);
const responsesLoading = ref(false);
const responsesFilter = ref('all');
const metrics = ref({});
const metricsLoading = ref(false);
const templateDialogVisible = ref(false);
const editingTemplate = ref(null);
const templateSaving = ref(false);
const sendingTemplateId = ref(null);

const templateForm = ref({
  name: '',
  template_type: 'nps',
  send_channel: 'email',
  message: '',
});

const templateTypeOptions = [
  { label: 'NPS', value: 'nps' },
  { label: 'CSAT', value: 'csat' },
];

const channelOptions = [
  { label: 'Email', value: 'email' },
  { label: 'SMS', value: 'sms' },
];

const responseFilterOptions = computed(() => [
  { label: 'All Templates', value: 'all' },
  ...templates.value.map((template) => ({ label: template.name, value: template.id })),
]);

const formatDateValue = (value) => (value ? value.split('T')[0] : '—');

async function loadTemplates() {
  templatesLoading.value = true;
  try {
    const data = await api.get('/api/surveys/templates');
    templates.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    templatesLoading.value = false;
  }
}

async function loadResponses() {
  responsesLoading.value = true;
  try {
    const params = new URLSearchParams();
    params.set('limit', '50');
    if (responsesFilter.value !== 'all') {
      params.set('template_id', responsesFilter.value);
    }
    const data = await api.get(`/api/surveys/responses?${params.toString()}`);
    responses.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    responsesLoading.value = false;
  }
}

async function loadMetrics() {
  metricsLoading.value = true;
  try {
    metrics.value = await api.get('/api/surveys/metrics?days=30');
  } finally {
    metricsLoading.value = false;
  }
}

function openTemplateDialog() {
  editingTemplate.value = null;
  templateForm.value = {
    name: '',
    template_type: 'nps',
    send_channel: 'email',
    message: '',
  };
  templateDialogVisible.value = true;
}

function openEditTemplate(template) {
  editingTemplate.value = template;
  templateForm.value = {
    name: template.name,
    template_type: template.template_type || 'nps',
    send_channel: template.send_channel || 'email',
    message: template.message || template.body || '',
  };
  templateDialogVisible.value = true;
}

async function saveTemplate() {
  if (!templateForm.value.name.trim()) {
    return;
  }
  templateSaving.value = true;
  const payload = { ...templateForm.value };
  try {
    if (editingTemplate.value) {
      await api.patch(`/api/surveys/templates/${editingTemplate.value.id}`, payload, {
        successMessage: 'Template updated',
      });
    } else {
      await api.post('/api/surveys/templates', payload, { successMessage: 'Template created' });
    }
    await loadTemplates();
    await loadResponses();
    templateDialogVisible.value = false;
  } finally {
    templateSaving.value = false;
  }
}

async function deleteTemplate(template) {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Delete this template?' }))) return;
  await api.del(`/api/surveys/templates/${template.id}`, { successMessage: 'Template deleted' });
  await loadTemplates();
  await loadResponses();
}

async function sendTemplate(templateId) {
  sendingTemplateId.value = templateId;
  try {
    await api.post('/api/surveys/send', { template_id: templateId }, { successMessage: 'Survey sent' });
  } finally {
    sendingTemplateId.value = null;
  }
}

const loadAll = async () => {
  await loadTemplates();
  await loadResponses();
  await loadMetrics();
};

onMounted(loadAll);
watch(responsesFilter, loadResponses);
</script>
