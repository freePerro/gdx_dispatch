<template>
  <div class="overhead-view">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Overhead</h1>
      </template>
      <template #end>
        <Select
          v-model="horizon"
          :options="horizonOptions"
          optionLabel="label"
          optionValue="value"
          class="filter-select"
          @change="onHorizonChange"
        />
        <Button label="Add obligation" icon="pi pi-plus" @click="openCreate" />
        <Button label="Refresh" icon="pi pi-refresh" severity="secondary" @click="o.refreshAll" />
      </template>
    </Toolbar>

    <Message severity="info" :closable="false" class="scope-note">
      This is the recurring overhead you must pay to keep the doors open — an
      <strong>outflow</strong> projection, not runway. It steps down as loans pay off and up on
      renewals. Completeness depends on what's entered here; variable costs are projected flat.
    </Message>

    <Message v-if="o.listError.value || o.projectionError.value" severity="error" :closable="false" class="scope-note">
      {{ o.listError.value || o.projectionError.value }}
    </Message>

    <!-- KPI cards -->
    <div class="kpi-row">
      <Card class="kpi">
        <template #title>Current monthly overhead</template>
        <template #content>
          <div class="kpi-value">{{ money(o.currentMonthlyTotal.value) }}</div>
          <div class="kpi-sub">{{ money(annualized) }} / yr</div>
        </template>
      </Card>
      <Card class="kpi">
        <template #title>In {{ horizon }} months</template>
        <template #content>
          <div class="kpi-value">{{ money(horizonTotal) }}</div>
          <div class="kpi-sub" :class="{ down: horizonDelta < 0, up: horizonDelta > 0 }">
            {{ horizonDelta === 0 ? 'no change' : (horizonDelta < 0 ? '▼ ' : '▲ ') + money(Math.abs(horizonDelta)) + ' / mo' }}
          </div>
        </template>
      </Card>
      <Card class="kpi">
        <template #title>Tracked obligations</template>
        <template #content>
          <div class="kpi-value">{{ o.obligations.value.length }}</div>
          <div class="kpi-sub">{{ estimateCount }} estimated</div>
        </template>
      </Card>
    </div>

    <!-- Projection chart -->
    <Card class="panel">
      <template #title>Projected monthly overhead</template>
      <template #content>
        <div v-if="o.projectionLoading.value" class="muted">Loading projection…</div>
        <div v-else-if="!hasMonths" class="muted">
          Add obligations to see your overhead projected forward.
        </div>
        <div v-else class="chart-wrap">
          <Line :data="chartData" :options="chartOptions" />
        </div>

        <ul v-if="stepDowns.length" class="stepdowns">
          <li v-for="sd in stepDowns" :key="sd.label">
            <i class="pi pi-arrow-down" />
            <strong>{{ formatMonth(sd.label) }}</strong>: overhead drops
            {{ money(sd.drop) }}/mo as {{ sd.ended.join(', ') }} ends.
          </li>
        </ul>
      </template>
    </Card>

    <!-- Obligations table -->
    <Card class="panel">
      <template #title>Obligations</template>
      <template #content>
        <DataTable :value="o.obligations.value" stripedRows responsiveLayout="scroll" :loading="o.listLoading.value">
          <template #empty>No overhead obligations yet.</template>
          <Column field="label" header="Label">
            <template #body="{ data }">
              {{ data.label }}
              <Tag v-if="data.is_estimate" value="est" severity="warn" class="ml" />
            </template>
          </Column>
          <Column field="category" header="Category">
            <template #body="{ data }"><Tag :value="data.category" severity="secondary" /></template>
          </Column>
          <Column header="Amount">
            <template #body="{ data }">{{ money(data.amount) }} / {{ data.cadence }}</template>
          </Column>
          <Column header="Monthly equiv.">
            <template #body="{ data }">{{ money(monthlyEquiv(data)) }}</template>
          </Column>
          <Column header="Ends">
            <template #body="{ data }">{{ data.end_date ? formatDate(data.end_date) : (data.term_total_occurrences ? data.term_total_occurrences + ' payments' : '—') }}</template>
          </Column>
          <Column header="" style="width: 100px">
            <template #body="{ data }">
              <Button icon="pi pi-pencil" text rounded @click="openEdit(data)" aria-label="Edit" />
              <Button icon="pi pi-trash" text rounded severity="danger" @click="onDelete(data)" aria-label="Delete" />
            </template>
          </Column>
        </DataTable>
      </template>
    </Card>

    <!-- Create / edit dialog -->
    <Dialog v-model:visible="dialogOpen" modal :header="editingId ? 'Edit obligation' : 'Add obligation'" :style="{ width: '560px' }">
      <div class="form-grid">
        <label class="row">
          <span>Label</span>
          <InputText v-model="form.label" placeholder="e.g. Truck loan" />
        </label>
        <label class="row">
          <span>Category</span>
          <Select v-model="form.category" :options="o.categories.value" placeholder="Select" />
        </label>
        <label class="row">
          <span>Amount</span>
          <InputNumber v-model="form.amount" mode="currency" currency="USD" :min="0" />
        </label>
        <label class="row">
          <span>Cadence</span>
          <Select v-model="form.cadence" :options="o.cadences.value" />
        </label>
        <label class="row">
          <span>Cost type</span>
          <Select v-model="form.cost_type" :options="o.costTypes.value" />
        </label>
        <label class="row">
          <span>Start date</span>
          <input type="date" v-model="form.start_date" class="date-input" />
        </label>
        <label class="row">
          <span>End / payoff date</span>
          <input type="date" v-model="form.end_date" class="date-input" />
        </label>
        <label class="row">
          <span>or payments remaining</span>
          <InputNumber v-model="form.term_total_occurrences" :min="1" :max="600" placeholder="e.g. 24" />
        </label>
        <label class="row check">
          <ToggleSwitch v-model="form.is_estimate" />
          <span>This amount is an estimate (e.g. payroll run-rate)</span>
        </label>
        <label class="row">
          <span>Notes</span>
          <Textarea v-model="form.notes" rows="2" autoResize />
        </label>

        <div class="changes">
          <div class="changes-head">
            <span>Scheduled changes (renewals / escalations)</span>
            <Button label="Add" icon="pi pi-plus" text size="small" @click="addChange" />
          </div>
          <div v-for="(c, i) in form.scheduled_changes" :key="i" class="change-row">
            <input type="date" v-model="c.effective_date" class="date-input" />
            <InputNumber v-model="c.amount" mode="currency" currency="USD" :min="0" />
            <Button icon="pi pi-times" text rounded severity="danger" @click="removeChange(i)" aria-label="Remove" />
          </div>
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="dialogOpen = false" />
        <Button label="Save" :loading="saving" :disabled="!canSave" @click="save" />
      </template>
    </Dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { Line } from 'vue-chartjs';
