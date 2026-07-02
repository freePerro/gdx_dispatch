<template>
    <section class="maintenance-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Maintenance Plans</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <Button label="+ New Plan" icon="pi pi-plus" class="mr-2" @click="openPlanDialog()" />
            <Button label="+ Enroll Customer" icon="pi pi-user-plus" severity="secondary" @click="openEnrollmentDialog" />
          </div>
        </template>
      </Toolbar>

      <div class="maintenance-alert" v-if="dueLoading">
        <div class="alert-content">
          <ProgressSpinner />
          <span>Checking this month&apos;s visits…</span>
        </div>
      </div>
      <div v-else class="maintenance-alert" :class="{ 'is-empty': !dueThisMonth.length }">
        <div class="alert-heading">
          <strong>{{ dueThisMonth.length }} enrollments due this month</strong>
          <span v-if="!dueThisMonth.length">Nothing scheduled for this month yet.</span>
        </div>
        <ul v-if="dueThisMonth.length" class="due-list">
          <li v-for="item in dueThisMonth" :key="item.id" class="due-row">
            <span class="due-name">{{ item.customer_name || 'Customer' }}</span>
            <span class="due-plan">{{ item.plan_name || 'Plan' }}</span>
            <span class="due-date">{{ formatDate(item.next_service_date) }}</span>
          </li>
        </ul>
      </div>

      <Tabs v-model:value="activeTab" class="maintenance-tabs">
        <TabList>
          <Tab value="plans">
            <span>Plans <small>({{ plans.length }})</small></span>
          </Tab>
          <Tab value="enrollments">
            <span>Enrollments <small>({{ enrollments.length }})</small></span>
          </Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'plans'">
        <div v-if="loadingPlans" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="plans"
          dataKey="id"
          paginator
          :rows="10"
          striped-rows
          class="clickable-row"
        >
          <template #empty>
            <div class="empty-state">
              <i class="pi pi-wrench" style="font-size:3rem; color:#64748b;"></i>
              <h3>No plans yet</h3>
              <p>Create a maintenance plan to automate recurring service work.</p>
              <Button label="+ New Plan" icon="pi pi-plus" @click="openPlanDialog()" />
            </div>
          </template>

          <Column field="name" header="Plan" />
          <Column field="visits_per_year" header="Visits / Year" style="width:140px" />
          <Column field="billing_type" header="Billing" style="width:120px" />
          <Column header="Price" style="width:140px">
            <template #body="{ data }">{{ formatCurrency(data.price) }}</template>
          </Column>
          <Column header="Active" style="width:120px">
            <template #body="{ data }">
              <Badge :value="data.active ? 'Active' : 'Inactive'" :severity="data.active ? 'success' : 'danger'" />
            </template>
          </Column>
          <Column header="Actions" style="width:220px">
            <template #body="{ data }">
              <Button
                icon="pi pi-pencil" aria-label="Edit"
                text
                size="small"
                label="Edit"
                @click.stop="openPlanDialog(data)"
              />
              <Button
                icon="pi pi-trash"
                severity="danger"
                text
                size="small"
                label="Delete"
                @click.stop="confirmDeletePlan(data)"
              />
            </template>
          </Column>
        </DataTable>
      </div>

      <div v-else>
        <div v-if="loadingEnrollments" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
      responsiveLayout="scroll"
          v-else
          :value="enrollments"
          dataKey="id"
          paginator
          :rows="10"
          striped-rows
          class="clickable-row"
        >
          <template #empty>
            <div class="empty-state">
              <i class="pi pi-users" style="font-size:3rem; color:#64748b;"></i>
              <h3>No enrollments</h3>
              <p>Enroll a customer to start tracking recurring visits.</p>
              <Button label="+ Enroll Customer" icon="pi pi-user-plus" @click="openEnrollmentDialog" />
            </div>
          </template>

          <Column field="customer_name" header="Customer" />
          <Column field="plan_name" header="Plan" />
          <Column header="Status" style="width:140px">
            <template #body="{ data }">
              <Badge :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
            </template>
          </Column>
          <Column field="next_service_date" header="Next Visit" style="width:160px">
            <template #body="{ data }">{{ formatDate(data.next_service_date) }}</template>
          </Column>
          <Column field="visits_completed" header="Visits" style="width:120px" />
          <Column header="Actions" style="width:300px">
            <template #body="{ data }">
              <div class="enrollment-actions">
                <Button
                  label="Advance"
                  icon="pi pi-step-forward"
                  size="small"
                  :loading="actionLoadingId === `advance-${data.id}`"
                  :disabled="data.status === 'cancelled'"
                  @click.stop="advanceEnrollment(data)"
                />
                <Button
                  v-if="data.status === 'active'"
                  label="Pause"
                  icon="pi pi-pause"
                  size="small"
                  severity="warn"
                  :loading="actionLoadingId === `pause-${data.id}`"
                  @click.stop="changeStatus(data, 'paused')"
                />
                <Button
                  v-if="data.status === 'paused'"
                  label="Resume"
                  icon="pi pi-play"
                  size="small"
                  severity="success"
                  :loading="actionLoadingId === `resume-${data.id}`"
                  @click.stop="changeStatus(data, 'active')"
                />
                <Button
                  v-if="data.status !== 'cancelled'"
                  label="Cancel"
                  icon="pi pi-times" aria-label="Remove"
                  size="small"
                  severity="danger"
                  :loading="actionLoadingId === `cancel-${data.id}`"
                  @click.stop="changeStatus(data, 'cancelled')"
                />
              </div>
            </template>
          </Column>
        </DataTable>
      </div>

      <Dialog
        v-model:visible="showPlanDialog"
        :header="planEditing ? `Edit ${planEditing.name}` : 'New Maintenance Plan'"
        modal
        :style="{ width: '640px' }"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Name *</label>
            <InputText v-model="planForm.name" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Description</label>
            <Textarea v-model="planForm.description" rows="3" class="w-full" />
          </div>
          <div class="form-field">
            <label>Visits / Year</label>
            <InputNumber v-model="planForm.visits_per_year" mode="decimal" min="1" class="w-full" />
          </div>
          <div class="form-field">
            <label>Billing</label>
            <Select v-model="planForm.billing_type" :options="billingTypeOptions" optionLabel="label" optionValue="value" class="w-full" />
          </div>
          <div class="form-field">
            <label>Price</label>
            <InputNumber v-model="planForm.price" mode="currency" currency="USD" class="w-full" />
          </div>
          <div class="form-field">
            <label>Active</label>
            <Select v-model="planForm.active" :options="booleanOptions" optionLabel="label" optionValue="value" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showPlanDialog = false" />
          <Button :label="planEditing ? 'Save Plan' : 'Create Plan'" icon="pi pi-check" :loading="planSaving" @click="savePlan" />
        </template>
      </Dialog>

      <Dialog v-model:visible="showEnrollmentDialog" header="New Enrollment" modal :style="{ width: '640px' }">
        <div class="form-grid">
          <div class="form-field">
            <label>Customer *</label>
            <Select v-model="enrollmentForm.customer_id" :options="customerOptions" optionLabel="label" optionValue="value" class="w-full" showClear />
          </div>
          <div class="form-field">
            <label>Plan *</label>
            <Select v-model="enrollmentForm.plan_id" :options="planOptions" optionLabel="label" optionValue="value" class="w-full" showClear />
          </div>
          <div class="form-field">
            <label>Start Date</label>
            <DatePicker v-model="enrollmentForm.start_date" date-format="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="enrollmentForm.status" :options="statusOptions" optionLabel="label" optionValue="value" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea v-model="enrollmentForm.notes" rows="3" class="w-full" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showEnrollmentDialog = false" />
          <Button label="Enroll" icon="pi pi-check" :loading="enrollmentSaving" @click="saveEnrollment" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatMoney as formatCurrency } from '../composables/useFormatters';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Textarea from 'primevue/textarea';
