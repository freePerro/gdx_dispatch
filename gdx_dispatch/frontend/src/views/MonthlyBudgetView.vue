<template>
  <section class="monthly-budget-view view-card">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Monthly Budget</h1>
        <Select
          v-model="yearLocal"
          :options="yearOptions"
          class="filter-select"
          @change="onPeriodChange"
        />
        <Select
          v-model="monthLocal"
          :options="monthOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          @change="onPeriodChange"
        />
      </template>
      <template #end>
        <Select
          v-model="accountingMethod"
          :options="accountingOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          :title="accountingTooltip"
          @change="onAccountingMethodChange"
        />
        <Button
          label="Refresh actuals (QB)"
          icon="pi pi-download"
          severity="secondary"
          :loading="refreshingActuals"
          @click="onRefreshActuals"
        />
        <Button
          label="Fill empty"
          icon="pi pi-bolt"
          severity="secondary"
          :loading="seedingBusy"
          @click="seedDialogOpen = true"
        />
        <Button
          label="Classify"
          icon="pi pi-sliders-h"
          severity="secondary"
          @click="onOpenClassify"
        />
        <Button
          label="Add line"
          icon="pi pi-plus"
          @click="openAddDialog"
        />
      </template>
    </Toolbar>

    <!-- Stale-basis warning. Shown after the user flips Cash↔Accrual but
         hasn't re-pulled actuals yet — the cached numbers are still on
         the old basis. -->
    <div v-if="accountingMethodStaleSinceChange" class="stale-banner">
      <i class="pi pi-exclamation-triangle"></i>
      <span>
        Accounting basis changed. Cached actuals are still on the previous basis —
        click <strong>Refresh actuals (QB)</strong> to re-pull from QuickBooks.
      </span>
      <Button
        label="Refresh now"
        icon="pi pi-download"
        size="small"
        @click="onRefreshAfterMethodChange"
        :loading="refreshingActuals"
      />
    </div>

    <!-- Freshness indicator. Reminds the user that "actuals" only includes
         what's been CATEGORIZED in QuickBooks — items in QB's For Review
         tab don't show until someone tells QB what they are. -->
    <div class="freshness-row">
      <i class="pi pi-clock"></i>
      <span v-if="data?.pnl_last_synced_at">
        Actuals last synced from QuickBooks <strong>{{ relativeTime(data.pnl_last_synced_at) }}</strong>
        ({{ formatExact(data.pnl_last_synced_at) }})
      </span>
      <span v-else class="muted">Actuals not yet synced</span>
      <span class="muted spacer">·</span>
      <span class="muted">
        Reflects everything categorized in QB as of that pull. Items still in QB's “For Review” tab are not counted yet.
      </span>
    </div>

    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-label">Total budget</div>
        <div class="kpi-value">{{ money(data?.totals?.budget) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Total actual</div>
        <div class="kpi-value">{{ money(data?.totals?.actual) }}</div>
      </div>
      <div class="kpi" :class="varianceClass">
        <div class="kpi-label">Variance</div>
        <div class="kpi-value">{{ money(data?.totals?.variance) }}</div>
      </div>
      <div class="kpi">
        <div class="kpi-label">Revenue forecast (30d)</div>
        <div class="kpi-value">{{ money(data?.totals?.monthly_revenue_forecast) }}</div>
      </div>
    </div>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <!-- First-run state: QB P&L cache empty for this tenant.
         Auditor 2026-05-24 caught the prior version rendered a blank table
         with no signal — accountant would think the feature was broken. -->
    <div v-else-if="!loading && noPnlCacheYet" class="empty-state-banner">
      <i class="pi pi-info-circle"></i>
      <div>
        <strong>No QuickBooks data cached yet.</strong>
        Click <em>Refresh actuals (QB)</em> above to pull this year's Profit &amp; Loss from QuickBooks.
        That one call populates expense categories, actuals, and enables auto-seed.
      </div>
    </div>

    <DataTable
      v-else
      :value="data?.lines || []"
      stripedRows
      responsiveLayout="scroll"
      class="budget-table"
      :empty-message="emptyMessage"
    >
      <Column field="account_name" header="Account">
        <template #body="{ data: row }">
          <div class="account-cell">
            <span>{{ row.account_name || row.qb_account_id }}</span>
            <Tag v-if="row.account_type" :value="row.account_type" severity="secondary" class="type-tag" />
          </div>
        </template>
      </Column>
      <Column header="Type" style="width: 160px">
        <template #body="{ data: row }">
          <Tag :value="lineTypeLabel(row.line_type)" :severity="lineTypeSeverity(row.line_type)" />
        </template>
      </Column>
      <Column header="3-mo avg" style="width: 130px">
        <template #body="{ data: row }">
          <button class="quickfill" :title="`Use as budget: ${money(row.trailing_3mo_avg)}`"
                  :disabled="row.is_locked || row.line_type === 'percent_of_revenue' || Number(row.trailing_3mo_avg) <= 0"
                  @click="onQuickFill(row, row.trailing_3mo_avg)">
            {{ money(row.trailing_3mo_avg) }}
          </button>
        </template>
      </Column>
      <Column header="6-mo avg" style="width: 130px">
        <template #body="{ data: row }">
          <button class="quickfill" :title="`Use as budget: ${money(row.trailing_6mo_avg)}`"
                  :disabled="row.is_locked || row.line_type === 'percent_of_revenue' || Number(row.trailing_6mo_avg) <= 0"
                  @click="onQuickFill(row, row.trailing_6mo_avg)">
            {{ money(row.trailing_6mo_avg) }}
          </button>
        </template>
      </Column>
      <Column :header="`${monthLabel.slice(0,3)} ${yearLocal - 1}`" style="width: 130px">
        <template #body="{ data: row }">
          <button v-if="row.same_month_last_year !== null" class="quickfill"
                  :title="`Use as budget: ${money(row.same_month_last_year)}`"
                  :disabled="row.is_locked || row.line_type === 'percent_of_revenue'"
                  @click="onQuickFill(row, row.same_month_last_year)">
            {{ money(row.same_month_last_year) }}
          </button>
          <span v-else class="muted">—</span>
        </template>
      </Column>
      <Column header="Budget" style="width: 140px">
        <template #body="{ data: row }">{{ money(row.amount) }}</template>
      </Column>
      <Column header="Actual" style="width: 140px">
        <template #body="{ data: row }">{{ money(row.actual) }}</template>
      </Column>
      <Column header="Variance $" style="width: 140px">
        <template #body="{ data: row }">
          <span :class="varianceCellClass(row.variance)">{{ money(row.variance) }}</span>
        </template>
      </Column>
      <Column header="Variance %" style="width: 110px">
        <template #body="{ data: row }">
          <span v-if="row.variance_pct === null">—</span>
          <span v-else :class="varianceCellClass(row.variance)">{{ row.variance_pct.toFixed(0) }}%</span>
        </template>
      </Column>
      <Column header="Source" style="width: 110px">
        <template #body="{ data: row }">
          <Tag :value="row.source" severity="secondary" />
        </template>
      </Column>
      <Column header="" style="width: 140px">
        <template #body="{ data: row }">
          <Button
            :icon="row.is_locked ? 'pi pi-lock' : 'pi pi-lock-open'"
            text rounded
            :severity="row.is_locked ? 'warn' : 'secondary'"
            :title="row.is_locked ? 'Unlock' : 'Lock'"
            @click="onToggleLock(row)"
          />
          <Button icon="pi pi-pencil" text rounded severity="secondary" title="Edit" @click="openEditDialog(row)" />
          <Button icon="pi pi-trash" text rounded severity="danger" title="Delete" :disabled="row.is_locked" @click="onDelete(row)" />
        </template>
      </Column>
    </DataTable>

    <!-- Auto-seed confirm. NOT a re-seed — only fills empty lines.
         Audit + Doug 2026-05-24 killed the overwrite footgun. -->
    <Dialog v-model:visible="seedDialogOpen" header="Fill empty lines from history" :modal="true" :style="{ width: '34rem' }">
      <p>
        Add a budget line for each expense account that doesn't have one yet for
        <strong>{{ monthLabel }} {{ yearLocal }}</strong>, set to the trailing average
        of the previous {{ seedLookback }} months and snapped to the nearest $10.
      </p>
      <p class="muted">
        Already-set lines are never touched. To reset one, delete it first — the next fill will refill it.
      </p>
      <div class="dialog-field">
        <label>Lookback (months)</label>
        <InputNumber v-model="seedLookback" :min="1" :max="12" />
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="seedDialogOpen = false" />
        <Button label="Fill empty lines" icon="pi pi-bolt" :loading="seedingBusy" @click="onSeed" />
      </template>
    </Dialog>

    <!-- Add line -->
    <Dialog v-model:visible="addDialogOpen" header="Add budget line" :modal="true" :style="{ width: '32rem' }">
      <div class="dialog-field">
        <label>Account (from QuickBooks)</label>
        <Select
          v-model="newLine.qb_account_id"
          :options="availableAccountOptions"
          optionLabel="label"
          optionValue="value"
          placeholder="Select an expense account"
          filter
          showClear
        />
      </div>
      <div class="dialog-field">
        <label>Type</label>
        <Select v-model="newLine.line_type" :options="lineTypeOptions" optionLabel="label" optionValue="value" />
      </div>
      <div class="dialog-field" v-if="newLine.line_type !== 'percent_of_revenue'">
        <label>Monthly amount ($)</label>
        <InputNumber v-model="newLine.amount" mode="currency" currency="USD" :min="0" />
      </div>
      <div class="dialog-field" v-else>
        <label>Percent of monthly revenue</label>
        <InputNumber v-model="newLine.pct_pct" suffix=" %" :min="0" :max="100" :maxFractionDigits="2" />
      </div>
      <div class="dialog-field">
        <label>Notes</label>
        <InputText v-model="newLine.notes" />
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="addDialogOpen = false" />
        <Button label="Add" :disabled="!newLine.qb_account_id" @click="onCreateLine" />
      </template>
    </Dialog>

    <!-- Edit line -->
    <Dialog v-model:visible="editDialogOpen" header="Edit budget line" :modal="true" :style="{ width: '32rem' }">
      <div class="dialog-field" v-if="editLine">
        <label>{{ editLine.account_name }}</label>
      </div>
      <div class="dialog-field">
        <label>Type</label>
        <Select v-model="editPayload.line_type" :options="lineTypeOptions" optionLabel="label" optionValue="value" />
      </div>
      <div class="dialog-field" v-if="editPayload.line_type !== 'percent_of_revenue'">
        <label>Monthly amount ($)</label>
        <InputNumber v-model="editPayload.amount" mode="currency" currency="USD" :min="0" />
      </div>
      <div class="dialog-field" v-else>
        <label>Percent of monthly revenue</label>
        <InputNumber v-model="editPayload.pct_pct" suffix=" %" :min="0" :max="100" :maxFractionDigits="2" />
      </div>
      <div class="dialog-field">
        <label>Notes</label>
        <InputText v-model="editPayload.notes" />
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="editDialogOpen = false" />
        <Button label="Save" @click="onSaveEdit" />
      </template>
    </Dialog>

    <!-- Classify proposals -->
    <Dialog v-model:visible="classifyDialogOpen" header="Classify accounts (fixed vs variable)" :modal="true" :style="{ width: '60rem' }">
      <div v-if="classifyLoading" class="spinner-wrap"><ProgressSpinner /></div>
      <div v-else-if="classifyError" class="error-banner">{{ classifyError }}</div>
      <DataTable
        v-else
        :value="classifyProposals"
        stripedRows
        v-model:selection="classifySelection"
        dataKey="qb_account_id"
        responsiveLayout="scroll"
      >
        <Column selectionMode="multiple" style="width: 3rem" />
        <Column field="account_name" header="Account" />
        <Column field="account_type" header="Type" />
        <Column header="Current">
          <template #body="{ data: row }">
            <Tag v-if="row.current_line_type" :value="lineTypeLabel(row.current_line_type)" severity="secondary" />
            <span v-else>—</span>
          </template>
        </Column>
        <Column header="Proposed">
          <template #body="{ data: row }">
            <Tag :value="lineTypeLabel(row.proposed_line_type)" :severity="lineTypeSeverity(row.proposed_line_type)" />
          </template>
        </Column>
        <Column field="reason" header="Reason" />
      </DataTable>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="classifyDialogOpen = false" />
        <Button :label="`Apply (${classifySelection.length})`" :disabled="classifySelection.length === 0" @click="onApplyClassify" />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useBudget } from '../composables/useBudget';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import Select from 'primevue/select';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Tag from 'primevue/tag';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';

import { useApi } from '../composables/useApi';
const api = useApi();

const {
  year, month, data, loading, error,
  seedingBusy, refreshingActuals,
  classifyProposals, classifyLoading, classifyError,
  load, setMonth, seed, refreshActuals,
  createLine, updateLine, deleteLine, lock, unlock, loadClassify,
} = useBudget();

const yearLocal = ref(year.value);
const monthLocal = ref(month.value);

// Cash vs Accrual. Loaded from tenant settings, persists per tenant.
const accountingMethod = ref('Accrual');
const accountingOptions = [
  { label: 'Accrual basis', value: 'Accrual' },
  { label: 'Cash basis', value: 'Cash' },
];
const accountingTooltip =
  'Accrual counts entered Bills/Purchases regardless of payment status (matches QB default). ' +
  'Cash only counts paid items.';
async function loadAccountingMethod() {
  try {
    const settings = await api.get('/api/settings');
    accountingMethod.value = settings?.qb_accounting_method || 'Accrual';
  } catch {
    // settings page may 403 for non-admin — leave default
  }
}
const accountingMethodStaleSinceChange = ref(false);
async function onAccountingMethodChange() {
  try {
    await api.patch('/api/settings', { qb_accounting_method: accountingMethod.value });
    // Cached qb_pnl_monthly was pulled on the OLD basis. Auditor 2026-05-24
    // flagged this — without a signal, the user toggles + the numbers
    // stay wrong silently. Show a banner offering one-click refresh.
    accountingMethodStaleSinceChange.value = true;
  } catch { /* toasted by useApi */ }
}
async function onRefreshAfterMethodChange() {
  accountingMethodStaleSinceChange.value = false;
  await onRefreshActuals();
}

const monthOptions = [
  { label: 'January', value: 1 }, { label: 'February', value: 2 },
  { label: 'March', value: 3 }, { label: 'April', value: 4 },
  { label: 'May', value: 5 }, { label: 'June', value: 6 },
  { label: 'July', value: 7 }, { label: 'August', value: 8 },
  { label: 'September', value: 9 }, { label: 'October', value: 10 },
  { label: 'November', value: 11 }, { label: 'December', value: 12 },
];

const yearOptions = computed(() => {
  const now = new Date().getFullYear();
  return [now - 2, now - 1, now, now + 1];
});

const monthLabel = computed(() =>
  monthOptions.find(o => o.value === monthLocal.value)?.label || '',
);

const varianceClass = computed(() => {
  const v = Number(data.value?.totals?.variance || 0);
  if (v > 0) return 'kpi-over';
  if (v < 0) return 'kpi-under';
  return '';
});

function varianceCellClass(v) {
  const n = Number(v || 0);
  if (n > 0) return 'variance-over';
  if (n < 0) return 'variance-under';
  return '';
}

const lineTypeOptions = [
  { label: 'Fixed', value: 'fixed' },
  { label: 'Variable', value: 'variable' },
  { label: '% of revenue', value: 'percent_of_revenue' },
];

function lineTypeLabel(t) {
  return lineTypeOptions.find(o => o.value === t)?.label || t;
}
function lineTypeSeverity(t) {
  if (t === 'fixed') return 'info';
  if (t === 'variable') return 'warn';
  if (t === 'percent_of_revenue') return 'success';
  return 'secondary';
}

function money(v) {
  const n = Number(v ?? 0);
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 });
}

