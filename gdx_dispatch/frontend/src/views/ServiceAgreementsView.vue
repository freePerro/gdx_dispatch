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
          <Column field="customer_name" header="Customer">
            <template #body="{ data }">{{ data.customer_name || '—' }}</template>
          </Column>
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
          <Column field="default_price" header="Price" style="width:140px">
            <!-- #672: the templates serializer emits `default_price` (the column
                 name); only the AGREEMENTS serializer emits `price`. Reading
                 `price` here rendered the formatCurrency placeholder forever. -->
            <template #body="{ data }">{{ formatCurrency(data.default_price) }}</template>
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
            <label for="agreement-name">Agreement Name *</label>
            <input
              id="agreement-name"
              v-model="agreementForm.name"
              type="text"
              class="p-inputtext w-full"
              data-testid="agreement-name"
            />
          </div>
          <div class="form-field">
            <!-- #684: an agreement points at a real customer row. This was a
                 free-text "Customer Name" the API never declared, while the
                 customer_id it requires stayed null — every create was a 422.
                 Same picker as the Jobs create dialog. -->
            <label for="agreement-customer">Customer *</label>
            <Select
              v-model="agreementForm.customer_id"
              inputId="agreement-customer"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              filter
              showClear
              :loading="customersLoading"
              placeholder="Select a customer"
              class="w-full"
              data-testid="agreement-customer-dropdown"
            />
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
          <!-- Status is an edit-only field: a new agreement is always created
               active (the create endpoint has no status field). -->
          <div v-if="editingAgreement" class="form-field">
            <label>Status</label>
            <!-- optionValue: without it the model became the whole {label, value}
                 object, which the PATCH's status pattern rejects. -->
            <Select
              v-model="agreementForm.status"
              :options="statusOptionList"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="agreement-status"
            />
          </div>
          <div class="form-field">
            <label for="agreement-start">Start Date *</label>
            <DatePicker
              v-model="agreementForm.start_date"
              inputId="agreement-start"
              dateFormat="yy-mm-dd"
              class="w-full"
              data-testid="agreement-start-date"
            />
          </div>
          <div class="form-field">
            <label for="agreement-end">End Date *</label>
            <DatePicker
              v-model="agreementForm.end_date"
              inputId="agreement-end"
              dateFormat="yy-mm-dd"
              class="w-full"
              data-testid="agreement-end-date"
            />
          </div>
          <div class="form-field">
            <label for="agreement-price">Price *</label>
            <input
              id="agreement-price"
              v-model.number="agreementForm.price"
              type="number"
              min="0"
              step="0.01"
              class="p-inputtext w-full"
              data-testid="agreement-price"
            />
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
        <p v-if="agreementFormError" class="form-error" role="alert" data-testid="agreement-form-error">
          {{ agreementFormError }}
        </p>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showAgreementDialog = false" />
          <Button
            :label="editingAgreement ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="savingAgreement"
            data-testid="agreement-save"
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
            <input v-model.number="templateForm.default_price" type="number" min="0" step="0.01" class="p-inputtext w-full" />
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
import { computed, onMounted, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatMoney as formatCurrency } from '../composables/useFormatters';
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
  // #672: `default_price` end to end — the column, the serializer and the
  // request models all use that name. The form used to call it `price`, which
  // TemplateIn does not declare, so a typed price was dropped and 0 stored.
  default_price: null,
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
// The edit form as it opened, in payload shape — the PATCH sends only what
// differs from it.
const editBaseline = ref(null);
const editingTemplate = ref(null);
const savingAgreement = ref(false);
const savingTemplate = ref(false);
const deletingTemplateId = ref(null);
const agreementFormError = ref('');
const customers = ref([]);
const customersLoading = ref(false);
// A "pick the customer" message must not outlive the fix: any edit clears it,
// and the next Create re-checks.
watch(agreementForm, () => { agreementFormError.value = ''; }, { deep: true });
// Picking a template offers its price — visible and editable before Create,
// never applied behind the user's back on the server. Only a price the dialog
// itself filled follows a template switch (Gold $299 → Silver $149); a typed
// price is the user's and stays. A $0 template offers nothing: default_price
// is NOT NULL DEFAULT 0, and every template made before #672 stored 0.00, so 0
// there means "never set", and filling it would slip a silent $0 past the
// required-price check.
let prefilledPrice = null;
watch(
  () => agreementForm.value.template_id,
  (templateId) => {
    const form = agreementForm.value;
    const blank = form.price === null || form.price === '' || form.price === undefined;
    const ours = prefilledPrice !== null && Number(form.price) === prefilledPrice;
    if (!blank && !ours) return;
    const tpl = templates.value.find((t) => String(t.id) === String(templateId));
    const offered = tpl ? Number(tpl.default_price) : 0;
    if (offered > 0) {
      form.price = offered;
      prefilledPrice = offered;
    } else if (ours) {
      form.price = null;
      prefilledPrice = null;
    }
  },
);

