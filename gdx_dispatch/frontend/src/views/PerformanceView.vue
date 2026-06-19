<template>
    <section class="performance-view view-card">
      <div class="page-header">
        <h2>Performance Tracker</h2>
      </div>

      <!-- Date Filter (always visible — user may need to widen the range) -->
      <div class="toolbar">
        <div class="flex align-items-center gap-2">
          <DatePicker v-model="startDate" dateFormat="yy-mm-dd" showIcon placeholder="Start" data-testid="perf-start-top" />
          <DatePicker v-model="endDate" dateFormat="yy-mm-dd" showIcon placeholder="End" data-testid="perf-end-top" />
          <Button label="Refresh" icon="pi pi-refresh" :loading="loading" data-testid="refresh-btn-top" @click="loadData" />
        </div>
      </div>

      <EmptyState
        v-if="!loading && !performanceData.length"
        icon="pi pi-chart-line"
        title="No performance data yet"
        message="Performance metrics will appear once team members complete jobs in the selected date range. Try widening the dates above."
      />

      <template v-else>
      <!-- Summary Cards -->
      <div class="summary-cards">
        <Card data-testid="avg-jobs">
          <template #title>Avg Jobs</template>
          <template #content><p class="stat-value">{{ avgStat("jobs_completed") }}</p></template>
        </Card>
        <Card data-testid="avg-revenue">
          <template #title>Avg Revenue</template>
          <template #content><p class="stat-value">{{ currency(avgStat("revenue_generated")) }}</p></template>
        </Card>
        <Card data-testid="avg-efficiency">
          <template #title>Avg Efficiency</template>
          <template #content>
            <p class="stat-value" :class="effClass(avgStat('efficiency_score'))">{{ avgStat("efficiency_score") }}%</p>
          </template>
        </Card>
        <Card data-testid="avg-ontime">
          <template #title>Avg On-Time</template>
          <template #content><p class="stat-value">{{ avgStat("on_time_pct") }}%</p></template>
        </Card>
      </div>

      <!-- Performance Table -->
      <DataTable
      responsiveLayout="scroll" :value="performanceData" :loading="loading" stripedRows :paginator="true" :rows="15" data-testid="performance-table">
        <template #empty>
          <EmptyState icon="pi pi-chart-line" title="No performance data" message="Performance metrics will appear once team members complete jobs." />
        </template>
        <Column field="user_name" header="Team Member" sortable />
        <Column field="jobs_completed" header="Jobs" sortable />
        <Column field="avg_completion_time_hours" header="Avg Hours" sortable>
          <template #body="{ data }">{{ Number(data.avg_completion_time_hours || 0).toFixed(1) }}</template>
        </Column>
        <Column field="revenue_generated" header="Revenue" sortable>
          <template #body="{ data }">{{ currency(data.revenue_generated) }}</template>
        </Column>
        <Column field="customer_rating" header="Rating" sortable>
          <template #body="{ data }">{{ Number(data.customer_rating || 0).toFixed(1) }} / 5</template>
        </Column>
        <Column field="on_time_pct" header="On-Time %" sortable>
          <template #body="{ data }">{{ Number(data.on_time_pct || 0).toFixed(0) }}%</template>
        </Column>
        <Column field="callbacks" header="Callbacks" sortable />
        <Column field="efficiency_score" header="Efficiency" sortable>
          <template #body="{ data }">
            <ProgressBar :value="data.efficiency_score || 0" :showValue="true" :class="effClass(data.efficiency_score)" style="height: 1.2rem" />
          </template>
        </Column>
      </DataTable>
      </template>
    </section>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import DatePicker from "primevue/datepicker";
import ProgressBar from "primevue/progressbar";
import EmptyState from "../components/EmptyState.vue";
import { useApi } from "../composables/useApi";

const toast = useToast();
const api = useApi();
const loading = ref(false);
const performanceData = ref([]);

const now = new Date();
const startDate = ref(new Date(now.getFullYear(), now.getMonth(), 1));
const endDate = ref(new Date());

function currency(v) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(v) || 0);
}
function fmtDate(d) {
  if (!d) return "";
  return d instanceof Date ? d.toISOString().slice(0, 10) : d;
}
function effClass(score) {
  const s = Number(score) || 0;
  if (s >= 80) return "eff-green";
  if (s >= 60) return "eff-yellow";
  return "eff-red";
}
function avgStat(field) {
  if (!performanceData.value.length) return 0;
  const sum = performanceData.value.reduce((a, r) => a + (Number(r[field]) || 0), 0);
  return Math.round((sum / performanceData.value.length) * 10) / 10;
}

async function loadData() {
  loading.value = true;
  try {
    const s = fmtDate(startDate.value);
    const e = fmtDate(endDate.value);
    const r = await api.get(`/api/performance/users?start=${s}&end=${e}`);
    performanceData.value = Array.isArray(r) ? r : r?.items || [];
  } catch (e) {
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
.eff-green :deep(.p-progressbar-value) { background: var(--p-green-500); }
.eff-yellow :deep(.p-progressbar-value) { background: var(--yellow-500, #eab308); }
.eff-red :deep(.p-progressbar-value) { background: var(--p-red-500); }
</style>