const noPnlCacheYet = computed(() =>
  data.value
  && (data.value.lines || []).length === 0
  && (data.value.available_accounts || []).length === 0,
);

const emptyMessage = computed(() =>
  noPnlCacheYet.value
    ? 'No QuickBooks P&L data cached yet. Click "Refresh actuals (QB)".'
    : 'No budget lines yet. Click "Auto-seed" to populate from trailing averages, or "Add line".',
);

const availableAccountOptions = computed(() => {
  const accts = data.value?.available_accounts || [];
  return accts.map(a => ({
    value: a.qb_account_id,
    label: `${a.account_name || a.qb_account_id} · ${a.account_type || ''}`,
  }));
});

async function onPeriodChange() {
  await setMonth(yearLocal.value, monthLocal.value);
}

// ----- Refresh + seed -----

async function onRefreshActuals() {
  try { await refreshActuals(); }
  catch (e) { /* useApi toasts the error */ }
}

const seedDialogOpen = ref(false);
const seedLookback = ref(3);

async function onSeed() {
  try {
    // Composable still accepts overwrite arg for back-compat; we pass false
    // explicitly — server no longer honors it either way.
    await seed(seedLookback.value, false);
    seedDialogOpen.value = false;
  } catch (e) { /* toasted */ }
}

// ----- Add line -----

