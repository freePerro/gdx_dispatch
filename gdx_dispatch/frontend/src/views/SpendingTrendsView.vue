<template>
  <section class="trends-view view-card">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Spending Trends</h1>
        <Select
          v-model="accountKind"
          :options="typeOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          @change="reload"
        />
        <Select
          v-model="months"
          :options="monthOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          @change="reload"
        />
      </template>
      <template #end>
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          @click="reload"
        />
      </template>
    </Toolbar>

    <div v-if="data?.pnl_last_synced_at" class="freshness-row">
      <i class="pi pi-clock"></i>
      <span>QuickBooks P&amp;L synced <strong>{{ relativeTime(data.pnl_last_synced_at) }}</strong></span>
    </div>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
    <div v-else-if="error" class="error-banner">{{ error }}</div>
    <div v-else-if="(data?.accounts || []).length === 0" class="empty-state-banner">
      <i class="pi pi-info-circle"></i>
      <div>
        <strong>No spending history cached.</strong>
        Open <em>Budget</em> and click <em>Refresh actuals (QB)</em> to pull the Profit &amp; Loss report
        — this view reads from the same cache.
      </div>
    </div>
    <template v-else>
      <!-- One line chart with one line per account -->
      <div class="chart-wrap">
        <Line :data="chartData" :options="chartOptions" />
      </div>

      <!-- Account table — totals over the visible window -->
      <DataTable
        :value="accountRows"
        stripedRows
        responsiveLayout="scroll"
        class="trends-table"
      >
        <Column field="account_name" header="Account">
          <template #body="{ data: row }">
            <span>{{ row.account_name }}</span>
            <i v-if="row.is_anomaly"
               class="pi pi-exclamation-triangle anomaly-flag"
               :title="`Net negative over the window — credits/refunds exceed debits. Check QB classification or recent vendor credits.`"></i>
          </template>
        </Column>
        <Column field="account_type" header="Type" style="width: 180px" />
        <Column header="Total" style="width: 140px">
          <template #body="{ data: row }">
            <span :class="row.is_anomaly ? 'amount-negative' : ''">{{ money(row.total) }}</span>
          </template>
        </Column>
        <Column header="Monthly avg" style="width: 140px">
          <template #body="{ data: row }">
            <span :class="row.is_anomaly ? 'amount-negative' : ''">{{ money(row.avg) }}</span>
          </template>
        </Column>
        <Column header="Months with activity" style="width: 180px">
          <template #body="{ data: row }">{{ row.months_with_spend }} / {{ months }}</template>
        </Column>
        <Column header="" style="width: 180px">
          <template #body="{ data: row }">
            <Button v-if="row.is_anomaly"
                    label="Fix in QuickBooks"
                    icon="pi pi-wrench"
                    size="small"
                    severity="warn"
                    @click="openAnomalyPanel(row)" />
          </template>
        </Column>
      </DataTable>
    </template>

    <!-- Anomaly review drawer -->
    <Dialog
      v-model:visible="anomalyDrawerOpen"
      :header="`Review anomalies — ${anomalyAccountName}`"
      :modal="true"
      :style="{ width: '72rem' }"
    >
      <div v-if="anomalies.loading.value" class="spinner-wrap"><ProgressSpinner /></div>
      <div v-else-if="anomalies.error.value" class="error-banner">{{ anomalies.error.value }}</div>
      <template v-else>
        <p class="muted">
          These transactions were posted to <strong>{{ anomalyAccountName }}</strong> but look miscategorized.
          Purchases/Expenses can be recategorized here with one click — GDX updates QuickBooks via API.
          <br/>
          <strong>Deposits and Transfers open in QB.</strong> Deposits should be matched to ReceivePayment entries
          against the original invoices (changing the category here would sever the A/R linkage);
          Transfers need the QB UI to switch entity type. Click <em>Open in QB</em> to fix those manually.
        </p>

        <div class="filter-bar">
          <span>
            <strong>{{ suspiciousCount }}</strong> suspicious ·
            <strong>{{ legitimateCount }}</strong> legitimate (no rule matched — likely correct as posted)
          </span>
          <label class="toggle-label">
            <input type="checkbox" v-model="showAllAnomalies" />
            Show legitimate too (for manual recategorize)
          </label>
        </div>
        <DataTable
          :value="anomalyTransactions"
          stripedRows responsiveLayout="scroll"
        >
          <Column header="Date" style="width: 110px">
            <template #body="{ data: t }">{{ t.txn_date }}</template>
          </Column>
          <Column header="Type" style="width: 90px">
            <template #body="{ data: t }">{{ t.txn_type }}</template>
          </Column>
          <Column header="Vendor / Memo">
            <template #body="{ data: t }">
              <div>{{ t.vendor_name || '—' }}</div>
              <div class="muted memo-line">{{ t.memo }}</div>
            </template>
          </Column>
          <Column header="Amount" style="width: 130px">
            <template #body="{ data: t }">
              <span :class="Number(t.amount) < 0 ? 'amount-negative' : ''">{{ money(t.amount) }}</span>
            </template>
          </Column>
          <Column header="Suggested" style="width: 280px">
            <template #body="{ data: t }">
              <template v-if="t.suggestion.action === 'open_in_qb'">
                <Tag value="Transfer — open in QB" severity="warn" />
                <div class="muted suggestion-reason">{{ t.suggestion.reason }}</div>
              </template>
              <template v-else-if="t.suggestion.action === 'recategorize'">
                <Select
                  v-model="picks[t.txn_id]"
                  :options="qbAccountOptions"
                  optionLabel="label"
                  optionValue="value"
                  filter
                  class="account-picker"
                />
                <div class="muted suggestion-reason">{{ t.suggestion.reason }}</div>
              </template>
              <template v-else>
                <Select
                  v-model="picks[t.txn_id]"
                  :options="qbAccountOptions"
                  optionLabel="label"
                  optionValue="value"
                  placeholder="Pick an account"
                  filter
                  class="account-picker"
                />
                <div class="muted suggestion-reason">{{ t.suggestion.reason }}</div>
              </template>
            </template>
          </Column>
          <Column header="" style="width: 160px">
            <template #body="{ data: t }">
              <Button
                v-if="t.suggestion.action === 'open_in_qb'"
                label="Open in QB"
                icon="pi pi-external-link"
                size="small"
                severity="secondary"
                @click="anomalies.openInQB(t)"
              />
              <template v-else>
                <Tag
                  v-if="anomalies.applied.value.get(t.txn_id)"
                  :value="`✓ ${anomalies.applied.value.get(t.txn_id).after_account_name || 'Applied'}`"
                  severity="success"
                />
                <Tag
                  v-else-if="anomalies.failed.value.get(t.txn_id)"
                  :value="anomalies.failed.value.get(t.txn_id)"
                  severity="danger"
                />
                <Button
                  v-else
                  label="Apply"
                  icon="pi pi-check"
                  size="small"
                  :loading="anomalies.applying.value.has(t.txn_id)"
                  :disabled="!picks[t.txn_id]"
                  @click="onApply(t)"
                />
              </template>
            </template>
          </Column>
        </DataTable>
      </template>
      <template #footer>
        <Button label="Close" severity="secondary" @click="closeAnomalyPanel" />
        <Button
          v-if="appliedAnyThisSession"
          label="Refresh actuals from QB"
          icon="pi pi-refresh"
          @click="onRefreshActuals"
        />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { Line } from 'vue-chartjs';
