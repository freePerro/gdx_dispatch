<template>
  <section class="accounting-ledger-view view-card">
    <header class="view-header">
      <h2 class="page-title">Ledger</h2>
      <p class="page-subtitle">
        Trial balance, profit &amp; loss, balance sheet, and the journal — read straight
        from the GDX general ledger. Cash-basis figures are derived at report time from
        the accrual books, the same way QuickBooks does it.
      </p>
    </header>

    <Tabs v-model:value="activeTab" data-testid="ledger-tabs">
      <TabList>
        <Tab value="trial-balance" data-testid="ledger-tab-trial-balance">Trial Balance</Tab>
        <Tab value="pnl" data-testid="ledger-tab-pnl">Profit &amp; Loss</Tab>
        <Tab value="balance-sheet" data-testid="ledger-tab-balance-sheet">Balance Sheet</Tab>
        <Tab value="journal" data-testid="ledger-tab-journal">Journal</Tab>
      </TabList>
      <TabPanels>
        <!-- ── Trial balance ─────────────────────────────────────────── -->
        <TabPanel value="trial-balance">
          <div class="report-controls">
            <label>As of
              <DatePicker v-model="tbAsOf" dateFormat="yy-mm-dd" showIcon data-testid="tb-as-of" />
            </label>
            <Button label="Run" size="small" data-testid="tb-run" @click="loadTrialBalance" />
            <Tag
              v-if="tb"
              :severity="tb.totals.zero_proof_cents === 0 ? 'success' : 'danger'"
              :value="tb.totals.zero_proof_cents === 0 ? 'Balanced — Σ = 0' : `OUT OF BALANCE: ${fmt(tb.totals.zero_proof_cents)}`"
              data-testid="tb-zero-proof"
            />
          </div>
          <DataTable v-if="tb" :value="tb.rows" size="small" data-testid="tb-table">
            <Column field="code" header="Code" style="width: 6rem" />
            <Column field="name" header="Account" />
            <Column field="type" header="Type" style="width: 8rem" />
            <Column header="Debit" class="amount-col">
              <template #body="{ data }">{{ data.debit_cents ? fmt(data.debit_cents) : '' }}</template>
            </Column>
            <Column header="Credit" class="amount-col">
              <template #body="{ data }">{{ data.credit_cents ? fmt(data.credit_cents) : '' }}</template>
            </Column>
          </DataTable>
          <p v-if="tb" class="totals-row" data-testid="tb-totals">
            Totals — debit {{ fmt(tb.totals.debit_cents) }} · credit {{ fmt(tb.totals.credit_cents) }}
          </p>
        </TabPanel>

        <!-- ── P&L ───────────────────────────────────────────────────── -->
        <TabPanel value="pnl">
          <div class="report-controls">
            <label>From
              <DatePicker v-model="pnlStart" dateFormat="yy-mm-dd" showIcon data-testid="pnl-start" />
            </label>
            <label>To
              <DatePicker v-model="pnlEnd" dateFormat="yy-mm-dd" showIcon data-testid="pnl-end" />
            </label>
            <SelectButton
              v-model="pnlBasis"
              :options="[{ label: 'Accrual', value: 'accrual' }, { label: 'Cash', value: 'cash' }]"
              optionLabel="label"
              optionValue="value"
              data-testid="pnl-basis"
            />
            <Button label="Run" size="small" data-testid="pnl-run" @click="loadPnl" />
          </div>
          <div v-if="pnl" data-testid="pnl-report">
            <h3 class="section-heading">Revenue</h3>
            <table class="report-table">
              <tbody>
                <tr v-for="row in pnl.revenue" :key="row.code">
                  <td class="code-col">{{ row.code }}</td>
                  <td>{{ row.name }}</td>
                  <td class="amount-col">{{ fmt(row.amount_cents) }}</td>
                </tr>
                <tr class="totals">
                  <td colspan="2">Total revenue</td>
                  <td class="amount-col" data-testid="pnl-revenue-total">{{ fmt(pnl.totals.revenue_cents) }}</td>
                </tr>
              </tbody>
            </table>
            <h3 class="section-heading">Expenses</h3>
            <table class="report-table">
              <tbody>
                <tr v-for="row in pnl.expenses" :key="row.code">
                  <td class="code-col">{{ row.code }}</td>
                  <td>{{ row.name }}</td>
                  <td class="amount-col">{{ fmt(row.amount_cents) }}</td>
                </tr>
                <tr class="totals">
                  <td colspan="2">Total expenses</td>
                  <td class="amount-col">{{ fmt(pnl.totals.expense_cents) }}</td>
                </tr>
                <tr class="totals net-income">
                  <td colspan="2">Net income ({{ pnl.basis }})</td>
                  <td class="amount-col" data-testid="pnl-net-income">{{ fmt(pnl.totals.net_income_cents) }}</td>
                </tr>
              </tbody>
            </table>
            <div
              v-if="pnl.skipped_invoices && pnl.skipped_invoices.length"
              class="skip-note"
              data-testid="pnl-skipped-note"
            >
              ⚠ {{ pnl.skipped_invoices.length }} invoice(s) couldn't be attributed on the
              cash basis (invoice data doesn't reconcile) and are excluded:
              <span v-for="(row, i) in pnl.skipped_invoices" :key="row.invoice_number">
                {{ row.invoice_number }}<span v-if="i < pnl.skipped_invoices.length - 1">, </span>
              </span>
            </div>
          </div>
        </TabPanel>

        <!-- ── Balance sheet ─────────────────────────────────────────── -->
        <TabPanel value="balance-sheet">
          <div class="report-controls">
            <label>As of
              <DatePicker v-model="bsAsOf" dateFormat="yy-mm-dd" showIcon data-testid="bs-as-of" />
            </label>
            <Button label="Run" size="small" data-testid="bs-run" @click="loadBalanceSheet" />
            <Tag
              v-if="bs"
              :severity="bs.totals.zero_proof_cents === 0 ? 'success' : 'danger'"
              :value="bs.totals.zero_proof_cents === 0 ? 'Balanced' : `OUT OF BALANCE: ${fmt(bs.totals.zero_proof_cents)}`"
              data-testid="bs-zero-proof"
            />
          </div>
          <div v-if="bs" class="bs-sections" data-testid="bs-report">
            <div v-for="section in bsSections" :key="section.key">
              <h3 class="section-heading">{{ section.label }}</h3>
              <table class="report-table">
                <tbody>
                  <tr v-for="row in bs[section.key]" :key="row.code">
                    <td class="code-col">{{ row.code }}</td>
                    <td>{{ row.name }}</td>
                    <td class="amount-col">{{ fmt(row.amount_cents) }}</td>
                  </tr>
                  <tr class="totals">
                    <td colspan="2">Total {{ section.label.toLowerCase() }}</td>
                    <td class="amount-col">{{ fmt(bs.totals[section.totalKey]) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </TabPanel>

        <!-- ── Journal ───────────────────────────────────────────────── -->
        <TabPanel value="journal">
          <div class="report-controls">
            <Select
              v-model="journalSourceType"
              :options="journalSourceOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All sources"
              showClear
              data-testid="journal-source-filter"
              @change="loadJournal(0)"
            />
          </div>
          <DataTable
            v-if="journal"
            :value="journal.entries"
            v-model:expandedRows="expandedEntries"
            dataKey="id"
            size="small"
            data-testid="journal-table"
          >
            <Column expander style="width: 3rem" />
            <Column field="entry_no" header="#" style="width: 5rem" />
            <Column field="effective_at" header="Date" style="width: 8rem" />
            <Column header="Source">
              <template #body="{ data }">
                <router-link
                  v-if="data.source.invoice_id"
                  :to="`/billing/${data.source.invoice_id}`"
                  class="source-link"
                >
                  {{ data.source.source_type }} · {{ data.source.invoice_number || data.source.invoice_id }}
                </router-link>
                <span v-else-if="data.source.source_type === 'expense'">
                  expense · {{ data.source.vendor }} ({{ data.source.category }})
                </span>
                <span v-else>{{ data.source.source_type || 'manual' }}</span>
              </template>
            </Column>
            <Column header="Status" style="width: 8rem">
              <template #body="{ data }">
                <Tag
                  :severity="data.status === 'posted' ? 'success' : 'secondary'"
                  :value="data.reverses_entry_id ? 'reversal' : data.status"
                />
              </template>
            </Column>
            <template #expansion="{ data }">
              <table class="report-table entry-lines" :data-testid="`journal-lines-${data.entry_no}`">
                <tbody>
                  <tr v-for="(line, i) in data.lines" :key="i">
                    <td class="code-col">{{ line.account_code }}</td>
                    <td>{{ line.account_name }}</td>
                    <td class="memo-col">{{ line.memo }}</td>
                    <td class="amount-col">{{ line.amount_cents > 0 ? fmt(line.amount_cents) : '' }}</td>
                    <td class="amount-col">{{ line.amount_cents < 0 ? fmt(-line.amount_cents) : '' }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-if="data.source.receipts && data.source.receipts.length" class="receipt-links">
                Receipts:
                <a
                  v-for="receipt in data.source.receipts"
                  :key="receipt.id"
                  href="#"
                  class="source-link"
                  @click.prevent="openAuthedFile(receipt.download_url)"
                >{{ receipt.filename }}</a>
              </p>
            </template>
          </DataTable>
          <Paginator
            v-if="journal && journal.total > journal.limit"
            :rows="journal.limit"
            :totalRecords="journal.total"
            :first="journal.offset"
            data-testid="journal-paginator"
            @page="loadJournal($event.first)"
          />
        </TabPanel>
      </TabPanels>
    </Tabs>
  </section>
</template>

<script setup>
import { onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Paginator from 'primevue/paginator';
import Select from 'primevue/select';
import SelectButton from 'primevue/selectbutton';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import { useApi } from '../composables/useApi';
import { openAuthedFile } from '../composables/useAuthedFile';

const { get } = useApi();

const activeTab = ref('trial-balance');

const today = new Date();
const monthStart = new Date(today.getFullYear(), today.getMonth(), 1);

const tbAsOf = ref(today);
const tb = ref(null);
const pnlStart = ref(monthStart);
const pnlEnd = ref(today);
const pnlBasis = ref('accrual');
const pnl = ref(null);
const bsAsOf = ref(today);
const bs = ref(null);
const journal = ref(null);
const journalSourceType = ref(null);
const expandedEntries = ref({});

const bsSections = [
  { key: 'assets', label: 'Assets', totalKey: 'asset_cents' },
  { key: 'liabilities', label: 'Liabilities', totalKey: 'liability_cents' },
  { key: 'equity', label: 'Equity', totalKey: 'equity_cents' },
];
const journalSourceOptions = [
  { label: 'Invoices', value: 'invoice' },
  { label: 'Payments', value: 'payment' },
  { label: 'Adjustments', value: 'adjustment' },
  { label: 'Expenses', value: 'expense' },
];

const fmt = (cents) => {
  const dollars = (cents || 0) / 100;
  return dollars.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
};

const iso = (value) => {
  if (!value) return '';
  const d = value instanceof Date ? value : new Date(value);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

const loadTrialBalance = async () => {
  tb.value = await get(`/api/accounting/reports/trial-balance?as_of=${iso(tbAsOf.value)}`);
};
const loadPnl = async () => {
  pnl.value = await get(
    `/api/accounting/reports/pnl?start=${iso(pnlStart.value)}&end=${iso(pnlEnd.value)}&basis=${pnlBasis.value}`,
  );
};
const loadBalanceSheet = async () => {
  bs.value = await get(`/api/accounting/reports/balance-sheet?as_of=${iso(bsAsOf.value)}`);
};
const loadJournal = async (first = 0) => {
  const params = new URLSearchParams({ limit: '50', offset: String(first) });
  if (journalSourceType.value) params.set('source_type', journalSourceType.value);
  journal.value = await get(`/api/accounting/journal?${params}`);
};

watch(activeTab, (tab) => {
  if (tab === 'pnl' && !pnl.value) loadPnl();
  if (tab === 'balance-sheet' && !bs.value) loadBalanceSheet();
  if (tab === 'journal' && !journal.value) loadJournal();
});

onMounted(loadTrialBalance);
</script>

<style scoped>
.accounting-ledger-view { padding: 1rem 1.25rem 2rem; }
.view-header { margin-bottom: 0.75rem; }
.page-subtitle { color: var(--p-text-muted-color, #64748b); margin-top: 0.25rem; }

.report-controls {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  margin: 0.75rem 0 1rem;
}
.report-controls label { display: flex; align-items: center; gap: 0.5rem; font-weight: 600; }

.section-heading { margin: 1.25rem 0 0.5rem; font-size: 1rem; }

.report-table { width: 100%; border-collapse: collapse; }
.report-table td { padding: 0.35rem 0.6rem; border-bottom: 1px solid var(--p-content-border-color, #e2e8f0); }
.report-table tr.totals td { font-weight: 700; border-top: 2px solid var(--p-content-border-color, #cbd5e1); }
.report-table tr.net-income td { font-size: 1.05rem; }

.code-col { width: 6rem; color: var(--p-text-muted-color, #64748b); font-variant-numeric: tabular-nums; }
.amount-col { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.memo-col { color: var(--p-text-muted-color, #64748b); font-size: 0.85rem; }
.totals-row { font-weight: 600; margin-top: 0.5rem; }
.skip-note {
  margin-top: 0.75rem;
  padding: 0.6rem 0.85rem;
  border: 1px solid var(--p-amber-300, #fcd34d);
  background: var(--p-amber-50, #fffbeb);
  color: var(--p-amber-900, #78350f);
  border-radius: 6px;
  font-size: 0.9rem;
}

.bs-sections { display: grid; gap: 0.5rem; }
.entry-lines { margin: 0.25rem 0 0.5rem 3rem; max-width: 60rem; }
.receipt-links { margin: 0 0 0.75rem 3rem; }
.receipt-links a { margin-right: 0.75rem; }
.source-link { color: var(--p-primary-color, #2563eb); text-decoration: none; }
.source-link:hover { text-decoration: underline; }

@media (max-width: 720px) {
  .entry-lines, .receipt-links { margin-left: 0.5rem; }
  .report-table { display: block; overflow-x: auto; }
}
</style>
