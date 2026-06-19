<template>
    <section class="service-agreements-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Service Agreements</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <Button label="+ New Agreement" icon="pi pi-plus" @click="openCreateAgreement" />
            <Button label="+ New Template" icon="pi pi-plus" severity="secondary" @click="openCreateTemplate" />
          </div>
        </template>
      </Toolbar>

      <div v-if="expiringCount > 0" class="alert-banner">
        <i class="pi pi-exclamation-triangle"></i>
        <span>{{ expiringCount }} agreement{{ expiringCount === 1 ? '' : 's' }} expire in the next 30 days</span>
      </div>

      <Tabs v-model:value="activeTab" class="main-tabs">
        <TabList>
          <Tab value="agreements">Agreements</Tab>
          <Tab value="templates">Templates</Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'agreements'" class="agreements-panel">
        <Tabs v-model:value="statusFilter" class="status-tabs">
          <TabList>
            <Tab v-for="tab in statusTabs" :key="tab" :value="tab">
              <span class="tab-label">
                {{ statusLabel(tab) }}
                <small v-if="counts[tab] !== undefined">({{ counts[tab] }})</small>
              </span>
            </Tab>
          </TabList>
        </Tabs>

        <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

        <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
          v-else
          :value="filteredAgreements"
          dataKey="id"
          paginator
          :rows="20"
          striped-rows
          @row-click="openEditAgreement($event.data)"
          
        >
          <template #empty>
            <div class="empty-state">
              <i class="pi pi-file-contract" style="font-size:3rem; color:#64748b;"></i>
              <h3>No agreements yet</h3>
              <p>Create an agreement to manage recurring services for a customer.</p>
              <Button label="+ New Agreement" icon="pi pi-plus" @click="openCreateAgreement" />
            </div>
          </template>
          <Column field="name" header="Agreement" />
          <Column field="customer_name" header="Customer" />
          <Column field="start_date" header="Start">
            <template #body="{ data }">{{ formatDate(data.start_date) }}</template>
          </Column>
          <Column field="end_date" header="End">
            <template #body="{ data }">{{ formatDate(data.end_date) }}</template>
          </Column>
          <Column field="price" header="Price" style="width:120px">
            <template #body="{ data }">{{ formatCurrency(data.price) }}</template>
          </Column>
          <Column field="status" header="Status" style="width:140px">
            <template #body="{ data }">
              <Badge :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
            </template>
          </Column>
          <Column header="Actions" style="width:220px">
            <template #body="{ data }">
              <Button
                v-if="data.status === 'active'"
                text
                size="small"
                icon="pi pi-times"
                severity="danger"
                label="Cancel"
                @click.stop="cancelAgreement(data)"
              />
              <Button text size="small" icon="pi pi-pencil" aria-label="Edit" label="Edit" @click.stop="openEditAgreement(data)" />
            </template>
          </Column>
        </DataTable>
      </div>

      <div v-else class="templates-panel">
        <div v-if="templateLoading" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="templates"
          dataKey="id"
          striped-rows
          paginator
          :rows="10"
          class="templates-table"
        >
          <template #empty>
            <div class="empty-state">
              <i class="pi pi-th-large" style="font-size:3rem; color:#64748b;"></i>
              <h3>No templates yet</h3>
              <p>Save a template to reuse services and pricing.</p>
              <Button label="+ New Template" icon="pi pi-plus" @click="openCreateTemplate" />
            </div>
          </template>
          <Column field="name" header="Template" />
          <Column field="price" header="Price" style="width:140px">
            <template #body="{ data }">{{ formatCurrency(data.price) }}</template>
          </Column>
          <Column header="Services">
            <template #body="{ data }">
              <ul v-if="data.services_included?.length" class="services-list">
                <li v-for="service in data.services_included" :key="`${data.id}-${service}`">{{ service }}</li>
              </ul>
              <span v-else class="text-muted">No services</span>
            </template>
          </Column>
          <Column header="Actions" style="width:220px">
            <template #body="{ data }">
              <Button text size="small" icon="pi pi-pencil" aria-label="Edit" label="Edit" @click.stop="openEditTemplate(data)" />
              <Button
                text
                size="small"
                icon="pi pi-trash"
                severity="danger"
                label="Delete"
                :loading="deletingTemplateId === data.id"
                @click.stop="deleteTemplate(data)"
              />
            </template>
          </Column>
        </DataTable>
      </div>

      <Dialog
        v-model:visible="showAgreementDialog"
        :header="editingAgreement ? `Edit ${editingAgreement.name}` : 'New Service Agreement'"
        modal
        :style="{ width: '620px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Agreement Name *</label>
            <input v-model="agreementForm.name" type="text" class="p-inputtext w-full" />
          </div>
          <div class="form-field">
            <label>Customer Name *</label>
            <input v-model="agreementForm.customer_name" type="text" class="p-inputtext w-full" />
          </div>
          <div class="form-field">
            <label>Template</label>
            <Select
              v-model="agreementForm.template_id"
              :options="templateOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select template"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="agreementForm.status" :options="statusOptionList" class="w-full" />
          </div>
          <div class="form-field">
            <label>Start Date</label>
            <DatePicker v-model="agreementForm.start_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field">
            <label>End Date</label>
            <DatePicker v-model="agreementForm.end_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field">
            <label>Price</label>
            <input v-model.number="agreementForm.price" type="number" min="0" step="0.01" class="p-inputtext w-full" />
          </div>
          <div class="form-field full-width">
            <label>Services Included</label>
            <textarea
              v-model="agreementForm.services_included"
              class="p-inputtextarea w-full"
              rows="3"
              placeholder="List each service on a new line"
            ></textarea>
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <textarea v-model="agreementForm.notes" class="p-inputtextarea w-full" rows="3"></textarea>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showAgreementDialog = false" />
          <Button
            :label="editingAgreement ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="savingAgreement"
            @click="saveAgreement"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showTemplateDialog"
        :header="editingTemplate ? `Edit ${editingTemplate.name}` : 'New Template'"
        modal
        :style="{ width: '520px' }"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Template Name *</label>
            <input v-model="templateForm.name" type="text" class="p-inputtext w-full" />
          </div>
          <div class="form-field">
            <label>Price</label>
            <input v-model.number="templateForm.price" type="number" min="0" step="0.01" class="p-inputtext w-full" />
          </div>
          <div class="form-field full-width">
            <label>Services Included</label>
            <textarea
              v-model="templateForm.services_included"
              class="p-inputtextarea w-full"
              rows="4"
              placeholder="One service per line"
            ></textarea>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showTemplateDialog = false" />
          <Button
            :label="editingTemplate ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="savingTemplate"
            @click="saveTemplate"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Tabs from 'primevue/tabs';
