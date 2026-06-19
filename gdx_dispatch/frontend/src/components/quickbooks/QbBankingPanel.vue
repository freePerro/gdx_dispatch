<template>
  <div class="qb-banking-panel">
    <!-- Cash-side balance cards (Bank / CC / Other Current Asset) -->
    <div v-if="cashBalances.length" class="balance-row" data-testid="qb-banking-balances">
      <div v-for="b in cashBalances" :key="b.qb_account_id" class="balance-card">
        <div class="balance-label">{{ b.name }}</div>
        <div class="balance-meta">{{ b.account_type }}</div>
        <div class="balance-value" :class="b.current_balance < 0 ? 'negative' : ''">
          {{ formatCurrency(b.current_balance) }}
        </div>
      </div>
    </div>
    <div v-else-if="!locBalances.length" class="empty balances-empty">
      No bank or credit-card accounts found. Click <strong>Sync Banking</strong> to pull from QuickBooks.
    </div>

    <!-- Lines of Credit — revolving debt, distinct from cash row.
         QBO returns liability balances as NEGATIVE when drawn (debt
         outstanding); we display abs() with sign-aware caption so the
         user reads "$12,000 Drawn" instead of "-$12,000". -->
    <div v-if="locBalances.length" class="loc-section" data-testid="qb-banking-locs">
      <div class="loc-header">Lines of Credit</div>
      <div class="balance-row">
        <div v-for="b in locBalances" :key="b.qb_account_id" class="balance-card loc-card">
          <div class="balance-label">{{ b.name }}</div>
          <div class="balance-meta">{{ locCaption(b.current_balance) }}</div>
          <div class="balance-value loc-value">
            {{ formatCurrency(Math.abs(b.current_balance)) }}
          </div>
        </div>
      </div>
    </div>

    <!-- Term Loans — non-LOC debt (notes payable, mortgages, term loans).
         Same QBO sign convention as LOCs — negative = outstanding, etc. -->
    <div v-if="loanBalances.length" class="loc-section" data-testid="qb-banking-loans">
      <div class="loc-header">Loans</div>
      <div class="balance-row">
        <div v-for="b in loanBalances" :key="b.qb_account_id" class="balance-card loan-card">
          <div class="balance-label">{{ b.name }}</div>
          <div class="balance-meta">{{ loanCaption(b.current_balance) }}</div>
          <div class="balance-value loan-value">
            {{ formatCurrency(Math.abs(b.current_balance)) }}
          </div>
        </div>
      </div>
    </div>

    <!-- Toolbar -->
    <div class="panel-toolbar">
      <label class="date-field" data-testid="qb-banking-startdate-label">
        <span class="date-label">Sync from</span>
        <DatePicker
          v-model="startDate"
          dateFormat="yy-mm-dd"
          :showIcon="true"
          placeholder="(all history)"
          size="small"
          data-testid="qb-banking-startdate"
        />
      </label>
      <Button
        label="Sync Banking"
        icon="pi pi-sync"
        size="small"
        :loading="syncing"
        data-testid="qb-banking-sync-btn"
        @click="syncBanking"
      />
      <Button
        label="Refresh"
        icon="pi pi-refresh"
        size="small"
        severity="secondary"
        :loading="loading"
        data-testid="qb-banking-refresh-btn"
        @click="fetchRows"
      />
    </div>

    <!-- Filter row -->
    <div class="filter-row">
      <span class="p-input-icon-left search-wrap">
        <i class="pi pi-search" />
        <InputText
          v-model="searchInput"
          placeholder="Search account, payee, memo, ID, type…"
          class="search-input"
          data-testid="qb-banking-search"
          @keyup.enter="onSearchEnter"
        />
      </span>
      <MultiSelect
        v-model="kindFilter"
        :options="kindOptions"
        optionLabel="label"
        optionValue="value"
        display="chip"
        placeholder="All types"
        class="kind-filter"
        data-testid="qb-banking-kind-filter"
        @change="applyFilters"
      />
      <Select
        v-model="accountFilter"
        :options="accountOptions"
        optionLabel="label"
        optionValue="value"
        placeholder="All accounts"
        class="account-filter"
        showClear
        data-testid="qb-banking-account-filter"
        @change="applyFilters"
      />
      <DatePicker
        v-model="dateRange"
        selectionMode="range"
        dateFormat="yy-mm-dd"
        :showIcon="true"
        placeholder="Date range"
        size="small"
        class="range-picker"
        data-testid="qb-banking-daterange"
        @date-select="onRangeChange"
        @clear="onRangeChange"
      />
      <Button
        v-if="hasActiveFilters"
        label="Clear"
        icon="pi pi-times"
        size="small"
        severity="secondary"
        text
        @click="clearFilters"
      />
    </div>

    <!-- Server-driven DataTable -->
    <DataTable
      :value="rows"
      :loading="loading"
      lazy
      paginator
      :rows="pageSize"
      :totalRecords="total"
      :first="(page - 1) * pageSize"
      :rowsPerPageOptions="[25, 50, 100, 200]"
      sortMode="single"
      :sortField="orderBy"
      :sortOrder="orderDirVal"
      striped-rows
      data-testid="qb-banking-table"
      @page="onPage"
      @sort="onSort"
    >
      <Column field="txn_date" header="Date" sortable :style="{ minWidth: '120px' }">
        <template #body="{ data }">{{ formatDate(data.txn_date) }}</template>
      </Column>
      <Column field="kind" header="Type" sortable :style="{ width: '130px' }">
        <template #body="{ data }">
          <Tag :value="prettyKind(data.kind)" :severity="kindSeverity(data.kind)" />
        </template>
      </Column>
      <Column field="direction" header="In/Out" :style="{ width: '90px' }">
        <template #body="{ data }">
          <span class="dir" :class="`dir-${data.direction}`" :title="directionLabel(data.direction)">
            <i :class="directionIcon(data.direction)" /> {{ directionLabel(data.direction) }}
          </span>
        </template>
      </Column>
      <Column field="account" header="Account" sortable :style="{ minWidth: '180px' }" />
      <Column field="counterparty" header="Payee / To" sortable :style="{ minWidth: '180px' }" />
      <Column field="amount" header="Amount" sortable :style="{ width: '140px' }" bodyClass="amount-col">
        <template #body="{ data }">
          <span :class="amountClass(data.amount)">{{ formatAmount(data.amount) }}</span>
        </template>
      </Column>
      <Column field="memo" header="Memo" :style="{ minWidth: '200px' }" />
      <Column header="Linked to" :style="{ minWidth: '160px' }">
        <template #body="{ data }">
          <span v-if="data.linked_txn_ids" class="linked-ids" :title="'Source transactions swept into this deposit'">
            {{ data.linked_txn_ids }}
          </span>
          <span v-else class="muted">—</span>
        </template>
      </Column>
      <template #empty>
        <p class="empty">
          <template v-if="hasActiveFilters">No transactions match the current filters.</template>
          <template v-else>No banking transactions cached. Click <strong>Sync Banking</strong>.</template>
        </p>
      </template>
    </DataTable>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import InputText from 'primevue/inputtext';
