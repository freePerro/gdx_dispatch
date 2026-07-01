<template>
    <section class="checklists-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Checklists</h2>
        </template>
        <template #end>
          <Button label="+ New Template" icon="pi pi-plus" severity="primary" @click="openTemplateDialog" />
        </template>
      </Toolbar>

      <Tabs v-model:value="activeTab" class="checklist-tabs">
        <TabList>
          <Tab value="templates">Templates</Tab>
          <Tab value="job">Job Checklist</Tab>
        </TabList>
        <TabPanels>
        <TabPanel value="templates">
          <div class="tab-content">
            <div v-if="templatesLoading" class="spinner-wrap"><ProgressSpinner /></div>
            <DataTable
      responsiveLayout="scroll"
              v-else
              :value="templates"
              row-key="id"
              paginator
              :rows="10"
              striped-rows
              class="clickable-row"
            >
              <Column field="name" header="Template" />
              <Column header="Items" :body="templateItemCount" />
              <Column header="Created" :body="formatTemplateDate" />
              <Column header="Actions" style="width:140px">
                <template #body="{ data }">
                  <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openTemplateDialog(data)" />
                </template>
              </Column>
            </DataTable>
            <div v-if="!templatesLoading && !templates.length" class="empty-state">
              <i class="pi pi-list"></i>
              <p>No templates yet.</p>
            </div>
          </div>
        </TabPanel>

        <TabPanel value="job">
          <div class="tab-content">
            <div class="job-controls">
              <div class="control-block">
                <label>Job ID</label>
                <InputText v-model="jobId" placeholder="Enter job id" @keyup.enter="loadJobChecklist" />
              </div>
              <Button label="Load Checklist" icon="pi pi-search" @click="loadJobChecklist" :loading="jobChecklistLoading" />
            </div>
            <div class="job-controls">
              <div class="control-block">
                <label>Template</label>
                <Select
                  v-model="selectedTemplateId"
                  :options="templateOptions"
                  placeholder="Choose template"
                  class="w-full"
                />
              </div>
              <Button
                label="Create Checklist"
                icon="pi pi-play"
                severity="success"
                :disabled="!jobId.trim() || !selectedTemplateId"
                :loading="creatingChecklist"
                @click="createJobChecklist"
              />
            </div>
            <div v-if="jobChecklistLoading" class="spinner-wrap"><ProgressSpinner /></div>
            <div v-else-if="jobChecklist">
              <div class="checklist-header">
                <span>Checklist ID: {{ jobChecklist.id }}</span>
                <span>Template: {{ jobChecklist.template_id }}</span>
                <span>Created: {{ formatDateValue(jobChecklist.created_at) }}</span>
              </div>
              <DataTable
      responsiveLayout="scroll" :value="jobChecklist.items" row-key="id" striped-rows>
                <Column field="label" header="Item" />
                <Column header="Complete" style="width:120px">
                  <template #body="{ data }">
                    <ToggleSwitch :model-value="data.completed" @change="toggleChecklistItem(data)" />
                  </template>
                </Column>
              </DataTable>
            </div>
            <div v-else class="empty-state">
              <i class="pi pi-clipboard"></i>
              <p>Load a job checklist to manage items.</p>
            </div>
          </div>
        </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog v-model:visible="templateDialogVisible" :header="dialogTitle" :style="{ width: '600px' }" modal>
        <form class="template-form" @submit.prevent="saveTemplate">
          <div class="form-field">
            <label>Template Name</label>
            <InputText v-model="dialogName" placeholder="Add a template name" class="w-full" />
          </div>
          <div class="form-field">
            <label>New Item</label>
            <div class="item-input">
              <InputText v-model="newItemText" placeholder="Type item text" class="w-full" @keyup.enter="addDialogItem" />
              <Button v-tooltip="'Add item'" aria-label="Add item" icon="pi pi-plus" class="p-button-text" @click.prevent="addDialogItem" />
            </div>
          </div>
          <OrderList
            v-model="dialogItems"
            dragdrop
            :list-style="{ maxHeight: '280px' }"
            class="order-list"
          >
            <template #item="slotProps">
              <div class="order-row">
                <span>{{ slotProps.item }}</span>
                <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="removeDialogItem(slotProps.index)" />
              </div>
            </template>
          </OrderList>
          <div class="form-actions">
            <Button label="Cancel" text @click="closeTemplateDialog" />
            <Button label="Save" severity="primary" type="submit" :loading="savingTemplate" />
          </div>
        </form>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import ToggleSwitch from 'primevue/toggleswitch';
import InputText from 'primevue/inputtext';
import OrderList from 'primevue/orderlist';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const activeTab = ref('templates');
const templates = ref([]);
const templatesLoading = ref(true);
const templateDialogVisible = ref(false);
const dialogName = ref('');
const dialogItems = ref([]);
const newItemText = ref('');
const savingTemplate = ref(false);
const editingTemplate = ref(null);

