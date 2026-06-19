<template>
    <section class="reports-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Reports</h2>
        </template>
        <template #end>
          <div class="date-range-row">
            <DatePicker v-model="dateRange" selection-mode="range" placeholder="Select period" date-format="yy-mm-dd" data-testid="reports-date-range" :show-icon="true" />
            <Button label="Apply" icon="pi pi-filter" data-testid="reports-apply-filter" @click="loadReports" />
            <Button label="Export CSV" icon="pi pi-download" severity="secondary" data-testid="reports-export-btn" @click="exportCsv" />
          </div>
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="reports-load-error">
        {{ loadError }}
      </div>
      <div v-if="isLoading" class="spinner-wrap" data-testid="reports-loading">
        <ProgressSpinner />
      </div>

      <template v-if="!isLoading && !loadError">
        <!-- Summary Cards -->
        <div class="summary-cards">
          <Card data-testid="report-revenue">
            <template #title>Revenue (Period)</template>
            <template #content><p class="report-value">{{ formatCurrency(summary.revenue_total) }}</p></template>
          </Card>
          <Card data-testid="report-open-jobs">
            <template #title>Open Jobs</template>
            <template #content><p class="report-value">{{ summary.open_jobs }}</p></template>
          </Card>
          <Card data-testid="report-completed">
            <template #title>Jobs Completed</template>
            <template #content><p class="report-value">{{ summary.jobs_completed }}</p></template>
          </Card>
          <Card data-testid="report-avg-value">
            <template #title>Avg Invoice (Period)</template>
            <template #content>
              <p class="report-value">{{ formatCurrency(summary.avg_job_value) }}</p>
              <p class="report-sub muted">across billed invoices in the window</p>
            </template>
          </Card>
        </div>

        <!-- Revenue by Period Chart -->
        <div class="chart-grid">
          <Card class="chart-card" data-testid="report-revenue-chart">
            <template #title>Revenue by Period</template>
            <template #content>
              <div v-if="revenueByPeriod.length === 0" class="muted">No revenue data for this period.</div>
              <Bar v-else :data="revenueChartData" :options="barChartOptions" data-testid="revenue-bar-chart" />
            </template>
          </Card>

          <Card class="chart-card" data-testid="report-jobs-pie">
            <template #title>Jobs by Status</template>
            <template #content>
              <div v-if="!jobStatusData.labels.length" class="muted">No job data.</div>
              <Pie v-else :data="jobStatusData" :options="pieChartOptions" data-testid="jobs-pie-chart" />
            </template>
          </Card>
        </div>

        <!-- Top Customers -->
        <Card class="top-customers-card" data-testid="report-top-customers">
          <template #title>Top Customers</template>
          <template #content>
            <DataTable
      responsiveLayout="scroll" :value="topCustomers" data-testid="top-customers-table" striped-rows>
              <Column field="customer_name" header="Customer" />
              <Column field="invoice_count" header="Invoices" style="width: 90px" />
              <!-- Linked Jobs counts unique jobs.id from invoices.job_id.
                   QB-imported invoices have no linked job, so this can be 0
                   while the customer still has revenue. The "Invoices" column
                   prevents the "0 jobs · $398" head-scratcher. -->
              <Column field="job_count" header="Linked Jobs" style="width: 110px" />
              <!-- Server returns period-filtered revenue (i.created_at within
                   start_dt/end_dt), not actual lifetime. Header reflects that. -->
              <Column header="Revenue (Period)">
                <template #body="{ data }">{{ formatCurrency(data.lifetime_value) }}</template>
              </Column>
            </DataTable>
          </template>
        </Card>
      </template>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApi } from "../composables/useApi";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import ProgressSpinner from "primevue/progressspinner";
import Toolbar from "primevue/toolbar";
import { Bar, Pie } from "vue-chartjs";
import {
  Chart as ChartJS, CategoryScale, LinearScale, BarElement,
  ArcElement, Title, Tooltip, Legend,
} from "chart.js";

ChartJS.register(CategoryScale, LinearScale, BarElement, ArcElement, Title, Tooltip, Legend);

const api = useApi();

const isLoading = ref(true);
const loadError = ref("");
const dateRange = ref(null);

const summary = ref({
  revenue_total: 0,
  open_jobs: 0,
  jobs_completed: 0,
  avg_job_value: 0,
});
const topCustomers = ref([]);
const revenueByPeriod = ref([]);

const jobStatusCounts = ref({});

