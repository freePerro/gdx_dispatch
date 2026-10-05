<template>
    <section class="performance-view view-card">
      <div class="page-header">
        <h2>Performance Tracker</h2>
      </div>

      <!-- Month filter. The endpoint aggregates by `period` (YYYY-MM); the
           start/end pickers this replaced sent params it never declared, so
           they filtered nothing (GDXA-174). -->
      <div class="toolbar">
        <div class="flex align-items-center gap-2">
          <DatePicker
            v-model="month"
            view="month"
            dateFormat="yy-mm"
            showIcon
            placeholder="Month"
            data-testid="perf-month"
            @update:modelValue="loadData"
          />
          <Button label="Refresh" icon="pi pi-refresh" :loading="loading" data-testid="refresh-btn-top" @click="loadData" />
        </div>
      </div>

      <Message v-if="hasReadFailure" severity="warn" :closable="false" class="mb-3" data-testid="perf-read-failed">
        Some figures could not be read right now. They show as — rather than as zero; try Refresh.
      </Message>

      <EmptyState
        v-if="!loading && !hasActivity && !hasReadFailure"
        icon="pi pi-chart-line"
        title="Not enough data yet"
        :message="`Nothing is recorded for ${monthLabel} yet — no completed jobs, invoices, tasks, checklists or clock hours. Pick another month above.`"
        data-testid="perf-empty"
      />

      <template v-else>
      <!-- Summary Cards — totals over what is actually recorded. A card with
           nothing to total says so instead of showing 0. -->
      <div class="summary-cards">
        <Card data-testid="total-jobs">
          <template #title>Jobs Completed</template>
          <template #content><p class="stat-value">{{ show(total("jobs_completed")) }}</p></template>
        </Card>
        <Card data-testid="total-revenue">
          <template #title>Revenue</template>
          <template #content><p class="stat-value">{{ show(total("revenue"), currency) }}</p></template>
        </Card>
        <Card data-testid="total-hours">
          <template #title>Clock Hours</template>
          <template #content>
            <p class="stat-value">{{ show(total("hours_worked"), hours) }}</p>
            <small v-if="hoursLeftOut" class="legend" data-testid="total-hours-partial">
              Leaves out {{ hoursLeftOut }} {{ hoursLeftOut === 1 ? "person" : "people" }} whose hours are not known yet
            </small>
          </template>
        </Card>
      </div>

      <DataTable
      responsiveLayout="scroll" :value="rows" :loading="loading" stripedRows :paginator="true" :rows="15" data-testid="performance-table">
        <Column field="name" header="Team Member" sortable />
        <Column field="jobs_completed" header="Jobs" sortable>
          <template #body="{ data }"><span :title="why(data, 'jobs_completed')">{{ show(data.jobs_completed) }}</span></template>
        </Column>
        <Column field="revenue" header="Revenue" sortable>
          <template #body="{ data }"><span :title="why(data, 'revenue')">{{ show(data.revenue, currency) }}</span></template>
        </Column>
        <Column field="avg_job_value" header="Avg Job" sortable>
          <template #body="{ data }"><span :title="why(data, 'avg_job_value')">{{ show(data.avg_job_value, currency) }}</span></template>
        </Column>
        <Column field="hours_worked" header="Clock Hours" sortable>
          <template #body="{ data }">
            <span :title="why(data, 'hours_worked')" :data-testid="`hours-${data.id}`">{{ show(data.hours_worked, hours) }}</span>
          </template>
        </Column>
        <Column field="tasks_completed" header="Tasks Done" sortable>
          <template #body="{ data }"><span :title="why(data, 'tasks_completed')">{{ show(data.tasks_completed) }}</span></template>
        </Column>
        <Column field="safety_checklists" header="Safety Checklists" sortable>
          <template #body="{ data }"><span :title="why(data, 'safety_checklists')">{{ show(data.safety_checklists) }}</span></template>
        </Column>
      </DataTable>
      <p class="legend" data-testid="perf-legend">
        — means the figure is not available for {{ monthLabel }}: usually nothing is recorded yet. Hover it for the reason.
        Revenue is what was invoiced (not necessarily paid) on the completed jobs counted.
        Clock hours are finished shifts from the time clock, breaks deducted; they are not billed or paid hours.
      </p>
      </template>
    </section>