import Toolbar from 'primevue/toolbar';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const billingTypeOptions = [
  { label: 'Monthly', value: 'monthly' },
  { label: 'Annual', value: 'annual' },
  { label: 'Per Visit', value: 'per_visit' },
];

const statusOptions = [
  { label: 'Active', value: 'active' },
  { label: 'Paused', value: 'paused' },
  { label: 'Cancelled', value: 'cancelled' },
];

const booleanOptions = [
  { label: 'Active', value: true },
  { label: 'Inactive', value: false },
];

const plans = ref([]);
const enrollments = ref([]);
const dueThisMonth = ref([]);
const customers = ref([]);

const activeTab = ref('plans');
const loadingPlans = ref(true);
const loadingEnrollments = ref(true);
const dueLoading = ref(true);

const showPlanDialog = ref(false);
const planSaving = ref(false);
const planEditing = ref(null);
const planForm = ref({});

const showEnrollmentDialog = ref(false);
const enrollmentSaving = ref(false);
const enrollmentForm = ref({});
const actionLoadingId = ref(null);

const planOptions = computed(() =>
  plans.value.map((plan) => ({ label: plan.name || 'Plan', value: plan.id }))
);

const customerOptions = computed(() =>
  customers.value.map((customer) => ({ label: customer.label, value: customer.value }))
);