import Toolbar from 'primevue/toolbar';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const statusTabs = ['active', 'expired', 'cancelled', 'all'];
const statusOptionList = [
  { label: 'Active', value: 'active' },
  { label: 'Expired', value: 'expired' },
  { label: 'Cancelled', value: 'cancelled' },
];

const emptyAgreement = () => ({
  customer_id: null,
  customer_name: '',
  template_id: null,
  name: '',
  status: 'active',
  start_date: new Date(),
  end_date: null,
  price: null,
  services_included: '',
  notes: '',
});

const emptyTemplate = () => ({
  name: '',
  price: null,
  services_included: '',
});

const activeTab = ref('agreements');
const statusFilter = ref('active');
const agreements = ref([]);
const loading = ref(false);
const expiringCount = ref(0);
const templates = ref([]);
const templateLoading = ref(false);
const showAgreementDialog = ref(false);
const showTemplateDialog = ref(false);
const agreementForm = ref(emptyAgreement());
const templateForm = ref(emptyTemplate());
const editingAgreement = ref(null);
const editingTemplate = ref(null);
const savingAgreement = ref(false);
const savingTemplate = ref(false);
const deletingTemplateId = ref(null);

const templateOptions = computed(() =>
  templates.value.map((tpl) => ({
    label: tpl.name,
    value: tpl.id,
  }))
);

