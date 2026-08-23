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

        <!-- Sales Tax collected — plan §16 (Doug: "track sales tax and report on
             it"). Split by provenance: GDX invoices (this app computed the tax)
             vs QuickBooks imports (QB computed it), so the office reconciles the
             two sources separately. Collected = tax on paid invoices (the
             remittance liability); Outstanding = billed-but-unpaid tax. -->
        <Card class="sales-tax-card" data-testid="report-sales-tax">
          <template #title>
            <div class="sales-tax-title">
              <span>Sales Tax Collected</span>
              <div class="sales-tax-totals" data-testid="sales-tax-totals">
                <span class="tax-chip">Collected {{ formatCurrencyCents(salesTax.totals.tax_collected) }}</span>
                <span class="tax-chip tax-chip-muted">Outstanding {{ formatCurrencyCents(salesTax.totals.tax_outstanding) }}</span>
                <span class="tax-chip tax-chip-total">Total {{ formatCurrencyCents(salesTax.totals.tax_total) }}</span>
              </div>
            </div>
          </template>
          <template #content>
            <p class="report-sub muted">
              GDX-generated {{ formatCurrencyCents(salesTax.totals.gdx_tax) }} ·
              QuickBooks import {{ formatCurrencyCents(salesTax.totals.quickbooks_tax) }}.
              Issued invoices only, grouped by invoice date; drafts, deposits, and voids excluded.
            </p>
            <div v-if="salesTaxError" class="inline-error" data-testid="sales-tax-error">
              Couldn't load the sales-tax report. Try Apply again.
            </div>
            <div v-else-if="salesTax.items.length === 0" class="muted" data-testid="sales-tax-empty">
              No sales tax recorded for this period.
            </div>
            <DataTable
              v-else
              responsiveLayout="scroll"
              :value="salesTax.items"
              data-testid="sales-tax-table"
              striped-rows
            >
              <Column header="Period">
                <template #body="{ data }">{{ formatTaxPeriod(data.period_start) }}</template>
              </Column>
              <Column header="GDX Tax">
                <template #body="{ data }">{{ formatCurrencyCents(data.gdx.tax_total) }}</template>
              </Column>
              <Column header="QB Tax">
                <template #body="{ data }">{{ formatCurrencyCents(data.quickbooks.tax_total) }}</template>
              </Column>
              <Column header="Collected">
                <template #body="{ data }">{{ formatCurrencyCents(data.tax_collected) }}</template>
              </Column>
              <Column header="Outstanding">
                <template #body="{ data }">{{ formatCurrencyCents(data.tax_outstanding) }}</template>
              </Column>
              <Column header="Total">
                <template #body="{ data }"><strong>{{ formatCurrencyCents(data.tax_total) }}</strong></template>
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
import { applyChartTheme, chartThemeColors } from "../utils/chartTheme";
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
const salesTax = ref({
  items: [],
  totals: {
    tax_total: 0, tax_collected: 0, tax_outstanding: 0,
    gdx_tax: 0, quickbooks_tax: 0,
  },
});
// Distinct from "no items": a failed fetch must not read as a tax-free month.
const salesTaxError = ref(false);

const jobStatusCounts = ref({});

// period_start is a UTC date_trunc boundary ("2026-07-01T00:00:00+00:00").
// Formatting it in local time would render July as "Jun 2026" for anyone west
// of UTC — which is everyone here — so pin the formatter to UTC.
function formatPeriodLabel(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  return d.toLocaleDateString(undefined, {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

// M8 (money-audit-2026-08-04): this read `b.label` / `b.value`, fields
// /api/reports/revenue-by-period has never emitted — it returns
// {period_start, invoice_count, revenue, avg_invoice}. Both arrays were
// therefore [undefined, ...], and Chart.js drew an empty frame on a 0–1 axis
// with no x labels. The backend was ALSO summing a null column, so each bug
// hid the other: fixing either alone still leaves the chart blank.
const revenueChartData = computed(() => ({
  labels: revenueByPeriod.value.map((b) => formatPeriodLabel(b.period_start)),
  datasets: [{
    label: "Revenue",
    data: revenueByPeriod.value.map((b) => Number(b.revenue ?? 0)),
    backgroundColor: "#0ea5e9",
    borderRadius: 4,
  }],
}));

// Computed (not a plain const) so the theme CSS vars are resolved when the
// chart renders after mount, not at module evaluation.
const barChartOptions = computed(() =>
  applyChartTheme({
    responsive: true,
    plugins: { legend: { display: false }, title: { display: false } },
    scales: {
      x: {},
      y: { ticks: { callback: (v) => "$" + v.toLocaleString() }, beginAtZero: true },
    },
  })
);

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

// Pie charts must not get scales.x/y injected (applyChartTheme would render
// axes), so theme only the legend labels here.
const pieChartOptions = computed(() => ({
  responsive: true,
  plugins: { legend: { position: "bottom", labels: { color: chartThemeColors().text } } },
}));

function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(Number(value) || 0);
}

// Tax needs the cents — $11.07 rounded to $11 loses the remittance amount.
function formatCurrencyCents(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value) || 0);
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// period_start is a calendar date (month or quarter start), possibly serialized
// with a UTC time part. Parse the y/m from the STRING — never via `new Date()`,
// which would shift "2026-07-01T00:00:00+00:00" back to Jun 30 in a timezone
// behind UTC (Doug's) and mislabel the whole row.
function formatTaxPeriod(iso) {
  if (!iso || typeof iso !== "string") return "—";
  const m = iso.match(/^(\d{4})-(\d{2})/);
  if (!m) return iso;
  const year = m[1];
  const monthIdx = parseInt(m[2], 10) - 1;
  if (monthIdx < 0 || monthIdx > 11) return iso;
  if (salesTax.value && salesTax.value.period === "quarter") {
    return `Q${Math.floor(monthIdx / 3) + 1} ${year}`;
  }
  return `${MONTH_NAMES[monthIdx]} ${year}`;
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
    const [summaryData, customersData, revenueData, jobsData, taxData] = await Promise.allSettled([
      api.get(`/api/reports/summary${params}`),
      api.get(`/api/reports/top-customers${params}`),
      api.get(`/api/reports/revenue-by-period${params}`),
      api.get(`/api/jobs${params || "?"}&page_size=1000`),
      api.get(`/api/reports/sales-tax${params}`),
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

    if (taxData.status === "fulfilled" && taxData.value) {
      salesTaxError.value = false;
      const td = taxData.value;
      salesTax.value = {
        items: Array.isArray(td.items) ? td.items : [],
        totals: { ...salesTax.value.totals, ...(td.totals || {}) },
        period: td.period || "month",
      };
    } else {
      // Rejected (e.g. a 500 from the endpoint). Flag it so the card shows an
      // error, not the "No sales tax recorded" empty state, which would be a
      // silent lie about a tax-free period.
      salesTaxError.value = true;
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

.sales-tax-card {
  margin-top: 1.5rem;
}

.sales-tax-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.sales-tax-totals {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

/* Theme vars, not literal colors — Doug runs dark mode. The surface-N scale
   does NOT invert with the theme (--p-surface-100 stays #f4f4f5 in dark), which
   gave white-on-light chips; use a transparent fill with tokens that DO flip
   (content-border-color, text-color, text-muted-color). Verified light+dark. */
.tax-chip {
  font-size: 0.85rem;
  font-weight: 600;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  background: transparent;
  color: var(--p-text-color, #1e293b);
  border: 1px solid var(--p-content-border-color, #e2e8f0);
}

.tax-chip-muted {
  color: var(--p-text-muted-color, #64748b);
}

.tax-chip-total {
  background: var(--p-primary-color, #0ea5e9);
  color: var(--p-primary-contrast-color, #ffffff);
  border-color: transparent;
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