const addDialogOpen = ref(false);
const newLine = ref({
  qb_account_id: null,
  line_type: 'fixed',
  amount: 0,
  pct_pct: 0,
  notes: '',
});

function openAddDialog() {
  newLine.value = { qb_account_id: null, line_type: 'fixed', amount: 0, pct_pct: 0, notes: '' };
  addDialogOpen.value = true;
}

async function onCreateLine() {
  const acct = (data.value?.available_accounts || []).find(a => a.qb_account_id === newLine.value.qb_account_id);
  const payload = {
    year: yearLocal.value,
    month: monthLocal.value,
    qb_account_id: newLine.value.qb_account_id,
    account_name: acct?.account_name || null,
    line_type: newLine.value.line_type,
    amount: newLine.value.line_type === 'percent_of_revenue' ? 0 : Number(newLine.value.amount || 0),
    pct_of_revenue: newLine.value.line_type === 'percent_of_revenue'
      ? Number((newLine.value.pct_pct || 0) / 100)
      : null,
    notes: newLine.value.notes || null,
  };
  try {
    await createLine(payload);
    addDialogOpen.value = false;
  } catch (e) { /* toasted */ }
}

// ----- Edit line -----

const editDialogOpen = ref(false);
const editLine = ref(null);
const editPayload = ref({ line_type: 'fixed', amount: 0, pct_pct: 0, notes: '' });