// Same source and shape as the Jobs create dialog's picker: the whole list
// (per_page=1000 is the endpoint's cap), ids as strings to match the API.
// /api/customers lists LIVE customers only, so an agreement whose customer was
// since deleted would show an empty picker on edit. Its customer rides along
// as an option, and the server's customer_deleted flag — not absence from
// this list, which can fail to load — decides whether it is marked deleted.
const customerOptions = computed(() => {
  // Two live customers with the same name would be two identical rows here —
  // pick the wrong one and the agreement lands on the wrong customer with
  // nothing on screen to say so. Duplicated names carry their email or phone.
  // The hint is the first of email / phone that actually differs across the
  // same-name group (duplicates often share an email), else the id prefix.
  const groups = {};
  customers.value.forEach((c) => { (groups[c.name] = groups[c.name] || []).push(c); });
  const hintField = (group) =>
    ['email', 'phone'].find((f) => {
      const values = group.map((c) => c[f]);
      return values.every(Boolean) && new Set(values).size === values.length;
    });
  const options = customers.value.map((c) => {
    const group = groups[c.name];
    if (group.length < 2) return { label: c.name, value: String(c.id) };
    const field = hintField(group);
    const hint = field ? c[field] : String(c.id).slice(0, 8);
    return { label: `${c.name} — ${hint}`, value: String(c.id) };
  });
  const current = agreementForm.value.customer_id;
  const editing = editingAgreement.value;
  if (current && editing && !options.some((o) => o.value === current)) {
    const name = editing.customer_name || 'Customer';
    options.push({ label: editing.customer_deleted ? `${name} (deleted)` : name, value: current });
  }
  return options;
});

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
  return { active: 'success', expired: 'warn', cancelled: 'danger' }[status] || 'secondary';
}

function formatDate(value) {
  return serializeDate(value) || '—';
}

// An agreement's dates are calendar dates. The DatePicker hands back a local
// midnight, and toISOString() converts that to UTC — so an evening create in a
// US timezone used to save the NEXT day. Read the local calendar parts instead.
function serializeDate(value) {
  if (!value) return null;
  if (value instanceof Date) {
    const mm = String(value.getMonth() + 1).padStart(2, '0');
    const dd = String(value.getDate()).padStart(2, '0');
    return `${value.getFullYear()}-${mm}-${dd}`;
  }
  if (typeof value === 'string') return value.split('T')[0];
  return null;
}

// The inverse: "2026-09-10T00:00:00+00:00" is the calendar date 2026-09-10,
// not a UTC instant — new Date(iso) would show the day before in the picker.
function parseDate(value) {
  if (!value) return null;
  const [y, m, d] = String(value).split('T')[0].split('-').map(Number);
  return y && m && d ? new Date(y, m - 1, d) : null;
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
    customer_id: data.customer_id ? String(data.customer_id) : null,
    template_id: data.template_id ?? null,
    name: data.name ?? '',
    status: data.status ?? 'active',
    start_date: parseDate(data.start_date),
    end_date: parseDate(data.end_date),
    price: data.price ?? null,
    services_included: (data.services_included || []).join('\n'),
    notes: data.notes ?? '',
  };
}

