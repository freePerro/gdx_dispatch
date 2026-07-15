<template>
    <section class="expenses-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Expenses</h2>
        </template>
        <template #end>
          <div class="toolbar-actions">
            <label class="toggle-label">
              <span class="toggle-copy">Receipts only</span>
              <ToggleSwitch
                v-model="onlyWithReceipt"
                data-testid="expenses-toggle-receipts"
              />
            </label>
            <Button
              label="+ Log Expense"
              icon="pi pi-plus"
              class="primary-action"
              data-testid="expenses-new-btn"
              @click="openCreate"
            />
          </div>
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button
          v-for="status in statusTabs"
          :key="status"
          :label="tabLabelWithCount(status)"
          :severity="statusFilter === status ? undefined : 'secondary'"
          size="small"
          :data-testid="`expenses-tab-${status}`"
          @click="statusFilter = status"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredExpenses"
        paginator
        :rows="15"
        striped-rows
        row-hover
        @row-click="openEdit($event.data)"
        
      >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-wallet" style="font-size:3rem; color:#64748b;"></i>
            <h3>No expenses yet</h3>
            <p>Log vendor spending so reimbursements stay in sync.</p>
            <Button label="+ Log Expense" data-testid="expenses-empty-btn" @click="openCreate" />
          </div>
        </template>

        <Column field="date" header="Date" style="width:130px">
          <template #body="{ data }">{{ formatDate(data.date) }}</template>
        </Column>
        <Column field="vendor" header="Vendor" />
        <Column field="category" header="Category" />
        <Column field="amount" header="Amount" style="width:140px" sortable>
          <template #body="{ data }">{{ formatMoney(data.amount) }}</template>
        </Column>
        <Column header="Job" style="width:200px">
          <template #body="{ data }">{{ jobLabel(data) }}</template>
        </Column>
        <Column header="Receipt" style="width:130px">
          <template #body="{ data }">
            <a
              v-if="data.receipt_url"
              :href="data.receipt_url"
              target="_blank"
              rel="noreferrer"
              :data-testid="`expense-receipt-${data.id}`"
            >
              View
            </a>
            <span v-else>—</span>
          </template>
        </Column>
        <Column field="status" header="Status" style="width:160px">
          <template #body="{ data }">
            <Tag :value="statusDisplay(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Actions" style="width:120px">
          <template #body="{ data }">
            <Button
              v-tooltip="'Edit'"
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              @click.stop="openEdit(data)"
              :data-testid="`expenses-edit-${data.id}`"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="dialogTitle"
        modal
        :style="{ width: '640px' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Date</label>
            <DatePicker
              v-model="form.date"
              class="w-full"
              dateFormat="yy-mm-dd"
              showIcon
              data-testid="expense-date"
            />
          </div>
          <div class="form-field">
            <label>Vendor</label>
            <InputText v-model="form.vendor" class="w-full" data-testid="expense-vendor" />
          </div>
          <div class="form-field">
            <label>Category</label>
            <Select
              v-model="form.category"
              :options="categoryOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="expense-category"
            />
          </div>
          <div class="form-field">
            <label>Amount</label>
            <InputNumber
              v-model="form.amount"
              mode="currency"
              currency="USD"
              class="w-full"
              data-testid="expense-amount"
            />
          </div>
          <div class="form-field">
            <label>Description</label>
            <Textarea v-model="form.description" rows="3" class="w-full" data-testid="expense-description" />
          </div>
          <div class="form-field">
            <label>Job</label>
            <Select
              v-model="form.job_id"
              :options="jobOptions"
              optionLabel="label"
              optionValue="id"
              filter
              showClear
              class="w-full"
              placeholder="Select job"
              data-testid="expense-job"
            />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select
              v-model="form.status"
              :options="statusTabs"
              class="w-full"
              data-testid="expense-status"
            />
          </div>
          <div class="form-field">
            <label>Receipt URL</label>
            <InputText v-model="form.receipt_url" class="w-full" data-testid="expense-receipt" />
          </div>
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            data-testid="expenses-cancel-btn"
            @click="showDialog = false"
          />
          <Button
            :label="editingExpense ? 'Save' : 'Log Expense'"
            icon="pi pi-check"
            :loading="saving"
            data-testid="expenses-save-btn"
            @click="saveExpense"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate, formatMoney, parseLocalDateString } from '../composables/useFormatters';
import { useToast } from 'primevue/usetoast';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
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
const toast = useToast();
const expenses = ref([]);
const loading = ref(true);
const statusTabs = ['Draft', 'Submitted', 'Approved', 'Reimbursed'];
const statusFilter = ref(statusTabs[0]);
const onlyWithReceipt = ref(false);
const showDialog = ref(false);
const editingExpense = ref(null);
const saving = ref(false);
const jobOptions = ref([]);

// GL S8: the backend validates categories against its canonical list (they
// drive the accounting category→account map), so the dropdown LOADS that
// list from /api/expense-categories instead of hardcoding a divergent
// vocabulary (the old materials/travel/meals list had zero overlap and
// every save would have 422'd). Fallback mirrors the server's canonical
// list for offline/first-paint.
const FALLBACK_CATEGORIES = [
  'Fuel', 'Parts/Supplies', 'Tools/Equipment', 'Advertising',
  'Insurance', 'Vehicle Maintenance', 'Subcontractor', 'Other',
];
const categoryOptions = ref(FALLBACK_CATEGORIES.map((c) => ({ value: c, label: c })));

