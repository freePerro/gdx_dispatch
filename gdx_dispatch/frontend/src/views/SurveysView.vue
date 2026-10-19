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
          <Column header="Type" style="width:100px">
            <template #body="slotProps">{{ kindLabel(slotProps.data.kind) }}</template>
          </Column>
          <Column field="question" header="Question" />
          <Column header="Active" style="width:90px">
            <template #body="slotProps">{{ slotProps.data.active ? 'Yes' : 'No' }}</template>
          </Column>
          <Column header="Actions" style="width:230px">
            <template #body="slotProps">
              <Button
                label="Create link"
                icon="pi pi-link"
                size="small"
                class="mr-1"
                :loading="sendingTemplateId === slotProps.data.id"
                data-testid="survey-create-link-btn"
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
              <div class="metric-value">{{ metrics.csat_avg ?? '—' }}</div>
            </Card>
          </div>
          <div class="metric-row compact">
            <Card class="metric-card">
              <div class="metric-label">Links Created</div>
              <div class="metric-value">{{ metrics.total_sent ?? '—' }}</div>
            </Card>
            <Card class="metric-card">
              <div class="metric-label">Total Responded</div>
              <div class="metric-value">{{ metrics.total_responded ?? '—' }}</div>
            </Card>
            <Card class="metric-card">
              <div class="metric-label">Response Rate</div>
              <div class="metric-value">{{ metrics.total_sent ? `${Math.round(metrics.response_rate * 100)}%` : '—' }}</div>
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
            <label for="survey-tpl-name">Name</label>
            <InputText id="survey-tpl-name" v-model="templateForm.name" maxlength="200" class="w-full" data-testid="survey-tpl-name" />
          </div>
          <div class="form-field">
            <label for="survey-tpl-kind">Survey Type</label>
            <Select id="survey-tpl-kind" v-model="templateForm.kind" :options="templateTypeOptions" optionLabel="label" optionValue="value" class="w-full" data-testid="survey-tpl-kind" />
          </div>
          <div class="form-field full-width">
            <label for="survey-tpl-question">Question</label>
            <Textarea id="survey-tpl-question" v-model="templateForm.question" rows="3" maxlength="500" class="w-full" data-testid="survey-tpl-question" />
          </div>
          <div class="form-field full-width">
            <label for="survey-tpl-follow-up">Follow-up question (optional)</label>
            <Textarea id="survey-tpl-follow-up" v-model="templateForm.follow_up_question" rows="2" maxlength="500" class="w-full" data-testid="survey-tpl-follow-up" />
          </div>
          <div class="form-field">
            <label for="survey-tpl-active">Active</label>
            <ToggleSwitch inputId="survey-tpl-active" v-model="templateForm.active" data-testid="survey-tpl-active" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="templateDialogVisible = false" />
          <Button label="Save" icon="pi pi-check" :loading="templateSaving" :disabled="!templateForm.name.trim() || !templateForm.question.trim()" data-testid="survey-tpl-save" @click="saveTemplate" />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="linkDialogVisible"
        header="Survey link created"
        modal
        :style="{ width: '520px' }"
        data-testid="survey-link-dialog"
      >
        <p class="survey-link-hint">
          Nothing was sent — copy this link and text or email it to the customer.
          It works once and expires in 30 days.
        </p>
        <div class="survey-link-row">
          <InputText :model-value="createdLink" readonly class="w-full" data-testid="survey-link-input" @focus="$event.target.select()" />
          <Button icon="pi pi-copy" :label="linkCopied ? 'Copied' : 'Copy'" @click="copyCreatedLink" data-testid="survey-link-copy" />
        </div>
        <template #footer>
          <Button label="Done" @click="linkDialogVisible = false" />
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
import ToggleSwitch from 'primevue/toggleswitch';
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
const linkDialogVisible = ref(false);
const createdLink = ref('');
const linkCopied = ref(false);
const editingTemplate = ref(null);
const templateSaving = ref(false);
const sendingTemplateId = ref(null);

// The template fields are SurveyTemplateIn's (routers/surveys.py). Until
// GDXA-1693 the form sent {template_type, send_channel, message}: create was a
// 422 every time (question is required) and an edit kept only the name.
const emptyTemplateForm = () => ({
  name: '',
  kind: 'nps',
  question: '',
  follow_up_question: '',
  active: true,
});

const templateForm = ref(emptyTemplateForm());

const templateTypeOptions = [
  { label: 'NPS', value: 'nps' },
  { label: 'CSAT', value: 'csat' },
  { label: 'Custom', value: 'custom' },
];

function kindLabel(kind) {
  return templateTypeOptions.find((o) => o.value === kind)?.label || kind || '—';
}

const responseFilterOptions = computed(() => [
  { label: 'All Templates', value: 'all' },
  ...templates.value.map((template) => ({ label: template.name, value: template.id })),
]);

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
  templateForm.value = emptyTemplateForm();
  templateDialogVisible.value = true;
}

function openEditTemplate(template) {
  editingTemplate.value = template;
  templateForm.value = {
    name: template.name || '',
    kind: template.kind || 'nps',
    question: template.question || '',
    follow_up_question: template.follow_up_question || '',
    active: template.active !== false,
  };
  templateDialogVisible.value = true;
}

async function saveTemplate() {
  const f = templateForm.value;
  if (!f.name.trim() || !f.question.trim()) {
    return;
  }
  templateSaving.value = true;
  const payload = {
    name: f.name.trim(),
    kind: f.kind,
    question: f.question.trim(),
    follow_up_question: f.follow_up_question.trim() || null,
    active: Boolean(f.active),
  };
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
  // Honest labeling (silent-success sweep 2026-09-19): the endpoint mints a
  // single-use link and sends nothing — the old success toast claimed a
  // send that never happened. Show the link so the office can deliver it.
  sendingTemplateId.value = templateId;
  try {
    const res = await api.post('/api/surveys/send', { template_id: templateId });
    createdLink.value = `${window.location.origin}${res.public_url}`;
    linkCopied.value = false;
    linkDialogVisible.value = true;
  } finally {
    sendingTemplateId.value = null;
  }
}

async function copyCreatedLink() {
  try {
    await navigator.clipboard.writeText(createdLink.value);
    linkCopied.value = true;
  } catch {
    // Clipboard can be unavailable (http, permissions); the input stays
    // selectable for a manual copy, so no fake "Copied" state.
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

<style scoped>
.form-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
.survey-link-hint {
  margin-bottom: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
}

.survey-link-row {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
</style>