function mapTemplateToForm(template) {
  return {
    name: template.name ?? '',
    default_price: template.default_price ?? null,
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

async function loadCustomers() {
  customersLoading.value = true;
  try {
    const data = await api.get('/api/customers?per_page=1000');
    customers.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    console.error('load_customers_failed', err?.message || err);
    customers.value = [];
  } finally {
    customersLoading.value = false;
  }
}

async function loadExpiringCount() {
  try {
    const data = await api.get('/api/service-agreements/expiring?days=30');
    // The endpoint returns the expiring agreements themselves. Reading a
    // number or {count} left this at 0 forever, so the banner never showed.
    expiringCount.value = Array.isArray(data) ? data.length : 0;
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
  prefilledPrice = null;
  resetAgreementForm();
  agreementFormError.value = '';
  showAgreementDialog.value = true;
}

function openEditAgreement(item) {
  editingAgreement.value = item;
  prefilledPrice = null;
  agreementForm.value = mapAgreementToForm(item);
  editBaseline.value = { ...agreementPayload(agreementForm.value), status: agreementForm.value.status };
  agreementFormError.value = '';
  showAgreementDialog.value = true;
}

// What's missing, in words — the old guard returned silently, so a Create
// click with an empty field did nothing at all.
function agreementFormProblem(form) {
  if (!String(form.name || '').trim()) return 'Give the agreement a name.';
  if (!form.customer_id) return 'Pick the customer this agreement is for.';
  if (!form.start_date) return 'Pick a start date.';
  if (!form.end_date) return 'Pick an end date.';
  if (serializeDate(form.end_date) <= serializeDate(form.start_date)) {
    return 'The end date must be after the start date.';
  }
  // Required, 0 allowed: a blank must never become a silent $0. (The API now
  // refuses a missing price, and the template prefill never offers 0.)
  if (form.price === null || form.price === '' || form.price === undefined || Number.isNaN(Number(form.price))) {
    return 'Enter the price (0 if the agreement is free).';
  }
  if (Number(form.price) < 0) return 'The price cannot be negative.';
  return '';
}

// The form as the API takes it — only keys the API declares (#684).
function agreementPayload(form) {
  return {
    customer_id: form.customer_id,
    template_id: form.template_id,
    name: String(form.name || '').trim(),
    start_date: serializeDate(form.start_date),
    end_date: serializeDate(form.end_date),
    price: Number(form.price),
    services_included: parseServices(form.services_included),
    notes: form.notes ?? '',
  };
}

async function saveAgreement() {
  const form = agreementForm.value;
  agreementFormError.value = agreementFormProblem(form);
  if (agreementFormError.value) return;
  savingAgreement.value = true;
  try {
    const payload = agreementPayload(form);

    if (editingAgreement.value?.id) {
      // Edit sends what the user CHANGED, nothing else (#684 audit). Sending
      // every field rewrote values nobody touched — a stored time of day
      // collapsed to midnight, an empty note became '' — and the audit row
      // then reported those rewrites as the user's edits.
      const full = { ...payload, status: form.status };
      const changed = Object.fromEntries(
        Object.entries(full).filter(
          ([key, value]) => JSON.stringify(value) !== JSON.stringify(editBaseline.value?.[key]),
        ),
      );
      if (Object.keys(changed).length === 0) {
        showAgreementDialog.value = false;
        return;
      }
      await api.patch(`/api/service-agreements/${editingAgreement.value.id}`, changed, {
        successMessage: 'Agreement updated',
      });
    } else {
      await api.post('/api/service-agreements', payload, {
        successMessage: 'Agreement created',
      });
    }

    await Promise.all([loadAgreements(), loadExpiringCount()]);
    showAgreementDialog.value = false;
  } catch {
    // useApiWithToast has already shown the server's reason; the dialog stays
    // open with everything the user typed.
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
      default_price:
        templateForm.value.default_price !== null
          ? Number(templateForm.value.default_price)
          : null,
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
  await Promise.all([loadAgreements(), loadTemplates(), loadExpiringCount(), loadCustomers()]);
});
</script>

<style scoped>
.service-agreements-view .toolbar-actions {
  display: flex;
  gap: 0.75rem;
}

.form-error {
  margin: 0.75rem 0 0;
  color: var(--p-red-500, #ef4444);
  font-size: 0.9rem;
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