import {
  Chart, LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Legend, Filler,
} from 'chart.js';

import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import Select from 'primevue/select';
import Card from 'primevue/card';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Textarea from 'primevue/textarea';
import ToggleSwitch from 'primevue/toggleswitch';
import Tag from 'primevue/tag';
import Message from 'primevue/message';

import { useOverhead } from '../composables/useOverhead';

Chart.register(LineElement, PointElement, LinearScale, CategoryScale, Tooltip, Legend, Filler);

const o = useOverhead();

const horizon = ref(12);
const horizonOptions = [
  { label: '3 months', value: 3 },
  { label: '6 months', value: 6 },
  { label: '12 months', value: 12 },
];

function onHorizonChange() {
  o.horizonMonths.value = horizon.value;
  o.loadProjection();
}

// ── formatters ──
function money(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(n));
}
function formatDate(s) {
  if (!s) return '';
  const t = Date.parse(s);
  return Number.isNaN(t) ? s : new Date(t).toLocaleDateString();
}
function formatMonth(label) {
  // label is "YYYY-MM"
  const [y, m] = String(label).split('-').map(Number);
  if (!y || !m) return label;
  return new Date(y, m - 1, 1).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
}

const _PER_YEAR = { weekly: 52, biweekly: 26, monthly: 12, quarterly: 4, semiannual: 2, annual: 1 };
function monthlyEquiv(row) {
  const per = _PER_YEAR[row.cadence] || 12;
  return (Number(row.amount) || 0) * per / 12;
}

// ── derived ──
const months = computed(() => o.projection.value?.months || []);
const hasMonths = computed(() => months.value.length > 0);
const annualized = computed(() => (Number(o.currentMonthlyTotal.value) || 0) * 12);
const horizonTotal = computed(() => {
  const m = months.value;
  return m.length ? Number(m[m.length - 1].total) : 0;
});
const horizonDelta = computed(() => horizonTotal.value - (Number(o.currentMonthlyTotal.value) || 0));
const stepDowns = computed(() => o.projection.value?.step_downs || []);
const estimateCount = computed(() => o.obligations.value.filter((x) => x.is_estimate).length);

const chartData = computed(() => ({
  labels: months.value.map((m) => formatMonth(m.label)),
  datasets: [
    {
      label: 'Monthly overhead',
      data: months.value.map((m) => Number(m.total)),
      borderColor: 'hsl(210, 80%, 45%)',
      backgroundColor: 'hsla(210, 80%, 45%, 0.15)',
      fill: true,
      tension: 0.2,
      pointRadius: 3,
      stepped: false,
    },
  ],
}));

const chartOptions = computed(() => ({
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: {
      callbacks: {
        label(ctx) {
          const fmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
          return fmt.format(ctx.parsed.y);
        },
      },
    },
  },
  scales: {
    y: {
      beginAtZero: true,
      ticks: {
        callback: (v) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(v),
      },
    },
  },
}));

// ── create / edit ──
const dialogOpen = ref(false);
const editingId = ref(null);
const saving = ref(false);
const form = reactive(emptyForm());