async function loadCategories() {
  try {
    const list = await api.get('/api/expense-categories');
    if (Array.isArray(list) && list.length) {
      categoryOptions.value = list.map((c) => ({ value: c, label: c }));
    }
  } catch {
    /* keep the fallback */
  }
}

const form = ref(emptyForm());

function emptyForm() {
  return {
    date: null,
    vendor: '',
    category: categoryOptions.value[0].value,
    amount: null,
    description: '',
    job_id: null,
    receipt_url: '',
    status: statusTabs[0],
  };
}

const dialogTitle = computed(() => (editingExpense.value ? 'Edit Expense' : 'Log Expense'));

const counts = computed(() => {
  const map = {};
  expenses.value.forEach((entry) => {
    const key = entry.status || statusTabs[0];
    map[key] = (map[key] || 0) + 1;
  });
  return map;
});

const filteredExpenses = computed(() => {
  return expenses.value
    .filter((entry) => entry.status === statusFilter.value)
    .filter((entry) => (onlyWithReceipt.value ? Boolean(entry.receipt_url) : true));
});

function tabLabel(status) {
  return status.replace('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function tabLabelWithCount(status) {
  const count = counts.value[status] || 0;
  return `${tabLabel(status)}${count ? ` (${count})` : ''}`;
}

function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

function jobLabel(expense) {
  if (expense.job_name) return expense.job_name;
  if (expense.job_number) return expense.job_number;
  const job = jobOptions.value.find((item) => item.id === expense.job_id);
  return job ? job.label : expense.job_id ? `#${expense.job_id}` : '—';
}

function statusSeverity(value) {
  return {
    Draft: 'info',
    Submitted: 'warning',
    Approved: 'success',
    Reimbursed: 'success',
  }[value] || 'info';
}

function statusDisplay(value) {
  if (!value) return 'Draft';
  return value.replace('_', ' ');
}

async function loadExpenses() {
  loading.value = true;
  try {
    const data = await api.get('/api/expenses');
    const list = Array.isArray(data) ? data : data?.items || [];
    expenses.value = list.map((e) => ({ ...e, status: capitalize(e.status) || 'Draft' }));
  } finally {
    loading.value = false;
  }
}

async function loadJobs() {
  try {
    const data = await api.get('/api/jobs?page_size=200');
    const list = Array.isArray(data) ? data : data?.items || [];
    jobOptions.value = list.map((job) => ({
      id: job.id,
      label: `${job.job_number || job.id?.toString().slice(0, 8)} — ${job.customer_name || ''}`.trim(),
    }));
  } catch {
    jobOptions.value = [];
  }
}

function openCreate() {
  editingExpense.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(entry) {
  editingExpense.value = entry;
  form.value = {
    // date-only strings must LOCAL-parse: UTC-parsing here made every
    // edit-save silently decrement the date by a day (audit round 5).
    date: entry.date ? (parseLocalDateString(entry.date) || new Date(entry.date)) : null,
    vendor: entry.vendor || '',
    category: entry.category || categoryOptions.value[0].value,
    amount: entry.amount ?? null,
    description: entry.description || '',
    job_id: entry.job_id || null,
    receipt_url: entry.receipt_url || '',
    status: entry.status || statusTabs[0],
  };
  showDialog.value = true;
}

// The backend field is a DATE; toISOString() shifted local midnight into a
// UTC datetime (2026-07-14T05:00Z in Central) — pydantic 422s it, and an
// evening pick would land on the WRONG day. Serialize the local Y-M-D.
// (Caught in the GL S8 headed browser walk — pre-existing bug.)
function toLocalDateString(d) {
  if (!d) return null;
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

async function saveExpense() {
  if (!form.value.vendor.trim()) return;
  if (!form.value.amount || Number(form.value.amount) <= 0) {
    // backend enforces gt=0 — say so instead of silently doing nothing
    // (audit round 5: a silent no-op also made legacy $0 rows uneditable)
    toast.add({ severity: 'warn', summary: 'Amount required', detail: 'Enter an amount greater than $0.', life: 4000 });
    return;
  }
  saving.value = true;
  const payload = {
    vendor: form.value.vendor,
    category: form.value.category,
    amount: Number(form.value.amount),
    description: form.value.description,
    job_id: form.value.job_id,
    receipt_url: form.value.receipt_url,
    status: form.value.status,
    date: toLocalDateString(form.value.date),
  };
  try {
    if (editingExpense.value) {
      await api.patch(`/api/expenses/${editingExpense.value.id}`, payload, { successMessage: 'Expense updated' });
    } else {
      await api.post('/api/expenses', payload, { successMessage: 'Expense logged' });
    }
    showDialog.value = false;
    await loadExpenses();
  } finally {
    saving.value = false;
  }
}

onMounted(() => {
  loadCategories();
  loadJobs();
  loadExpenses();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}
.filter-tabs {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin: 1rem 0;
}
.toolbar-actions {
  display: flex;
  gap: 0.75rem;
  align-items: center;
}
.toggle-label {
  display: flex;
  gap: 0.4rem;
  align-items: center;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.toggle-copy {
  font-size: 0.85rem;
}
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
.form-field label {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.w-full {
  width: 100%;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
}
.clickable-row {
  cursor: pointer;
}
.empty-state {
  text-align: center;
  padding: 3rem;
  color: var(--p-text-muted-color);
}
.empty-state h3 {
  margin: 1rem 0 0.5rem;
  color: var(--text-color);
}
</style>