import MultiSelect from 'primevue/multiselect';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import { useApiWithToast } from '../../composables/useApiWithToast';
import { buildBankingSyncSummary } from './bankingSyncSummary.js';

const api = useApiWithToast();
const balances = ref([]);
const rows = ref([]);
const total = ref(0);
const loading = ref(false);
const syncing = ref(false);
const startDate = ref(null);

// Filters (server-driven)
const searchInput = ref('');
const kindFilter = ref([]);          // multi-select
const accountFilter = ref(null);     // single-select with clear
const dateRange = ref(null);

// Pagination + sort (server-driven)
const page = ref(1);
const pageSize = ref(25);
const orderBy = ref('txn_date');
const orderDir = ref('desc');
const orderDirVal = computed(() => (orderDir.value === 'asc' ? 1 : -1));

const kindOptions = [
  { label: 'Purchases',          value: 'purchase' },
  { label: 'Deposits',           value: 'deposit' },
  { label: 'Transfers',          value: 'transfer' },
  { label: 'Bill Payments',      value: 'bill_payment' },
  { label: 'Sales Receipts',     value: 'sales_receipt' },
  { label: 'Refunds',            value: 'refund_receipt' },
  { label: 'Journal Entries',    value: 'journal_entry' },
  { label: 'Customer Payments',  value: 'customer_payment' },
  { label: 'Vendor Credits',     value: 'vendor_credit' },
];

const cashBalances = computed(() => balances.value.filter(b => b.kind === 'cash' || !b.kind));
const locBalances  = computed(() => balances.value.filter(b => b.kind === 'loc'));
const loanBalances = computed(() => balances.value.filter(b => b.kind === 'loan'));

const accountOptions = computed(() => {
  const names = new Set(balances.value.map(b => b.name).filter(Boolean));
  return [...names].sort().map(n => ({ label: n, value: n }));
});

const hasActiveFilters = computed(() =>
  !!searchInput.value || kindFilter.value.length > 0 || !!accountFilter.value || !!dateRange.value,
);

