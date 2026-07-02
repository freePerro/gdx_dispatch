<template>
    <section class="payments-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Payments</h2>
        </template>
        <template #end>
          <Button
            label="+ Record Payment / Refund"
            icon="pi pi-credit-card"
            class="p-button-outlined"
            data-testid="payments-record-btn"
            @click="openDialog"
          />
        </template>
      </Toolbar>

      <div class="filter-tabs" data-testid="payments-tabs">
        <Button
          v-for="status in paymentTabs"
          :key="status"
          :label="tabLabel(status)"
          :severity="statusFilter === status ? undefined : 'secondary'"
          size="small"
          class="p-button-text"
          :data-testid="`payments-tab-${status}`"
          @click="statusFilter = status"
        />
        <span style="flex:1"></span>
        <Button
          v-for="src in ['all', 'manual', 'quickbooks']"
          :key="src"
          :label="src === 'all' ? 'All sources' : (src === 'quickbooks' ? 'QuickBooks' : 'Manual')"
          :severity="sourceFilter === src ? undefined : 'secondary'"
          size="small"
          class="p-button-text"
          :data-testid="`payments-source-${src}`"
          @click="sourceFilter = src; loadPayments()"
        />
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <DataTable
      responsiveLayout="scroll"
        v-else
        :value="filteredPayments"
        paginator
        :rows="20"
        striped-rows
        class="clickable-row"
        data-testid="payments-table"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-credit-card"
            title="No payments yet"
            message="Payments show up here once invoices get paid, or you can record one manually."
            action-label="Record Payment"
            @action="openDialog"
          />
        </template>
        <Column
          field="date"
          header="Date"
          :style="{ width: '120px' }"
        >
          <!-- Backend returns payment_date (canonical) and created_at; legacy
               manual records used `date`. Try all three. -->
          <template #body="{ data }">{{ formatDate(data.payment_date || data.date || data.created_at) }}</template>
        </Column>
        <Column header="Customer">
          <template #body="{ data }">{{ data.customer_name || data.customer || '—' }}</template>
        </Column>
        <Column header="Invoice" :style="{ width: '160px' }">
          <!-- Prefer the human-readable invoice_number ("1009") over the UUID. -->
          <template #body="{ data }">{{ data.invoice_number || data.invoice_id || '—' }}</template>
        </Column>
        <Column field="amount" header="Amount" :style="{ width: '140px' }" sortable>
          <template #body="{ data }">{{ formatCurrency(data.amount) }}</template>
        </Column>
        <Column field="method" header="Method" :style="{ width: '120px' }" />
        <Column field="status" header="Status" :style="{ width: '140px' }">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="processor_ref" header="Processor Ref" :style="{ width: '200px' }" />
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        header="Record Payment or Refund"
        :modal="true"
        :style="{ width: '520px' }"
        data-testid="payments-dialog"
      >
        <div class="form-grid">
          <div class="form-field">
            <label for="payments-date">Date</label>
            <InputText
              id="payments-date"
              type="date"
              v-model="form.date"
              class="w-full"
              data-testid="payments-date-input"
            />
          </div>
          <div class="form-field">
            <label for="payments-invoice">Invoice</label>
            <AutoComplete
              id="payments-invoice"
              v-model="invoicePick"
              :suggestions="invoiceSuggestions"
              @complete="onInvoiceComplete"
              @item-select="onInvoiceSelect"
              option-label="label"
              dropdown
              forceSelection
              placeholder="Search invoice # or customer…"
              class="w-full"
              data-testid="payments-invoice-input"
            />
            <p v-if="form.invoice_id && !invoicePick" class="muted-cell" style="margin: 0.25rem 0 0; font-size: 0.85rem">
              Selected: {{ form.invoice_id }}
            </p>
          </div>
          <div class="form-field">
            <label for="payments-customer">Customer</label>
            <InputText
              id="payments-customer"
              v-model="form.customer"
              class="w-full"
              data-testid="payments-customer-input"
            />
          </div>
          <div class="form-field">
            <label for="payments-amount">Amount</label>
            <InputNumber
              id="payments-amount"
              v-model="form.amount"
              mode="currency"
              currency="USD"
              class="w-full"
              data-testid="payments-amount-input"
            />
          </div>
          <div class="form-field">
            <label for="payments-method">Method</label>
            <Select
              id="payments-method"
              v-model="form.method"
              :options="methodOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="payments-method-select"
            />
          </div>
          <div class="form-field">
            <label for="payments-status">Status</label>
            <Select
              id="payments-status"
              v-model="form.status"
              :options="statusOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="payments-status-select"
            />
          </div>
          <div class="form-field full-width">
            <label for="payments-processor">Processor Ref</label>
            <InputText
              id="payments-processor"
              v-model="form.processor_ref"
              class="w-full"
              data-testid="payments-processor-input"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button
            label="Save"
            icon="pi pi-check"
            :loading="saving"
            @click="savePayment"
            data-testid="payments-save-btn"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDate, formatMoney as formatCurrency } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import AutoComplete from 'primevue/autocomplete';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();

const payments = ref([]);
const loading = ref(true);
const statusFilter = ref('All');
const showDialog = ref(false);
const saving = ref(false);