import {
  Chart, LineElement, PointElement, LinearScale, CategoryScale,
  Tooltip, Legend, Filler,
} from 'chart.js';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import Select from 'primevue/select';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Tag from 'primevue/tag';
import Dialog from 'primevue/dialog';
import ProgressSpinner from 'primevue/progressspinner';
import { useApi } from '../composables/useApi';
import { useBudgetAnomalies } from '../composables/useBudgetAnomalies';

Chart.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Legend, Filler);

const api = useApi();

// Default "spending" covers Expense + COGS + Other Expense — the full
// money-out picture. The prior "Expense"-only default hid COGS, which is
// the largest spend bucket for service businesses (GDX had $26K of COGS
// invisible behind the dropdown).
const accountKind = ref('spending');
const months = ref(24);
const data = ref(null);
const loading = ref(false);
const error = ref(null);

const typeOptions = [
  { label: 'All spending (Expense + COGS + Other Expense)', value: 'spending' },
  { label: 'Income', value: 'income' },
  { label: 'Everything (P&L)', value: 'all' },
];
const monthOptions = [
  { label: 'Last 6 months', value: 6 },
  { label: 'Last 12 months', value: 12 },
  { label: 'Last 24 months', value: 24 },
  { label: 'Last 36 months', value: 36 },
];

async function reload() {
  loading.value = true;
  error.value = null;
  try {
    const qs = `months=${months.value}&account_kind=${encodeURIComponent(accountKind.value)}`;
    data.value = await api.get(`/api/budgets/trends?${qs}`);
  } catch (e) {
    error.value = e?.message || 'failed to load trends';
    data.value = null;
  } finally {
    loading.value = false;
  }
}

// Build the unified month-axis from the response window (descending → ascending).
const monthAxis = computed(() => {
  const today = new Date();
  const axis = [];
  for (let i = months.value - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth() - i, 1);
    axis.push({ y: d.getFullYear(), m: d.getMonth() + 1, label: d.toLocaleString('en-US', { month: 'short', year: '2-digit' }) });
  }
  return axis;
});