const counts = computed(() => {
  const result = { all: agreements.value.length };
  statusTabs.forEach((status) => {
    if (status === 'all') return;
    result[status] = agreements.value.filter((item) => item.status === status).length;
  });
  return result;
});

const filteredAgreements = computed(() => {
  if (statusFilter.value === 'all') return agreements.value;
  return agreements.value.filter((item) => item.status === statusFilter.value);
});

function statusLabel(status) {
  if (status === 'all') return 'All';
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function statusSeverity(status) {
  return { active: 'success', expired: 'warning', cancelled: 'danger' }[status] || 'secondary';
}

function formatCurrency(value) {
  if (value === undefined || value === null || value === '') return '—';
  return `$${Number(value).toFixed(2)}`;
}

function formatDate(value) {
  if (!value) return '—';
  if (value instanceof Date) return value.toISOString().split('T')[0];
  if (typeof value === 'string') return value.split('T')[0];
  return '—';
}

function serializeDate(value) {
  if (!value) return null;
  if (value instanceof Date) return value.toISOString().split('T')[0];
  if (typeof value === 'string') return value.split('T')[0];
  return null;
}

function parseServices(text) {
  if (!text) return [];
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

function mapAgreementToForm(data) {
  return {
    customer_id: data.customer_id ?? null,
    customer_name: data.customer_name ?? '',
    template_id: data.template_id ?? null,
    name: data.name ?? '',
    status: data.status ?? 'active',
    start_date: data.start_date ? new Date(data.start_date) : null,
    end_date: data.end_date ? new Date(data.end_date) : null,
    price: data.price ?? null,
    services_included: (data.services_included || []).join('\n'),
    notes: data.notes ?? '',
  };
}

function mapTemplateToForm(template) {
  return {
    name: template.name ?? '',
    price: template.price ?? null,
    services_included: (template.services_included || []).join('\n'),
  };
}

async function loadAgreements() {
  loading.value = true;
  try {
    const data = await api.get('/api/service-agreements?limit=500');
    const list = Array.isArray(data) ? data : data?.items || [];
    agreements.value = list;
  } catch (err) {
    console.error('load_service_agreements_failed', err?.message || err);
    agreements.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadTemplates() {
  templateLoading.value = true;
  try {
    const data = await api.get('/api/service-agreements/templates');
    templates.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    console.error('load_agreement_templates_failed', err?.message || err);
    templates.value = [];
  } finally {
    templateLoading.value = false;
  }
}

async function loadExpiringCount() {
  try {
    const data = await api.get('/api/service-agreements/expiring?days=30');
    expiringCount.value = typeof data === 'number' ? data : data?.count || 0;
  } catch {
    expiringCount.value = 0;
  }
}

function resetAgreementForm() {
  agreementForm.value = emptyAgreement();
}

function resetTemplateForm() {
  templateForm.value = emptyTemplate();
}

function openCreateAgreement() {
  editingAgreement.value = null;
  resetAgreementForm();
  showAgreementDialog.value = true;
}

function openEditAgreement(item) {
  editingAgreement.value = item;
  agreementForm.value = mapAgreementToForm(item);
  showAgreementDialog.value = true;
}

async function saveAgreement() {
  if (!agreementForm.value.name.trim() || !agreementForm.value.customer_name.trim()) return;
  savingAgreement.value = true;
  try {
    const payload = {
      customer_id: agreementForm.value.customer_id,
      customer_name: agreementForm.value.customer_name,
      template_id: agreementForm.value.template_id,
      name: agreementForm.value.name,
      status: agreementForm.value.status,
      start_date: serializeDate(agreementForm.value.start_date),
      end_date: serializeDate(agreementForm.value.end_date),
      price: agreementForm.value.price !== null ? Number(agreementForm.value.price) : null,
      services_included: parseServices(agreementForm.value.services_included),
      notes: agreementForm.value.notes,
    };

    if (editingAgreement.value?.id) {
      await api.patch(`/api/service-agreements/${editingAgreement.value.id}`, payload, {
        successMessage: 'Agreement updated',
      });
    } else {
      await api.post('/api/service-agreements', payload, {
        successMessage: 'Agreement created',
      });
    }

    await Promise.all([loadAgreements(), loadExpiringCount()]);
    showAgreementDialog.value = false;
  } finally {
    savingAgreement.value = false;
  }
}

async function cancelAgreement(item) {
  if (!item?.id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: 'Cancel this agreement?' }))) return;
  try {
    await api.post(`/api/service-agreements/${item.id}/cancel`, null, {
      successMessage: 'Agreement cancelled',
    });
    await Promise.all([loadAgreements(), loadExpiringCount()]);
  } catch (e) {
    // Alerts handled by useApiWithToast
  }
}

function openCreateTemplate() {
  editingTemplate.value = null;
  resetTemplateForm();
  showTemplateDialog.value = true;
}

function openEditTemplate(template) {
  editingTemplate.value = template;
  templateForm.value = mapTemplateToForm(template);
  showTemplateDialog.value = true;
}

async function saveTemplate() {
  if (!templateForm.value.name.trim()) return;
  savingTemplate.value = true;
  try {
    const payload = {
      name: templateForm.value.name,
      price: templateForm.value.price !== null ? Number(templateForm.value.price) : null,
      services_included: parseServices(templateForm.value.services_included),
    };

    if (editingTemplate.value?.id) {
      await api.patch(`/api/service-agreements/templates/${editingTemplate.value.id}`, payload, {
        successMessage: 'Template updated',
      });
    } else {
      await api.post('/api/service-agreements/templates', payload, {
        successMessage: 'Template created',
      });
    }

    await loadTemplates();
    showTemplateDialog.value = false;
  } finally {
    savingTemplate.value = false;
  }
}

async function deleteTemplate(template) {
  if (!template?.id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: 'Delete this template?' }))) return;
  deletingTemplateId.value = template.id;
  try {
    await api.del(`/api/service-agreements/templates/${template.id}`, {
      successMessage: 'Template deleted',
    });
    await loadTemplates();
  } finally {
    deletingTemplateId.value = null;
  }
}