function emptyForm() {
  return {
    label: '',
    category: 'other',
    amount: null,
    cadence: 'monthly',
    cost_type: 'fixed',
    start_date: new Date().toISOString().slice(0, 10),
    end_date: '',
    term_total_occurrences: null,
    is_estimate: false,
    notes: '',
    scheduled_changes: [],
  };
}

const canSave = computed(() => form.label.trim().length > 0 && Number(form.amount) >= 0 && !!form.start_date);

function openCreate() {
  Object.assign(form, emptyForm());
  editingId.value = null;
  dialogOpen.value = true;
}

function openEdit(row) {
  Object.assign(form, {
    label: row.label,
    category: row.category,
    amount: Number(row.amount),
    cadence: row.cadence,
    cost_type: row.cost_type,
    start_date: row.start_date || '',
    end_date: row.end_date || '',
    term_total_occurrences: row.term_total_occurrences,
    is_estimate: row.is_estimate,
    notes: row.notes || '',
    scheduled_changes: (row.scheduled_changes || []).map((c) => ({
      effective_date: c.effective_date,
      amount: Number(c.amount),
    })),
  });
  editingId.value = row.id;
  dialogOpen.value = true;
}

function addChange() {
  form.scheduled_changes.push({ effective_date: '', amount: null });
}
function removeChange(i) {
  form.scheduled_changes.splice(i, 1);
}

function buildPayload() {
  const payload = {
    label: form.label.trim(),
    category: form.category || 'other',
    amount: String(Number(form.amount) || 0),
    cadence: form.cadence,
    cost_type: form.cost_type,
    start_date: form.start_date,
    is_estimate: !!form.is_estimate,
    notes: form.notes || null,
  };
  if (form.end_date) payload.end_date = form.end_date;
  if (form.term_total_occurrences) payload.term_total_occurrences = Number(form.term_total_occurrences);
  const changes = (form.scheduled_changes || []).filter((c) => c.effective_date && c.amount != null);
  if (changes.length) {
    payload.scheduled_changes = changes.map((c) => ({
      effective_date: c.effective_date,
      amount: String(Number(c.amount) || 0),
    }));
  }
  return payload;
}

async function save() {
  saving.value = true;
  try {
    const payload = buildPayload();
    if (editingId.value) {
      await o.updateObligation(editingId.value, payload);
    } else {
      await o.createObligation(payload);
    }
    dialogOpen.value = false;
  } catch (_) {
    // error toast handled by useApi
  } finally {
    saving.value = false;
  }
}

async function onDelete(row) {
  if (!window.confirm(`Remove "${row.label}" from overhead?`)) return;
  await o.deleteObligation(row.id);
}

onMounted(() => {
  o.horizonMonths.value = horizon.value;
  o.refreshAll();
});
</script>

<style scoped>
.overhead-view { padding: 1rem; }
.view-heading { font-size: 1.4rem; margin: 0; }
.filter-select { min-width: 9rem; }
.scope-note { margin: 1rem 0; }
.kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; margin-bottom: 1rem; }
.kpi-value { font-size: 1.8rem; font-weight: 700; }
.kpi-sub { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
/* Status colors: dark enough on a light card, with brighter shades for the dark
   theme (data-theme="dark" on <html>) so they don't sink into the background. */
.kpi-sub.down { color: #15803d; }
.kpi-sub.up { color: #b91c1c; }
:global([data-theme='dark'] .overhead-view .kpi-sub.down) { color: #4ade80; }
:global([data-theme='dark'] .overhead-view .kpi-sub.up) { color: #f87171; }
.panel { margin-bottom: 1rem; }
.chart-wrap { height: 340px; }
.muted { color: var(--p-text-muted-color, #6b7280); padding: 2rem 0; text-align: center; }
.stepdowns { list-style: none; padding: 0; margin: 1rem 0 0; }
.stepdowns li { padding: 0.35rem 0; display: flex; gap: 0.5rem; align-items: center; }
.stepdowns .pi-arrow-down { color: #15803d; }
:global([data-theme='dark'] .overhead-view .stepdowns .pi-arrow-down) { color: #4ade80; }
.form-grid { display: flex; flex-direction: column; gap: 0.75rem; }
.row { display: grid; grid-template-columns: 11rem 1fr; align-items: center; gap: 0.75rem; }
.row.check { grid-template-columns: auto 1fr; }
.date-input { padding: 0.5rem; border: 1px solid var(--p-inputtext-border-color, #d1d5db); border-radius: 6px; }
.changes { border-top: 1px solid var(--p-content-border-color, #e5e7eb); padding-top: 0.75rem; }
.changes-head { display: flex; justify-content: space-between; align-items: center; }
.change-row { display: grid; grid-template-columns: 1fr 1fr auto; gap: 0.5rem; margin-top: 0.5rem; }
.ml { margin-left: 0.4rem; }
</style>