// Generate distinct colors for any N accounts via HSL hue cycling.
// Auditor 2026-05-24 flagged the prior fixed 12-color palette which
// repeated at account #13 — GDX has >20 expense accounts.
function paletteFor(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    // Golden-ratio offset distributes hues evenly even for arbitrary N.
    const hue = Math.round((i * 137.508) % 360);
    // Saturation + lightness mix so adjacent lines feel distinct.
    const sat = 65 + (i % 2) * 10;  // 65 / 75 alternating
    const light = 45 + (i % 3) * 5; // 45 / 50 / 55 cycling
    out.push(`hsl(${hue}, ${sat}%, ${light}%)`);
  }
  return out;
}

const chartData = computed(() => {
  const axis = monthAxis.value;
  const accounts = data.value?.accounts || [];
  const colors = paletteFor(accounts.length);
  const datasets = accounts.map((acct, idx) => {
    const byKey = new Map(acct.series.map(s => [`${s.year}-${s.month}`, Number(s.amount)]));
    const color = colors[idx];
    return {
      label: acct.account_name || acct.qb_account_id,
      data: axis.map(a => byKey.get(`${a.y}-${a.m}`) || 0),
      borderColor: color,
      backgroundColor: color.replace('hsl', 'hsla').replace(')', ', 0.2)'),
      tension: 0.25,
      pointRadius: 2,
    };
  });
  return {
    labels: axis.map(a => a.label),
    datasets,
  };
});

const chartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  interaction: { mode: 'index', intersect: false },
  plugins: {
    legend: { position: 'right' },
    tooltip: {
      callbacks: {
        label(ctx) {
          const val = ctx.parsed.y;
          const fmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
          return `${ctx.dataset.label}: ${fmt.format(val)}`;
        },
      },
    },
  },
  scales: {
    y: {
      beginAtZero: true,
      ticks: {
        callback(value) {
          return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(value);
        },
      },
    },
  },
}));

const accountRows = computed(() => {
  const accts = data.value?.accounts || [];
  return accts.map(a => {
    const amounts = a.series.map(s => Number(s.amount));
    const total = amounts.reduce((s, n) => s + n, 0);
    // "Months with activity" = any nonzero amount (positive OR negative).
    // The earlier `n > 0` filter was inconsistent with the total (which
    // includes negatives) and produced "0 months / $-66K total" on
    // accounts where QBO posted net-negative (vendor credits exceeding
    // debits). Coherent now: total/avg/count all use the same population.
    const nonzero = amounts.filter(n => n !== 0);
    return {
      ...a,
      total,
      avg: nonzero.length ? total / nonzero.length : 0,
      months_with_spend: nonzero.length,
      is_anomaly: total < 0,  // negative expense = credits > debits
    };
  }).sort((a, b) => Math.abs(b.total) - Math.abs(a.total));
});

function money(v) {
  return Number(v ?? 0).toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}
function relativeTime(iso) {
  const t = new Date(iso).getTime();
  const diffMs = Date.now() - t;
  if (diffMs < 60_000) return 'just now';
  const mins = Math.round(diffMs / 60_000);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} hr ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

// ─── Anomaly review drawer ─────────────────────────────────────
const anomalies = useBudgetAnomalies();
const anomalyDrawerOpen = ref(false);
const anomalyAccountName = ref('');
// picks: txn_id -> selected account_id. Pre-populated with `null` for
// every txn in openAnomalyPanel so Vue's reactivity tracks v-model
// updates from the dropdown (new-key writes on a plain object inside
// a ref worked unreliably for "no rule matched" rows — user picked an
// account but Apply stayed disabled).
const picks = ref({});
// Default: show only transactions worth user attention. The "no rule
// matched" rows are typically legitimate (fuel purchases on a fuel
// account). Doug 2026-05-25: "no way to submit it to QuickBooks" was
// really "the bulk of the list has a disabled Apply button — what am I
// supposed to do here." Hiding them by default makes the right rows
// jump out; toggle reveals everything if user wants to recategorize one.
const showAllAnomalies = ref(false);

function openAnomalyPanel(row) {
  anomalyAccountName.value = row.account_name;
  anomalyDrawerOpen.value = true;
  showAllAnomalies.value = false;  // reset to filtered view on each open
  // Reset picks. Will be repopulated once the data lands.
  picks.value = {};
  anomalies.year.value = new Date().getFullYear();
  anomalies.load(row.qb_account_id).then(() => {
    // Pre-fill EVERY transaction's pick slot. Suggested ones get the
    // suggestion; everything else starts null. This guarantees the v-model
    // dropdown writes hit existing reactive keys, so Apply enables
    // correctly when the user picks a category manually.
    const next = {};
    const accts = anomalies.data.value?.accounts || [];
    for (const a of accts) {
      for (const t of (a.transactions || [])) {
        next[t.txn_id] = (
          t.suggestion?.action === 'recategorize' && t.suggestion.suggested_account_id
            ? t.suggestion.suggested_account_id
            : null
        );
      }
    }
    picks.value = next;
  });
}