const jobId = ref('');
const jobChecklist = ref(null);
const jobChecklistLoading = ref(false);
const selectedTemplateId = ref('');
const creatingChecklist = ref(false);

const templateOptions = computed(() =>
  templates.value.map((tmpl) => ({ label: tmpl.name, value: tmpl.id }))
);

const dialogTitle = computed(() => (editingTemplate.value ? `Edit ${editingTemplate.value.name}` : 'New Template'));

onMounted(() => {
  loadTemplates();
});

watch(templateDialogVisible, (visible) => {
  if (!visible) {
    resetDialog();
  }
});

async function loadTemplates() {
  templatesLoading.value = true;
  try {
    const data = await api.get('/api/checklist-templates');
    templates.value = Array.isArray(data) ? data : [];
  } catch (err) {
    console.error('load_checklist_templates_failed', err?.message || err);
    templates.value = [];
  } finally {
    templatesLoading.value = false;
  }
}

function templateItemCount(row) {
  return (row.items?.length || 0).toString();
}

function formatTemplateDate(row) {
  return formatDateValue(row.created_at);
}

function formatDateValue(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString();
}

function openTemplateDialog(template = null) {
  editingTemplate.value = template;
  if (template) {
    dialogName.value = template.name;
    dialogItems.value = [...(template.items || [])];
  } else {
    dialogName.value = '';
    dialogItems.value = [];
  }
  templateDialogVisible.value = true;
}

function closeTemplateDialog() {
  templateDialogVisible.value = false;
}

function resetDialog() {
  dialogName.value = '';
  dialogItems.value = [];
  newItemText.value = '';
  editingTemplate.value = null;
}

function addDialogItem() {
  const text = newItemText.value.trim();
  if (!text) return;
  dialogItems.value = [...dialogItems.value, text];
  newItemText.value = '';
}

function removeDialogItem(index) {
  dialogItems.value = dialogItems.value.filter((_, idx) => idx !== index);
}

async function saveTemplate() {
  if (!dialogName.value.trim() || !dialogItems.value.length) return;
  savingTemplate.value = true;
  try {
    await api.post(
      '/api/checklist-templates',
      { name: dialogName.value.trim(), items: dialogItems.value },
      { successMessage: 'Checklist template saved' }
    );
    await loadTemplates();
    closeTemplateDialog();
  } catch (err) {
    console.error('save_checklist_template_failed', err?.message || err);
  } finally {
    savingTemplate.value = false;
  }
}

async function loadJobChecklist() {
  if (!jobId.value.trim()) return;
  jobChecklistLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${jobId.value.trim()}/checklist`);
    jobChecklist.value = data;
  } catch {
    jobChecklist.value = null;
  } finally {
    jobChecklistLoading.value = false;
  }
}

async function createJobChecklist() {
  if (!jobId.value.trim() || !selectedTemplateId.value) return;
  creatingChecklist.value = true;
  try {
    await api.post(
      `/api/jobs/${jobId.value.trim()}/checklist`,
      { template_id: selectedTemplateId.value },
      { successMessage: 'Job checklist created' }
    );
    await loadJobChecklist();
  } finally {
    creatingChecklist.value = false;
  }
}

async function toggleChecklistItem(item) {
  if (!jobChecklist.value) return;
  const nextValue = !item.completed;
  await api.patch(
    `/api/checklists/${jobChecklist.value.id}/items/${item.id}`,
    { completed: nextValue },
    { successMessage: 'Checklist item updated' }
  );
  item.completed = nextValue;
}
</script>

<style scoped>
.checklists-view {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.checklist-tabs .p-tabview-panels {
  border: 1px solid var(--border-strong);
  border-top: none;
  border-radius: 0 0 0.75rem 0.75rem;
  padding: var(--space-4);
}
.tab-content {
  min-height: 340px;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: var(--space-5);
}
.empty-state {
  text-align: center;
  padding: var(--space-4);
  color: var(--text-muted);
}
.empty-state i {
  font-size: 2rem;
  margin-bottom: var(--space-2);
}
.job-controls {
  display: flex;
  gap: var(--space-3);
  align-items: flex-end;
  margin-bottom: var(--space-3);
}
.control-block {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.checklist-header {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  margin-bottom: var(--space-3);
  font-size: 0.9rem;
  color: var(--text-muted);
}
.detail-card {
  margin-top: var(--space-3);
}
.template-form .form-field {
  margin-bottom: var(--space-3);
}
.template-form label {
  font-weight: 600;
  margin-bottom: var(--space-2);
  display: block;
}
.item-input {
  display: flex;
  gap: var(--space-2);
}
.order-list {
  border: 1px solid var(--border-strong);
  border-radius: 0.5rem;
  overflow: hidden;
}
.order-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border-subtle);
}
.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
@media (max-width: 900px) {
  .job-controls {
    flex-direction: column;
  }
}
</style>