function openEditDialog(row) {
  editLine.value = row;
  editPayload.value = {
    line_type: row.line_type,
    amount: Number(row.amount || 0),
    pct_pct: row.pct_of_revenue !== null ? Number(row.pct_of_revenue) * 100 : 0,
    notes: row.notes || '',
  };
  editDialogOpen.value = true;
}

async function onSaveEdit() {
  if (!editLine.value) return;
  const p = editPayload.value;
  const payload = {
    line_type: p.line_type,
    notes: p.notes || null,
  };
  if (p.line_type === 'percent_of_revenue') {
    payload.pct_of_revenue = Number((p.pct_pct || 0) / 100);
    payload.amount = 0;
  } else {
    payload.amount = Number(p.amount || 0);
    payload.pct_of_revenue = null;
  }
  try {
    await updateLine(editLine.value.id, payload);
    editDialogOpen.value = false;
  } catch (e) { /* toasted */ }
}

async function onDelete(row) {
  if (!confirm(`Delete budget line for ${row.account_name || row.qb_account_id}?`)) return;
  await deleteLine(row.id);
}

async function onToggleLock(row) {
  if (row.is_locked) await unlock(row.id);
  else await lock(row.id);
}

// ----- Classify -----

const classifyDialogOpen = ref(false);
const classifySelection = ref([]);