</template>

<script setup>
import { computed, ref, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import DatePicker from "primevue/datepicker";
import Message from "primevue/message";
import EmptyState from "../components/EmptyState.vue";
import { useApi } from "../composables/useApi";
import { formatMoney as currency } from "../composables/useFormatters";

const toast = useToast();
const api = useApi();
const loading = ref(false);
const rows = ref([]);
const month = ref(new Date());

// Why the server left a stat null. `read_failed` also raises the banner.
const REASONS = {
  no_data: "Nothing recorded for this person in this month",
  read_failed: "Could not be read right now",
  period_required: "Pick a month",
  not_recorded: "This system does not record it",
  shift_flagged: "A shift needs a look on Timesheets (no clock-out, no duration, or implausibly long)",
  in_progress: "Clocked in now; no shift finished yet this month",
  restricted: "Crew hours are shown to dispatch and admin roles only",
};

function fmtPeriod(d) {
  const date = d instanceof Date ? d : new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}
const monthLabel = computed(() =>
  (month.value instanceof Date ? month.value : new Date()).toLocaleDateString(undefined, { month: "long", year: "numeric" }),
);

// Up to two decimals, as the server rounds: two minutes on the clock is 0.03,
// and one decimal would render it "0.0" — a nought that reads as no work.
function hours(v) {
  return String(Math.round(Number(v) * 100) / 100);
}
function show(v, fmt) {
  if (v === null || v === undefined) return "—";
  return fmt ? fmt(v) : String(v);
}
function why(row, field) {
  const reason = row.unavailable?.[field];
  return reason ? REASONS[reason] || reason : "";
}
// Hours that exist but are not known yet; such a person is left out of the
// Clock Hours total, and the card says how many were left out.
const UNKNOWN_HOURS = ["shift_flagged", "in_progress", "read_failed"];
const hoursLeftOut = computed(() =>
  rows.value.filter((r) => UNKNOWN_HOURS.includes(r.unavailable?.hours_worked)).length,
);
// Totals only over values that exist; null when nobody has one, so the card
// reads "—" rather than a 0 nobody measured.
function total(field) {
  const known = rows.value.map((r) => r[field]).filter((v) => v !== null && v !== undefined);
  if (!known.length) return null;
  const sum = known.reduce((a, v) => a + Number(v), 0);
  return Math.round(sum * 100) / 100;
}

const hasActivity = computed(() =>
  rows.value.some(
    (r) => r.jobs_completed > 0 || r.revenue > 0 || (r.hours_worked !== null && r.hours_worked !== undefined)
      // A reader who may not see hours cannot be told there are none.
      || [...UNKNOWN_HOURS, "restricted"].includes(r.unavailable?.hours_worked) || r.tasks_completed > 0 || r.safety_checklists > 0,
  ),
);
const hasReadFailure = computed(() =>
  rows.value.some((r) => Object.values(r.unavailable || {}).includes("read_failed")),
);

async function loadData() {
  loading.value = true;
  try {
    const r = await api.get(`/api/performance/users?period=${fmtPeriod(month.value)}`);
    // The endpoint answers `{users: [{id, name, stats: {...}, unavailable}]}`.
    rows.value = (r?.users || []).map((u) => ({
      id: u.id,
      name: u.name || u.email || "",
      ...(u.stats || {}),
      unavailable: u.unavailable || {},
    }));
  } catch (e) {
    rows.value = [];
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load performance data", life: 4000 });
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  loadData();
});
</script>

<style scoped>
.performance-view { padding: 1.5rem; }
.page-header { margin-bottom: 1rem; }
.page-header h2 { margin: 0; }
.summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.stat-value { font-size: 1.8rem; font-weight: 700; margin: 0; }
.toolbar { display: flex; justify-content: flex-start; margin-bottom: 1rem; }
.legend { margin-top: 0.75rem; font-size: 0.875rem; color: var(--p-text-muted-color); }
</style>