function closeAnomalyPanel() {
  anomalyDrawerOpen.value = false;
}

const allAnomalyTransactions = computed(() => {
  const accts = anomalies.data.value?.accounts || [];
  return accts.flatMap(a => a.transactions || []);
});

// What's worth user attention: has a non-"unknown" suggestion (either a
// recategorize candidate or an Open-in-QB row). "no rule matched" rows
// are filtered out by default.
const anomalyTransactions = computed(() => {
  const all = allAnomalyTransactions.value;
  if (showAllAnomalies.value) return all;
  return all.filter(t => t.suggestion?.action && t.suggestion.action !== 'unknown');
});

const suspiciousCount = computed(() =>
  allAnomalyTransactions.value.filter(
    t => t.suggestion?.action && t.suggestion.action !== 'unknown',
  ).length,
);
const legitimateCount = computed(() =>
  allAnomalyTransactions.value.length - suspiciousCount.value,
);

const qbAccountOptions = computed(() => {
  const qa = anomalies.data.value?.qb_accounts || [];
  return qa
    .filter(a => a.active !== false)
    .map(a => {
      const bal = a.current_balance != null ? ` · ${money(a.current_balance)}` : '';
      return {
        value: a.qb_account_id,
        label: `${a.name} · ${a.account_type || ''}${bal}`,
      };
    });
});

const appliedAnyThisSession = computed(() => anomalies.applied.value.size > 0);

async function onApply(txn) {
  const target = picks.value[txn.txn_id];
  if (!target) return;
  try { await anomalies.apply(txn, target); }
  catch (_) { /* failed map already populated; useApi toasted */ }
}

async function onRefreshActuals() {
  // Same call the Budget page makes — picks up the recategorization.
  try {
    await api.post(`/api/budgets/refresh-actuals?year=${anomalies.year.value}`);
    // Reload trends with the fresh cache.
    await reload();
    // The drawer's anomaly list is now stale; close it so the user re-opens
    // and sees the updated state.
    anomalyDrawerOpen.value = false;
  } catch { /* toasted */ }
}

onMounted(reload);
</script>

<style scoped>
.trends-view { padding: 1rem; }
.view-heading { margin: 0 1rem 0 0; }
.filter-select { min-width: 12rem; margin-right: .5rem; }
.freshness-row {
  display: flex; align-items: center; gap: .5rem;
  font-size: .85rem;
  padding: .5rem .25rem 1rem .25rem;
  color: var(--text-secondary);
}
.freshness-row strong { color: var(--text-primary); font-weight: 600; }
.chart-wrap {
  height: 480px;
  width: 100%;
  margin: 1rem 0 2rem;
}
.trends-table { margin-top: 1.5rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.error-banner {
  background: var(--color-danger-bg);
  color: var(--color-danger-500);
  border: 1px solid var(--color-danger-border);
  border-radius: .375rem;
  padding: .75rem 1rem;
  margin: .5rem 0;
}
.empty-state-banner {
  display: flex; gap: 1rem; align-items: flex-start;
  background: var(--surface-elevated);
  border: 1px solid var(--color-info-500);
  color: var(--text-primary);
  border-radius: .5rem;
  padding: 1rem 1.25rem;
  margin: 1rem 0;
}
.empty-state-banner i { font-size: 1.25rem; margin-top: 2px; color: var(--color-info-500); }
.empty-state-banner em { font-style: normal; font-weight: 600; }
.anomaly-flag {
  margin-left: .5rem;
  color: var(--color-warning-500);
  cursor: help;
}
.amount-negative {
  color: var(--color-danger-500);
  font-weight: 600;
}
.muted {
  color: var(--text-muted);
  font-size: .85rem;
}
.memo-line {
  margin-top: .15rem;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 28rem;
}
.suggestion-reason {
  font-size: .75rem;
  margin-top: .25rem;
}
.account-picker { width: 100%; min-width: 14rem; }
.filter-bar {
  display: flex; justify-content: space-between; align-items: center;
  gap: 1rem;
  padding: .75rem 1rem;
  background: var(--surface-elevated);
  border: 1px solid var(--border-subtle);
  color: var(--text-primary);
  border-radius: .375rem;
  margin: 1rem 0;
  font-size: .85rem;
}
.toggle-label {
  display: flex; align-items: center; gap: .5rem;
  cursor: pointer;
  user-select: none;
}
.toggle-label input { cursor: pointer; }
</style>