async function onOpenClassify() {
  classifyDialogOpen.value = true;
  classifySelection.value = [];
  await loadClassify(6);
}

async function onApplyClassify() {
  // Each accepted proposal becomes a PATCH on the line for the CURRENT
  // month. If no line exists for an account in the current month, create
  // one at $0 with the proposed type — the user can then auto-seed or
  // hand-edit. This keeps "Classify" useful for accounts not in a budget yet.
  const linesByAcct = new Map(
    (data.value?.lines || []).map(l => [l.qb_account_id, l]),
  );
  for (const prop of classifySelection.value) {
    const existing = linesByAcct.get(prop.qb_account_id);
    if (existing) {
      await updateLine(existing.id, { line_type: prop.proposed_line_type });
    } else {
      await createLine({
        year: yearLocal.value,
        month: monthLocal.value,
        qb_account_id: prop.qb_account_id,
        account_name: prop.account_name,
        line_type: prop.proposed_line_type,
        amount: 0,
        pct_of_revenue: null,
      });
    }
  }
  classifyDialogOpen.value = false;
}

// Quick-fill: copy a history value into the budget for that row.
// Refuses to operate on percent_of_revenue lines — those resolve their
// effective dollar amount from revenue, so writing a fixed dollar would
// either be ignored or silently flip the line type. The auditor caught
// the prior buried-ternary form. Cleaner: do nothing, surface why.
async function onQuickFill(row, value) {
  if (row.is_locked) return;
  if (row.line_type === 'percent_of_revenue') {
    // useApi's toast handler isn't reachable from here; surface inline.
    alert('This line is a percent-of-revenue line. To use a fixed dollar amount, edit the line and change Type to Fixed first.');
    return;
  }
  const n = Number(value || 0);
  if (n <= 0) return;
  try {
    await updateLine(row.id, { amount: n });
  } catch { /* toasted */ }
}