function sanitizeList(data) {
  if (Array.isArray(data)) return data;
  if (data?.items) return data.items;
  return [];
}

function buildPlanForm(plan) {
  return {
    name: plan?.name || '',
    description: plan?.description || '',
    visits_per_year: plan?.visits_per_year ?? 1,
    billing_type: plan?.billing_type || 'monthly',
    price: plan?.price ?? 0,
    active: plan?.active ?? true,
  };
}

function emptyEnrollment() {
  return {
    customer_id: null,
    plan_id: null,
    status: 'active',
    start_date: null,
    notes: '',
  };
}

function formatDate(value) {
  if (!value) return '—';
  const normalized = typeof value === 'string' ? value : value?.toString?.() ?? '';
  return normalized.split('T')[0] || normalized;
}

function statusLabel(status) {
  if (!status) return 'Unknown';
  return status.replace('_', ' ').replace(/(^|\s)\S/g, (char) => char.toUpperCase());
}

function statusSeverity(status) {
  return { active: 'success', paused: 'warning', cancelled: 'danger' }[status] || 'info';
}

async function loadPlans() {
  loadingPlans.value = true;
  try {
    const data = await api.get('/api/maintenance/plans');
    plans.value = sanitizeList(data);
  } finally {
    loadingPlans.value = false;
  }
}

async function loadEnrollments() {
  loadingEnrollments.value = true;
  try {
    const data = await api.get('/api/maintenance/enrollments');
    enrollments.value = sanitizeList(data).map((item) => ({
      ...item,
      customer_name: item.customer?.name || item.customer_name || 'Customer',
      plan_name: item.plan?.name || item.plan_name || 'Plan',
    }));
  } finally {
    loadingEnrollments.value = false;
  }
}

async function loadDueThisMonth() {
  dueLoading.value = true;
  try {
    const data = await api.get('/api/maintenance/due-this-month');
    dueThisMonth.value = sanitizeList(data).map((item) => ({
      ...item,
      customer_name: item.customer?.name || item.customer_name || 'Customer',
      plan_name: item.plan?.name || item.plan_name || 'Plan',
    }));
  } finally {
    dueLoading.value = false;
  }
}

