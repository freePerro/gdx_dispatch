<template>
    <section class="variance-view view-card">
      <div class="page-header">
        <h2>Estimate vs Actual Variance</h2>
      </div>

      <!-- Date Filter (always visible) -->
      <div class="toolbar">
        <div class="flex align-items-center gap-2">
          <DatePicker v-model="startDate" dateFormat="yy-mm-dd" showIcon placeholder="Start" data-testid="variance-start-top" />
          <DatePicker v-model="endDate" dateFormat="yy-mm-dd" showIcon placeholder="End" data-testid="variance-end-top" />
          <Button label="Refresh" icon="pi pi-refresh" :loading="loading" data-testid="refresh-btn-top" @click="loadReport" />
        </div>
      </div>

      <EmptyState
        v-if="!loading && !details.length"
        icon="pi pi-chart-bar"
        title="No variance data yet"
        message="Variance appears once jobs have both estimates and actual costs recorded in the selected date range. Try widening the dates above."
      />

      <template v-else>
      <!-- Summary Cards -->
      <div class="summary-cards">
        <Card data-testid="total-jobs">
          <template #title>Total Jobs</template>
          <template #content><p class="stat-value">{{ summary.total_jobs || 0 }}</p></template>
        </Card>
        <Card data-testid="avg-variance">
          <template #title>Avg Variance</template>
          <template #content>
            <p class="stat-value" :class="summary.avg_variance_pct >= 0 ? 'text-green' : 'text-red'">
              {{ formatPct(summary.avg_variance_pct) }}
            </p>
          </template>
        </Card>
        <Card data-testid="over-budget">
          <template #title>Over Budget</template>
          <template #content><p class="stat-value text-red">{{ summary.jobs_over_budget || 0 }}</p></template>
        </Card>
        <Card data-testid="under-budget">
          <template #title>Under Budget</template>
          <template #content><p class="stat-value text-green">{{ summary.jobs_under_budget || 0 }}</p></template>
        </Card>
      </div>

      <!-- Variance Table -->
      <DataTable
      responsiveLayout="scroll" :value="details" :loading="loading" stripedRows :paginator="true" :rows="15" data-testid="variance-table">
        <template #empty>
          <EmptyState icon="pi pi-chart-bar" title="No variance data" message="Variance data will appear once jobs have both estimates and actual costs recorded." />
        </template>
        <Column field="job_title" header="Job" sortable />
        <Column field="customer_name" header="Customer" sortable />
        <Column field="estimated_total" header="Estimated" sortable>
          <template #body="{ data }">{{ currency(data.estimated_total) }}</template>
        </Column>
        <Column field="actual_total" header="Actual" sortable>
          <template #body="{ data }">{{ currency(data.actual_total) }}</template>
        </Column>
        <Column field="variance_amount" header="Variance $" sortable>
          <template #body="{ data }">
            <span :class="data.variance_amount >= 0 ? 'text-green' : 'text-red'">
              {{ currency(data.variance_amount) }}
            </span>
          </template>
        </Column>
        <Column field="variance_pct" header="Variance %" sortable>
          <template #body="{ data }">
            <span :class="data.variance_pct >= 0 ? 'text-green' : 'text-red'">
              {{ formatPct(data.variance_pct) }}
            </span>
          </template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
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
import Tag from "primevue/tag";
import EmptyState from "../components/EmptyState.vue";
import { useApi } from "../composables/useApi";
import { formatMoney as currency, formatPercent } from "../composables/useFormatters";

const toast = useToast();
const api = useApi();
const loading = ref(false);
const summary = ref({});
const details = ref([]);

const now = new Date();
const startDate = ref(new Date(now.getFullYear(), now.getMonth() - 1, 1));
const endDate = ref(new Date());

function formatPct(v) {
  return formatPercent(v ?? 0, { digits: 1, whole: true });
}
function fmtDate(d) {
  if (!d) return "";
  return d instanceof Date ? d.toISOString().slice(0, 10) : d;
}
function statusSeverity(s) {
  const map = { completed: "success", in_progress: "info", scheduled: "warn", cancelled: "danger" };
  return map[(s || "").toLowerCase()] || "secondary";
}

async function loadReport() {
  loading.value = true;
  try {
    const s = fmtDate(startDate.value);
    const e = fmtDate(endDate.value);
    const r = await api.get(`/api/variance/summary?start=${s}&end=${e}`);
    summary.value = r?.summary || {};
    details.value = r?.details || [];
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load variance report", life: 4000 });
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  loadReport();
});
</script>

<style scoped>
.variance-view { padding: 1.5rem; }
.page-header { margin-bottom: 1rem; }
.page-header h2 { margin: 0; }
.summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }
.stat-value { font-size: 1.8rem; font-weight: 700; margin: 0; }
.text-green { color: var(--p-green-600); }
.text-red { color: var(--p-red-600); }
.toolbar { display: flex; justify-content: flex-start; margin-bottom: 1rem; }
</style>