const toIsoDate = (d) => {
  if (!d) return '';
  const dt = d instanceof Date ? d : new Date(d);
  if (Number.isNaN(dt.getTime())) return '';
  return dt.toISOString().slice(0, 10);
};

const dateBounds = computed(() => {
  if (!dateRange.value) return { start: '', end: '' };
  const [s, e] = Array.isArray(dateRange.value) ? dateRange.value : [dateRange.value, null];
  return { start: toIsoDate(s), end: toIsoDate(e) };
});

const buildQuery = () => {
  const p = new URLSearchParams();
  if (searchInput.value) p.set('search', searchInput.value);
  if (kindFilter.value.length) p.set('kind', kindFilter.value.join(','));
  if (accountFilter.value) p.set('account', accountFilter.value);
  if (dateBounds.value.start) p.set('start_date', dateBounds.value.start);
  if (dateBounds.value.end)   p.set('end_date', dateBounds.value.end);
  p.set('order_by', orderBy.value);
  p.set('order_dir', orderDir.value);
  p.set('page', String(page.value));
  p.set('page_size', String(pageSize.value));
  return p.toString();
};

const fetchBalances = async () => {
  try {
    const b = await api.get('/api/qb/banking/balances');
    balances.value = Array.isArray(b?.items) ? b.items : [];
  } catch {
    balances.value = [];
  }
};

const fetchRows = async () => {
  loading.value = true;
  try {
    const url = `/api/qb/banking/transactions?${buildQuery()}`;
    const t = await api.get(url);
    rows.value = Array.isArray(t?.items) ? t.items : [];
    total.value = Number.isFinite(t?.total) ? t.total : rows.value.length;
  } catch (err) {
    api.toast?.add?.({
      severity: 'error', summary: 'Could not load banking data',
      detail: err?.message || 'Unknown error', life: 4000,
    });
    rows.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
};

const applyFilters = () => {
  page.value = 1;       // reset to first page on any filter change
  fetchRows();
};

const onRangeChange = () => {
  // PrimeVue fires date-select on every click in range mode; only refetch
  // once both ends are set (or the range is cleared).
  const r = dateRange.value;
  if (!r || (Array.isArray(r) && (!r[0] || r[1]))) applyFilters();
};

const clearFilters = () => {
  searchInput.value = '';
  kindFilter.value = [];
  accountFilter.value = null;
  dateRange.value = null;
  applyFilters();
};

const onPage = (ev) => {
  page.value = (ev.page ?? 0) + 1;
  pageSize.value = ev.rows ?? pageSize.value;
  fetchRows();
};

const onSort = (ev) => {
  orderBy.value = ev.sortField || 'txn_date';
  orderDir.value = (ev.sortOrder === 1) ? 'asc' : 'desc';
  page.value = 1;
  fetchRows();
};

const syncBanking = async () => {
  syncing.value = true;
  try {
    const params = new URLSearchParams();
    const iso = toIsoDate(startDate.value);
    if (iso) params.set('start_date', iso);
    const url = '/api/qb/banking/sync' + (params.toString() ? `?${params}` : '');
    const result = await api.post(url);
    const { summary, totalErrors } = buildBankingSyncSummary(result, iso);
    api.toast?.add?.({
      severity: totalErrors > 0 ? 'warn' : 'success',
      summary: totalErrors > 0 ? 'Banking synced with errors' : 'Banking synced',
      detail: summary,
      life: totalErrors > 0 ? 8000 : 4000,
    });
    await fetchBalances();
    await fetchRows();
  } catch (err) {
    api.toast?.add?.({ severity: 'error', summary: 'Sync failed', detail: err?.message || 'Unknown error', life: 4000 });
  } finally {
    syncing.value = false;
  }
};

// Live debounced search — typing pauses 300ms before firing. Enter cancels
// the pending timer and fires immediately so we don't double-fire.
let searchTimer = null;
const onSearchEnter = () => {
  if (searchTimer) { clearTimeout(searchTimer); searchTimer = null; }
  applyFilters();
};
watch(searchInput, () => {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { searchTimer = null; applyFilters(); }, 300);
});

const prettyKind = (k) => ({
  purchase: 'Purchase', deposit: 'Deposit', transfer: 'Transfer',
  bill_payment: 'Bill Pmt', sales_receipt: 'Sales Rcpt', refund_receipt: 'Refund',
  journal_entry: 'Journal', customer_payment: 'Cust Pmt', vendor_credit: 'Vendor Cr',
}[k] || k);
const kindSeverity = (k) => ({
  purchase: 'warning', deposit: 'success', transfer: 'info',
  bill_payment: 'warning', sales_receipt: 'success', refund_receipt: 'danger',
  journal_entry: 'secondary', customer_payment: 'success', vendor_credit: 'info',
}[k] || 'secondary');