// Freshness helpers (relative + exact display).
function relativeTime(iso) {
  if (!iso) return '';
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
function formatExact(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    });
  } catch { return iso; }
}

onMounted(async () => {
  await Promise.all([load(), loadAccountingMethod()]);
});
</script>

<style scoped>
.monthly-budget-view {
  padding: 1rem;
}
.view-heading { margin: 0 1rem 0 0; }
.filter-select { min-width: 8rem; margin-right: .5rem; }
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin: 1rem 0; }
.kpi {
  background: var(--p-surface-50, #f8fafc);
  border: 1px solid var(--p-surface-200, #e2e8f0);
  border-radius: .5rem;
  padding: 1rem;
}
.kpi-over { background: rgba(239, 68, 68, .08); border-color: rgba(239, 68, 68, .25); }
.kpi-under { background: rgba(34, 197, 94, .08); border-color: rgba(34, 197, 94, .25); }
.kpi-label { font-size: .75rem; color: var(--p-text-color-secondary, #64748b); text-transform: uppercase; letter-spacing: .05em; }
.kpi-value { font-size: 1.5rem; font-weight: 600; margin-top: .25rem; }
.variance-over { color: var(--p-red-600, #dc2626); font-weight: 600; }
.variance-under { color: var(--p-green-600, #16a34a); font-weight: 600; }
.account-cell { display: flex; align-items: center; gap: .5rem; }
.type-tag { font-size: .7rem; }
.error-banner {
  background: rgba(239, 68, 68, .1);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid rgba(239, 68, 68, .25);
  border-radius: .375rem;
  padding: .75rem 1rem;
  margin: .5rem 0;
}
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.dialog-field { display: flex; flex-direction: column; gap: .25rem; margin-bottom: 1rem; }
.dialog-field label { font-size: .85rem; color: var(--p-text-color-secondary, #64748b); }
.budget-table { margin-top: 1rem; }
.empty-state-banner {
  display: flex;
  gap: 1rem;
  align-items: flex-start;
  background: var(--p-blue-50, #eff6ff);
  border: 1px solid var(--p-blue-200, #bfdbfe);
  color: var(--p-blue-900, #1e3a8a);
  border-radius: .5rem;
  padding: 1rem 1.25rem;
  margin: 1rem 0;
}
.empty-state-banner i { font-size: 1.25rem; margin-top: 2px; }
.empty-state-banner em { font-style: normal; font-weight: 600; }
.freshness-row {
  display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
  font-size: .85rem;
  padding: .5rem .25rem;
  color: var(--p-text-color-secondary, #64748b);
}
.freshness-row strong { color: var(--p-text-color, #1e293b); font-weight: 600; }
.freshness-row .spacer { margin: 0 .25rem; }
.muted { color: var(--p-text-color-secondary, #94a3b8); }
.quickfill {
  background: transparent;
  border: 1px dashed transparent;
  color: var(--p-text-color, inherit);
  cursor: pointer;
  padding: 2px 6px;
  border-radius: 4px;
  font: inherit;
  text-align: right;
  width: 100%;
}
.quickfill:hover:not(:disabled) {
  background: var(--p-blue-50, #eff6ff);
  border-color: var(--p-blue-200, #bfdbfe);
}
.quickfill:disabled {
  cursor: default;
  color: var(--p-text-color-secondary, #94a3b8);
}
.stale-banner {
  display: flex; gap: 1rem; align-items: center;
  background: var(--p-amber-50, #fffbeb);
  border: 1px solid var(--p-amber-200, #fde68a);
  color: var(--p-amber-900, #78350f);
  border-radius: .5rem;
  padding: .75rem 1.25rem;
  margin: 1rem 0;
}
.stale-banner i { font-size: 1.25rem; }
</style>