async function loadCustomers() {
  try {
    const data = await api.get('/api/customers?per_page=500');
    customers.value = sanitizeList(data).map((customer) => ({
      label: customer.name || customer.company_name || `Customer ${customer.id?.slice?.(0, 6) || customer.id}`,
      value: customer.id,
    }));
  } catch {
    customers.value = [];
  }
}

function openPlanDialog(plan) {
  planEditing.value = plan || null;
  planForm.value = buildPlanForm(plan);
  showPlanDialog.value = true;
}

function openEnrollmentDialog() {
  enrollmentForm.value = emptyEnrollment();
  showEnrollmentDialog.value = true;
}

async function savePlan() {
  if (!planForm.value.name?.trim()) return;
  planSaving.value = true;
  try {
    if (planEditing.value?.id) {
      await api.patch(`/api/maintenance/plans/${planEditing.value.id}`, planForm.value, {
        successMessage: 'Plan updated',
      });
    } else {
      await api.post('/api/maintenance/plans', planForm.value, { successMessage: 'Plan created' });
    }
    await loadPlans();
    showPlanDialog.value = false;
  } finally {
    planSaving.value = false;
  }
}

async function confirmDeletePlan(plan) {
  if (!plan?.id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: `Remove the ${plan.name} plan?` }))) return;
  await api.del(`/api/maintenance/plans/${plan.id}`, { successMessage: 'Plan removed' });
  await loadPlans();
}

async function saveEnrollment() {
  if (!enrollmentForm.value.customer_id || !enrollmentForm.value.plan_id) return;
  enrollmentSaving.value = true;
  try {
    const payload = { ...enrollmentForm.value };
    if (payload.start_date instanceof Date) {
      payload.start_date = payload.start_date.toISOString().split('T')[0];
    }
    await api.post('/api/maintenance/enrollments', payload, { successMessage: 'Enrollment created' });
    showEnrollmentDialog.value = false;
    await Promise.all([loadEnrollments(), loadDueThisMonth()]);
  } finally {
    enrollmentSaving.value = false;
  }
}

async function advanceEnrollment(enrollment) {
  if (!enrollment?.id) return;
  actionLoadingId.value = `advance-${enrollment.id}`;
  try {
    await api.post(`/api/maintenance/enrollments/${enrollment.id}/advance`, null, {
      successMessage: 'Advanced to next visit',
    });
    await Promise.all([loadEnrollments(), loadDueThisMonth()]);
  } finally {
    actionLoadingId.value = null;
  }
}

async function changeStatus(enrollment, status) {
  if (!enrollment?.id) return;
  actionLoadingId.value = `${status}-${enrollment.id}`;
  try {
    await api.patch(`/api/maintenance/enrollments/${enrollment.id}`, { status }, {
      successMessage: 'Enrollment updated',
    });
    await loadEnrollments();
  } finally {
    actionLoadingId.value = null;
  }
}

onMounted(() => {
  Promise.all([loadPlans(), loadEnrollments(), loadDueThisMonth(), loadCustomers()]);
});
</script>

<style scoped>
.maintenance-view {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.toolbar-actions {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.maintenance-alert {
  border-radius: 0.5rem;
  border: 1px solid var(--surface-border);
  background: var(--p-content-hover-background);
  padding: 1rem;
}

.maintenance-alert .alert-content {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  color: var(--p-text-muted-color);
}

.maintenance-alert .alert-heading {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 1rem;
}

.maintenance-alert.is-empty {
  opacity: 0.7;
}

.due-list {
  margin: 0.75rem 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.due-row {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  font-size: 0.95rem;
}

.due-name {
  font-weight: 600;
}

.due-date {
  color: var(--p-text-muted-color);
}

.maintenance-tabs {
  --header-padding: 0 0 0 0;
}

.spinner-wrap.small {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.form-field.full-width {
  grid-column: 1 / -1;
}

.enrollment-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.empty-state {
  text-align: center;
}

.empty-state h3 {
  margin-bottom: 0.35rem;
}
</style>