const methodOptions = [
  { label: 'Card', value: 'card' },
  { label: 'ACH', value: 'ach' },
  { label: 'Cash', value: 'cash' },
  { label: 'Check', value: 'check' },
];
const statusOptions = [
  { label: 'Pending', value: 'pending' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
  { label: 'Refunded', value: 'refunded' },
];
const paymentTabs = ['All', 'Pending', 'Completed', 'Failed', 'Refunded'];

const emptyForm = () => ({
  date: new Date().toISOString().split('T')[0],
  customer: '',
  invoice_id: '',
  amount: null,
  method: 'card',
  status: 'completed',
  processor_ref: '',
});

const form = ref(emptyForm());

const filteredPayments = computed(() => {
  if (statusFilter.value === 'All') return payments.value;
  return payments.value.filter((payment) => payment.status === statusFilter.value);
});

const counts = computed(() => {
  const map = { All: payments.value.length };
  payments.value.forEach((payment) => {
    map[payment.status] = (map[payment.status] || 0) + 1;
  });
  return map;
});

const statusSeverity = (status) => {
  return {
    Pending: 'warning',
    Completed: 'success',
    Failed: 'danger',
    Refunded: 'info',
  }[status] || 'secondary';
};

const tabLabel = (status) => {
  const label = status.replace('_', ' ');
  const suffix = status === 'All' ? '' : counts.value[status] ? ` (${counts.value[status]})` : '';
  return `${label}${suffix}`;
};

function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

const sourceFilter = ref('all');

const loadPayments = async () => {
  loading.value = true;
  try {
    const qs = sourceFilter.value && sourceFilter.value !== 'all'
      ? `?source=${sourceFilter.value}`
      : '';
    const data = await api.get(`/api/payments${qs}`);
    const list = Array.isArray(data) ? data : data?.items || [];
    payments.value = list.map((p) => ({ ...p, status: capitalize(p.status) || 'Completed' }));
  } finally {
    loading.value = false;
  }
};

// Invoice picker state for the Record Payment dialog (S110 D-S110-payments-invoice-id-freetext).
// We load unpaid invoices once when the dialog opens and let AutoComplete filter
// client-side. On select, we populate form.invoice_id, customer, and amount
// (balance_due) so the operator only has to confirm — not retype.
const invoiceCatalog = ref([]);
const invoicePick = ref(null);
const invoiceSuggestions = ref([]);

function _balanceOf(inv) {
  // Backend may return amount_due, balance, or compute (total - amount_paid).
  if (inv.balance_due != null) return Number(inv.balance_due);
  if (inv.amount_due != null) return Number(inv.amount_due);
  if (inv.balance != null) return Number(inv.balance);
  const total = Number(inv.total ?? inv.amount ?? 0);
  const paid = Number(inv.amount_paid ?? 0);
  return Math.max(total - paid, 0);
}

function _invoiceLabel(inv) {
  const num = inv.invoice_number || inv.number || inv.invoice_id || inv.id?.slice?.(0, 8) || '?';
  const cust = inv.customer_name || inv.customer || '—';
  const bal = _balanceOf(inv);
  const balStr = formatCurrency(bal);
  return `${num} · ${cust} · ${balStr} due`;
}

const loadInvoiceCatalog = async () => {
  try {
    // Pull "open" invoices (anything not Paid). Cap at 500 — if a tenant has
    // more open invoices than that they need a search-only flow, not a picker.
    const data = await api.get('/api/invoices?per_page=500');
    const list = Array.isArray(data) ? data : data?.items || [];
    invoiceCatalog.value = list
      .filter((inv) => String(inv.status || '').toLowerCase() !== 'paid')
      .map((inv) => ({
        ...inv,
        label: _invoiceLabel(inv),
      }));
  } catch (_) {
    invoiceCatalog.value = [];
  }
};

const onInvoiceComplete = (event) => {
  const q = String(event.query || '').toLowerCase().trim();
  if (!q) {
    invoiceSuggestions.value = invoiceCatalog.value.slice(0, 100);
    return;
  }
  invoiceSuggestions.value = invoiceCatalog.value
    .filter((inv) => inv.label.toLowerCase().includes(q))
    .slice(0, 100);
};

const onInvoiceSelect = (event) => {
  const inv = event.value;
  if (!inv) return;
  form.value.invoice_id = inv.invoice_number || inv.number || inv.id;
  if (inv.customer_name || inv.customer) {
    form.value.customer = inv.customer_name || inv.customer;
  }
  const bal = _balanceOf(inv);
  if (bal > 0 && form.value.amount == null) {
    form.value.amount = bal;
  }
};

const openDialog = () => {
  form.value = emptyForm();
  invoicePick.value = null;
  showDialog.value = true;
  loadInvoiceCatalog();
};

const savePayment = async () => {
  if (!form.value.invoice_id?.trim() || !form.value.amount) return;
  saving.value = true;
  try {
    await api.post('/api/payments', { ...form.value });
    showDialog.value = false;
    await loadPayments();
  } finally {
    saving.value = false;
  }
};

onMounted(() => loadPayments());
</script>