onMounted(async () => {
  await Promise.all([loadAgreements(), loadTemplates(), loadExpiringCount()]);
});
</script>

<style scoped>
.service-agreements-view .toolbar-actions {
  display: flex;
  gap: 0.75rem;
}

.alert-banner {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem 1rem;
  margin-top: 1rem;
  border-radius: 0.35rem;
  background: var(--surface-highlight);
  border: 1px solid var(--border-subtle);
}

.main-tabs {
  margin-top: 1.25rem;
}

.status-tabs {
  margin-bottom: 1rem;
}

.tab-label {
  display: flex;
  gap: 0.35rem;
  align-items: center;
  text-transform: capitalize;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
}

.spinner-wrap.small {
  padding: 1.5rem 0;
}

.empty-state {
  text-align: center;
}

.templates-panel {
  margin-top: 1rem;
}

.templates-table {
  margin-top: 1rem;
}

.services-list {
  margin: 0;
  padding-left: 1rem;
  list-style: disc;
}

.services-list li {
  font-size: 0.9rem;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.form-field.full-width {
  grid-column: 1 / -1;
}

.clickable-row .p-datatable-tbody > tr {
  cursor: pointer;
}

.tab-label small {
  font-size: 0.75rem;
  color: var(--text-secondary);
}

.text-muted {
  color: var(--p-text-muted-color, #9e9e9e);
}

.w-full {
  width: 100%;
}
</style>