const revenueChartData = computed(() => ({
  labels: revenueByPeriod.value.map((b) => b.label),
  datasets: [{
    label: "Revenue",
    data: revenueByPeriod.value.map((b) => b.value),
    backgroundColor: "#0ea5e9",
    borderRadius: 4,
  }],
}));

const barChartOptions = {
  responsive: true,
  plugins: { legend: { display: false }, title: { display: false } },
  scales: {
    x: { ticks: { color: "#94a3b8" }, grid: { color: "#1e293b" } },
    y: { ticks: { color: "#94a3b8", callback: (v) => "$" + v.toLocaleString() }, grid: { color: "#1e293b" }, beginAtZero: true },
  },
};

const jobStatusData = computed(() => {
  const labels = Object.keys(jobStatusCounts.value);
  const data = Object.values(jobStatusCounts.value);
  return {
    labels,
    datasets: [{
      data,
      backgroundColor: ["#0ea5e9", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#64748b"],
    }],
  };
});

const pieChartOptions = {
  responsive: true,
  plugins: { legend: { position: "bottom", labels: { color: "#94a3b8" } } },
};

function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(Number(value) || 0);
}

function buildDateParams() {
  if (!dateRange.value || !Array.isArray(dateRange.value) || !dateRange.value[0]) return "";
  const fmt = (d) => d.toISOString().split("T")[0];
  let params = `?start_date=${fmt(dateRange.value[0])}`;
  if (dateRange.value[1]) params += `&end_date=${fmt(dateRange.value[1])}`;
  return params;
}

async function loadReports() {
  isLoading.value = true;
  loadError.value = "";
  const params = buildDateParams();
  try {
    const [summaryData, customersData, revenueData, jobsData] = await Promise.allSettled([
      api.get(`/api/reports/summary${params}`),
      api.get(`/api/reports/top-customers${params}`),
      api.get(`/api/reports/revenue-by-period${params}`),
      api.get(`/api/jobs${params || "?"}&page_size=1000`),
    ]);

    if (summaryData.status === "fulfilled" && summaryData.value) {
      summary.value = { ...summary.value, ...summaryData.value };
    }

    if (customersData.status === "fulfilled") {
      const list = customersData.value;
      topCustomers.value = Array.isArray(list) ? list : list?.items || list?.data || [];
    }

    if (revenueData.status === "fulfilled") {
      const rd = revenueData.value;
      revenueByPeriod.value = Array.isArray(rd) ? rd : rd?.items || rd?.data || rd?.periods || [];
    }

    if (jobsData.status === "fulfilled") {
      const jobs = jobsData.value;
      const jobList = Array.isArray(jobs) ? jobs : jobs?.items || jobs?.data || [];
      const counts = {};
      jobList.forEach((j) => { counts[j.status || "unknown"] = (counts[j.status || "unknown"] || 0) + 1; });
      jobStatusCounts.value = counts;
    }
  } catch (error) {
    loadError.value = error?.message || "Failed to load reports.";
  } finally {
    isLoading.value = false;
  }
}

function exportCsv() {
  const params = buildDateParams();
  window.open(`/api/reports/export${params || "?format=csv"}`, "_blank");
}

onMounted(() => {
  loadReports();
});
</script>

<style scoped>
.page-title {
  margin: 0;
}

.date-range-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.summary-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(150px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1.5rem;
}

.report-value {
  font-size: 1.5rem;
  font-weight: 700;
}

.chart-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 1.5rem;
}

@media (max-width: 768px) {
  .chart-grid {
    grid-template-columns: 1fr;
  }
}

.chart-card {
  margin-bottom: 0;
}

.bar-chart {
  display: grid;
  gap: 0.5rem;
}

.bar-row {
  display: grid;
  grid-template-columns: 80px 1fr 100px;
  align-items: center;
  gap: 0.5rem;
}

.bar-label {
  font-size: 0.85rem;
  color: var(--muted, #888);
  text-align: right;
}

.bar-track {
  background: rgba(255, 255, 255, 0.05);
  border-radius: 4px;
  height: 24px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  background: var(--primary, #4fc3f7);
  border-radius: 4px;
  transition: width 0.4s ease;
  min-width: 2px;
}

.bar-value {
  font-size: 0.85rem;
  font-weight: 600;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
}

.top-customers-card {
  margin-top: 1rem;
}

.muted {
  color: var(--muted, #888);
}

@media (max-width: 900px) {
  .summary-cards {
    grid-template-columns: repeat(2, 1fr);
  }

  .bar-row {
    grid-template-columns: 60px 1fr 80px;
  }
}

@media (max-width: 640px) {
  .summary-cards {
    grid-template-columns: 1fr;
  }

  .date-range-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