const directionLabel = (d) => ({ in: 'In', out: 'Out', transfer: 'Move' }[d] || '—');
const directionIcon  = (d) => ({
  in: 'pi pi-arrow-down', out: 'pi pi-arrow-up', transfer: 'pi pi-arrows-h',
}[d] || 'pi pi-minus');

const formatDate = (value) => {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString();
};

// QBO liability sign convention (LOC, Loan, Credit Card):
//   negative (normal case) → debt outstanding (amount you owe)
//   zero → no outstanding balance (unused LOC / paid-off loan)
//   positive (rare; overpaid past zero) → credit balance with the lender
// We display abs() so the user sees a clean positive dollar figure
// and the caption / red color carry the "this is debt" signal.
const locCaption = (value) => {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return 'Unused';
  if (n > 0) return 'Credit balance';
  return 'Drawn / outstanding';
};
const loanCaption = (value) => {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return 'Paid off';
  if (n > 0) return 'Credit balance';
  return 'Outstanding';
};

const formatCurrency = (value) => {
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  return num.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
};

// Accounting-style: negatives render in parens (color-blind safe), red color is a secondary cue.
const formatAmount = (value) => {
  const num = Number(value);
  if (Number.isNaN(num)) return '—';
  const abs = Math.abs(num).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
  return num < 0 ? `(${abs})` : abs;
};
const amountClass = (value) => {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return 'amt-zero';
  return n < 0 ? 'amt-neg' : 'amt-pos';
};

onMounted(async () => {
  await fetchBalances();
  await fetchRows();
});
</script>

<style scoped>
.qb-banking-panel { padding: 0.5rem 0 1rem; display: flex; flex-direction: column; gap: 0.75rem; }
.balance-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.75rem;
}
.balance-card {
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 0.75rem;
}
.balance-label { font-weight: 600; }
.balance-meta { font-size: 0.75rem; color: var(--p-text-muted-color); margin-bottom: 0.25rem; }
.balance-value { font-size: 1.25rem; font-weight: 600; }
.balance-value.negative { color: var(--p-red-600, #dc2626); }

.loc-section { display: flex; flex-direction: column; gap: 0.5rem; }
.loc-header {
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.loc-card {
  border-left: 4px solid var(--p-red-500, #ef4444);
  background: color-mix(in srgb, var(--p-red-50, #fef2f2) 30%, var(--p-content-background));
}
.loc-value {
  color: var(--p-red-700, #b91c1c);
}
.loan-card {
  border-left: 4px solid var(--p-amber-500, #f59e0b);
  background: color-mix(in srgb, var(--p-amber-50, #fffbeb) 30%, var(--p-content-background));
}
.loan-value {
  color: var(--p-amber-700, #b45309);
}

.balances-empty {
  background: var(--p-surface-100);
  border-radius: 8px;
  padding: 0.75rem;
}
.panel-toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.date-field { display: flex; align-items: center; gap: 0.5rem; margin-right: 0.5rem; }
.date-label { font-size: 0.8125rem; color: var(--p-text-muted-color); }

.filter-row {
  display: grid;
  grid-template-columns: minmax(220px, 1.4fr) minmax(180px, 1fr) minmax(180px, 1fr) minmax(200px, 1fr) auto;
  gap: 0.5rem;
  align-items: center;
}
.search-wrap { position: relative; display: block; }
.search-wrap i { position: absolute; top: 50%; left: 0.6rem; transform: translateY(-50%); color: var(--p-text-muted-color); }
.search-input { width: 100%; padding-left: 2rem; }
.kind-filter, .account-filter, .range-picker { width: 100%; }

.amount-col { text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, monospace; }
.amt-pos  { color: inherit; }
.amt-neg  { color: var(--p-red-600, #dc2626); }
.amt-zero { color: var(--p-text-muted-color); }

.dir { display: inline-flex; align-items: center; gap: 0.25rem; font-size: 0.8125rem; }
.dir-in       { color: var(--p-green-600, #16a34a); }
.dir-out      { color: var(--p-red-600, #dc2626); }
.dir-transfer { color: var(--p-blue-600, #2563eb); }

.linked-ids { font-family: ui-monospace, monospace; font-size: 0.75rem; color: var(--p-text-muted-color); }
.muted { color: var(--p-text-muted-color); }

.empty {
  color: var(--p-text-muted-color);
  text-align: center;
  padding: 1.25rem;
  margin: 0;
}

@media (max-width: 900px) {
  .filter-row { grid-template-columns: 1fr 1fr; }
}
</style>
