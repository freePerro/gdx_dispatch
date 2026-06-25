<template>
    <section class="dashboard-view view-card">
      <div class="dashboard-period">
        <label class="period-label" for="dashboard-period-select">Showing:</label>
        <Select
          id="dashboard-period-select"
          v-model="period"
          :options="PERIOD_OPTIONS"
          optionLabel="label"
          optionValue="value"
          @change="loadSummary"
          data-testid="dashboard-period"
          class="period-select"
        />
      </div>
      <div class="kpi-grid">
        <Card
          v-for="kpi in kpis"
          :key="kpi.key"
          class="kpi-card"
          data-testid="kpi-card"
        >
          <template #title>{{ kpi.label }}</template>
          <template #content>
            <div class="kpi-body">
              <Skeleton v-if="summaryLoading" width="6rem" height="2rem" class="kpi-skeleton" />
              <div v-else class="kpi-value" :data-testid="`kpi-value-${kpi.key}`">{{ kpi.formattedValue }}</div>
              <div class="kpi-trend" :class="kpi.trendDirection">
                <Skeleton v-if="summaryLoading" width="3.5rem" height="0.85rem" />
                <template v-else>
                  <span aria-hidden="true">{{ kpi.trendArrow }}</span>
                  <span>{{ kpi.trendLabel }}</span>
                </template>
              </div>
            </div>
          </template>
        </Card>
      </div>

      <div class="dashboard-actions" data-tour="dashboard-quick-actions">
        <Button label="+ Service Call" icon="pi pi-phone" data-testid="quick-service-call" data-tour="dash-service-call" severity="warn" @click="showServiceCallDialog = true" />
        <Button label="+ New Job" icon="pi pi-plus" data-testid="quick-new-job" data-tour="dash-new-job" @click="router.push({ path: '/jobs', query: { new: '1' } })" />
        <Button label="+ New Estimate" icon="pi pi-plus" data-testid="quick-new-estimate" data-tour="dash-new-estimate" severity="secondary" @click="router.push('/estimates/new')" />
        <Button label="+ New Customer" icon="pi pi-plus" data-testid="quick-new-customer" data-tour="dash-new-customer" severity="secondary" @click="router.push({ path: '/customers', query: { new: '1' } })" />
        <Button label="Open Dispatch" icon="pi pi-map" data-testid="quick-open-dispatch" data-tour="dash-open-dispatch" severity="info" @click="router.push('/dispatch')" />
      </div>

      <!-- Estimates Pipeline (above-tech only) -->
      <Card v-if="canSeePipeline && pipelineLoaded" class="pipeline-card" data-testid="estimates-pipeline-card">
        <template #title>
          <div class="pipeline-title-row">
            <span><i class="pi pi-chart-line" style="color:#2563eb" /> Estimates Pipeline</span>
            <Button
              label="View Estimates"
              icon="pi pi-arrow-right"
              iconPos="right"
              text
              size="small"
              @click="router.push('/estimates')"
            />
          </div>
        </template>
        <template #content>
          <div class="pipeline-grid">
            <div class="pipeline-stat">
              <div class="pipeline-label">Open Estimates</div>
              <div class="pipeline-value" data-testid="pipeline-count">{{ pipeline.count }}</div>
            </div>
            <div class="pipeline-stat">
              <div class="pipeline-label">Total Value</div>
              <div class="pipeline-value" data-testid="pipeline-sell">{{ formatCurrency(pipeline.total_sell) }}</div>
            </div>
            <div class="pipeline-stat">
              <div class="pipeline-label">Estimated Profit</div>
              <div class="pipeline-value" :class="{ negative: pipeline.net_profit < 0 }" data-testid="pipeline-profit">
                {{ formatCurrency(pipeline.net_profit) }}
              </div>
              <div class="pipeline-sub">{{ formatPercent(pipeline.blended_margin) }} blended margin</div>
            </div>
          </div>
          <p v-if="pipeline.estimates_with_manual_lines > 0" class="pipeline-warn">
            ⚠ {{ pipeline.estimates_with_manual_lines }}
            estimate{{ pipeline.estimates_with_manual_lines === 1 ? '' : 's' }}
            ha{{ pipeline.estimates_with_manual_lines === 1 ? 's' : 've' }}
            manually-priced lines (no cost data) — excluded from totals.
          </p>
        </template>
      </Card>

      <!-- Sales Funnel — bookings clock (separate from /summary's invoice clock) -->
      <Card v-if="canSeePipeline && funnelLoaded" class="funnel-card" data-testid="sales-funnel-card">
        <template #title>
          <div class="pipeline-title-row">
            <span><i class="pi pi-shopping-cart" style="color:#16a34a" /> Sales Funnel</span>
            <Button label="View Estimates" icon="pi pi-arrow-right" iconPos="right" text size="small" @click="router.push('/estimates')" />
          </div>
        </template>
        <template #content>
          <h4 class="funnel-section-title">Sold Jobs</h4>
          <div class="sold-grid">
            <div class="sold-window" v-for="w in soldWindows" :key="w.key" :data-testid="`sold-${w.key}`">
              <div class="sold-window-label">{{ w.label }}</div>
              <div class="sold-window-row">
                <div class="sold-stat">
                  <div class="sold-stat-label">Jobs</div>
                  <div class="sold-stat-value" :data-testid="`sold-${w.key}-count`">{{ w.data.count }}</div>
                </div>
                <div class="sold-stat">
                  <div class="sold-stat-label">Doors</div>
                  <div class="sold-stat-value" :data-testid="`sold-${w.key}-doors`">{{ w.data.door_count }}</div>
                </div>
                <div class="sold-stat">
                  <div class="sold-stat-label">$</div>
                  <div class="sold-stat-value" :data-testid="`sold-${w.key}-dollars`">{{ formatCurrency(w.data.dollar_amount) }}</div>
                </div>
                <div class="sold-stat">
                  <div class="sold-stat-label">Avg Ticket</div>
                  <div class="sold-stat-value">{{ formatCurrency(w.data.avg_ticket) }}</div>
                </div>
              </div>
            </div>
          </div>

          <div class="funnel-row">
            <div class="funnel-tile">
              <div class="funnel-tile-label">Close Rate (30d)</div>
              <div class="funnel-tile-value" data-testid="close-rate">
                {{ funnel.close_rate.rate === null ? '—' : formatPercent(funnel.close_rate.rate) }}
              </div>
              <div class="funnel-tile-sub">{{ funnel.close_rate.accepted }} of {{ funnel.close_rate.decisions }} sent estimates</div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Estimates Outstanding</div>
              <div class="funnel-tile-value" data-testid="outstanding-count">{{ funnel.estimates_outstanding.count }}</div>
              <div class="funnel-tile-sub">{{ formatCurrency(funnel.estimates_outstanding.dollar_amount) }} at stake</div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Aging (sent → ?)</div>
              <div class="aging-bars">
                <div v-for="(b, k) in funnel.estimates_outstanding.buckets" :key="k" class="aging-bar">
                  <span class="aging-bar-label">{{ b.label }}</span>
                  <span class="aging-bar-count">{{ b.count }}</span>
                  <span class="aging-bar-dollars">{{ formatCurrency(b.dollar_amount) }}</span>
                </div>
              </div>
            </div>
          </div>

          <h4 class="funnel-section-title">Billed (Invoice Clock)</h4>
          <div class="funnel-row">
            <div class="funnel-tile">
              <div class="funnel-tile-label">Billed Today</div>
              <div class="funnel-tile-value" data-testid="billed-today">{{ formatCurrency(funnel.billed.today.revenue) }}</div>
              <div class="funnel-tile-sub">{{ funnel.billed.today.count }} invoices</div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Billed This Week</div>
              <div class="funnel-tile-value" data-testid="billed-week">{{ formatCurrency(funnel.billed.this_week.revenue) }}</div>
              <div class="funnel-tile-sub">{{ funnel.billed.this_week.count }} invoices</div>
            </div>
          </div>
        </template>
      </Card>

      <!-- Operations KPIs -->
      <Card v-if="canSeePipeline && opsLoaded" class="ops-card" data-testid="operations-card">
        <template #title><i class="pi pi-wrench" style="color:#0ea5e9" /> Operations (last 30 days)</template>
        <template #content>
          <div class="ops-grid">
            <div class="funnel-tile">
              <div class="funnel-tile-label">First-Time Fix Rate</div>
              <div class="funnel-tile-value" data-testid="first-time-fix">
                {{ ops.first_time_fix.rate === null ? '—' : formatPercent(ops.first_time_fix.rate) }}
              </div>
              <div class="funnel-tile-sub">
                {{ ops.first_time_fix.callbacks }} callbacks of {{ ops.first_time_fix.completed }} completed
              </div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Same-Day Booking</div>
              <div class="funnel-tile-value" data-testid="same-day-rate">
                {{ ops.response_speed.same_day_rate === null ? '—' : formatPercent(ops.response_speed.same_day_rate) }}
              </div>
              <div class="funnel-tile-sub">
                Same+next: {{ ops.response_speed.same_or_next_day_rate === null ? '—' : formatPercent(ops.response_speed.same_or_next_day_rate) }}
              </div>
            </div>
            <div class="funnel-tile unavailable">
              <div class="funnel-tile-label">Avg Job Duration</div>
              <div class="funnel-tile-value">—</div>
              <div class="funnel-tile-sub" :title="ops.avg_job_duration.unavailable_reason">data not yet captured</div>
            </div>
            <div class="funnel-tile unavailable">
              <div class="funnel-tile-label">Tech Utilization</div>
              <div class="funnel-tile-value">—</div>
              <div class="funnel-tile-sub" :title="ops.tech_utilization.unavailable_reason">data not yet captured</div>
            </div>
          </div>
        </template>
      </Card>

      <!-- Cash & Risk KPIs -->
      <Card v-if="canSeePipeline && cashLoaded" class="cash-card" data-testid="cash-risk-card">
        <template #title><i class="pi pi-dollar" style="color:#dc2626" /> Cash & Risk</template>
        <template #content>
          <div class="cash-grid">
            <div class="cash-tile">
              <div class="funnel-tile-label">AR Outstanding</div>
              <div class="funnel-tile-value" data-testid="ar-total">{{ formatCurrency(cash.ar_aging.total_outstanding) }}</div>
              <div class="aging-bars">
                <div v-for="(b, k) in cash.ar_aging.buckets" :key="k" class="aging-bar">
                  <span class="aging-bar-label">{{ b.label }}</span>
                  <span class="aging-bar-count">{{ b.count }}</span>
                  <span class="aging-bar-dollars">{{ formatCurrency(b.total) }}</span>
                </div>
              </div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Gross Margin (30d)</div>
              <div class="funnel-tile-value" data-testid="gross-margin">
                {{ cash.gross_margin.margin_pct === null ? '—' : formatPercent(cash.gross_margin.margin_pct) }}
              </div>
              <div class="funnel-tile-sub">
                {{ formatCurrency(cash.gross_margin.net_profit) }} on {{ formatCurrency(cash.gross_margin.total_sell) }}
              </div>
              <div v-if="cash.gross_margin.estimates_with_manual_lines > 0" class="pipeline-warn">
                ⚠ {{ cash.gross_margin.estimates_with_manual_lines }} estimate(s) had manual-priced lines (excluded)
              </div>
            </div>
            <div class="funnel-tile">
              <div class="funnel-tile-label">Warranty Callbacks (30d)</div>
              <div class="funnel-tile-value" data-testid="warranty-rate">
                {{ cash.warranty_callbacks.rate === null ? '—' : formatPercent(cash.warranty_callbacks.rate) }}
              </div>
              <div class="funnel-tile-sub">
                {{ cash.warranty_callbacks.filed }} filed of {{ cash.warranty_callbacks.completed_jobs }} completed
              </div>
            </div>
          </div>
        </template>
      </Card>

      <!-- Needs Attention section -->
      <Card v-if="attentionItems.length" class="attention-card" data-testid="needs-attention">
        <template #title><i class="pi pi-exclamation-triangle" style="color:#f59e0b"></i> Needs Attention</template>
        <template #content>
          <div class="attention-list">
            <div v-for="item in attentionItems" :key="item.id" class="attention-item" @click="router.push(item.link)">
              <Tag :value="item.type" :severity="item.severity" size="small" />
              <span class="attention-text">{{ item.text }}</span>
            </div>
          </div>
        </template>
      </Card>

      <div class="dashboard-grid">
        <Card class="activity-card">
          <template #title>Recent Activity</template>
          <template #content>
            <ul class="activity-list" data-testid="recent-activity-list">
              <li v-for="item in recentActivity" :key="item.id" class="activity-item" :data-testid="`activity-item-${item.id}`">
                <span class="activity-title"><i v-if="item.icon" :class="item.icon" aria-hidden="true" style="margin-right:0.4rem;opacity:0.6" />{{ item.title }}</span>
                <span class="activity-meta">{{ item.meta }}</span>
              </li>
              <li v-if="!recentActivity.length" class="activity-empty">No recent job activity.</li>
            </ul>
          </template>
        </Card>

        <Card class="todays-jobs-card">
          <template #title>Today's Schedule</template>
          <template #content>
            <ul class="activity-list">
              <li v-for="job in todaysJobs" :key="job.id" class="activity-item" @click="router.push(`/jobs/${job.id}`)">
                <span class="activity-title">{{ job.customer_name || 'Customer' }} — {{ jobDisplayTitle(job) }}</span>
                <span class="activity-meta">{{ job.technician_name || 'Unassigned' }} · {{ job.time_window || job.scheduled_time || 'Anytime' }}</span>
              </li>
              <li v-if="!todaysJobs.length" class="activity-empty">No jobs scheduled today.</li>
            </ul>
          </template>
        </Card>
      </div>
    </section>

    <!-- Service Call Intake Dialog -->
    <Dialog v-model:visible="showServiceCallDialog" header="New Service Call" :style="{ width: '500px' }" modal>
      <div class="flex flex-column gap-3 mt-2">
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Customer Name *</label>
          <InputText v-model="scForm.customer_name" placeholder="Who is calling?" data-testid="sc-name" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Phone</label>
          <InputText v-model="scForm.customer_phone" placeholder="(555) 000-0000" data-testid="sc-phone" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Urgency</label>
          <Select v-model="scForm.urgency" :options="['normal','urgent','emergency']" data-testid="sc-urgency" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Problem Description *</label>
          <Textarea v-model="scForm.problem_description" rows="3" placeholder="What's the issue? e.g., Door won't open, spring broke..." data-testid="sc-problem" />
        </div>
        <div class="flex flex-column gap-1">
          <label class="font-semibold">Preferred Time</label>
          <InputText v-model="scForm.preferred_window" placeholder="ASAP, Tomorrow morning, etc." data-testid="sc-window" />
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" @click="showServiceCallDialog = false" />
        <Button label="Create Service Call" icon="pi pi-phone" severity="warn" :loading="scSubmitting"
          :disabled="!scForm.customer_name || !scForm.problem_description" @click="submitServiceCall" data-testid="sc-submit" />
      </template>
    </Dialog>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { useAuthStore } from "../stores/auth";
import { normalizeRole } from "../constants/roles";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Skeleton from "primevue/skeleton";
import Tag from "primevue/tag";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import Select from "primevue/select";

const api = useApi();
const router = useRouter();
const toast = useToast();
const auth = useAuthStore();
const canSeePipeline = computed(() => {
  // Mirrors server gate: owner/admin/dispatcher/sales/accounting/manager.
  // Technician + viewer hidden — same rule as EstimateProfitPanel.
  // normalizeRole maps the short DB form (e.g. 'dispatch') to canonical
  // ('dispatcher') so it matches the long-form allowlist below.
  const r = normalizeRole(auth.role);
  return ['owner', 'admin', 'dispatcher', 'sales', 'accounting', 'manager'].includes(r);
});
const pipeline = ref({ count: 0, total_sell: 0, total_cost: 0, net_profit: 0, blended_margin: 0, estimates_with_manual_lines: 0 });
const pipelineLoaded = ref(false);

// Sales funnel + ops + cash KPI tiles (sprint S107).
const EMPTY_SOLD = { count: 0, door_count: 0, dollar_amount: 0, avg_ticket: 0 };
const funnel = ref({
  sold: { today: { ...EMPTY_SOLD }, this_week: { ...EMPTY_SOLD }, last_30_days: { ...EMPTY_SOLD } },
  billed: { today: { revenue: 0, count: 0 }, this_week: { revenue: 0, count: 0 } },
  close_rate: { rate: null, accepted: 0, decisions: 0, window_days: 30 },
  estimates_outstanding: {
    count: 0,
    dollar_amount: 0,
    buckets: {
      lt_3d: { label: '0-2 days', count: 0, dollar_amount: 0 },
      d3_7: { label: '3-7 days', count: 0, dollar_amount: 0 },
      d8_14: { label: '8-14 days', count: 0, dollar_amount: 0 },
      gt_14: { label: '15+ days', count: 0, dollar_amount: 0 },
    },
  },
});
const funnelLoaded = ref(false);
const ops = ref({
  first_time_fix: { rate: null, completed: 0, callbacks: 0, window_days: 30 },
  response_speed: { same_day_rate: null, same_or_next_day_rate: null, same_day: 0, next_day: 0, total_booked: 0, window_days: 30 },
  avg_job_duration: { value: null, unavailable_reason: '' },
  tech_utilization: { value: null, unavailable_reason: '' },
});
const opsLoaded = ref(false);
const cash = ref({
  ar_aging: {
    buckets: {
      current: { label: 'Current (0-30)', count: 0, total: 0 },
      d31_60: { label: '31-60 days', count: 0, total: 0 },
      d61_90: { label: '61-90 days', count: 0, total: 0 },
      d90_plus: { label: '90+ days', count: 0, total: 0 },
    },
    total_outstanding: 0,
  },
  gross_margin: { margin_pct: null, total_sell: 0, total_cost: 0, net_profit: 0, estimates_with_manual_lines: 0, window_days: 30 },
  warranty_callbacks: { rate: null, filed: 0, completed_jobs: 0, window_days: 30 },
});
const cashLoaded = ref(false);

const soldWindows = computed(() => [
  { key: 'today', label: 'Today', data: funnel.value.sold.today },
  { key: 'week', label: 'This Week', data: funnel.value.sold.this_week },
  { key: '30d', label: 'Last 30 Days', data: funnel.value.sold.last_30_days },
]);
const showServiceCallDialog = ref(false);
const scForm = ref({ customer_name: "", customer_phone: "", problem_description: "", urgency: "normal", preferred_window: "" });
const scSubmitting = ref(false);

const PERIOD_OPTIONS = [
  { label: "Last 7 days", value: "week", days: 6 },
  { label: "Last 30 days", value: "month", days: 29 },
  { label: "Last 90 days", value: "quarter", days: 89 },
  { label: "Year to date", value: "ytd", days: null },
];
const period = ref("month");

function periodRange(value) {
  const today = new Date();
  const end = today.toISOString().slice(0, 10);
  if (value === "ytd") {
    const jan1 = new Date(today.getFullYear(), 0, 1);
    return { start_date: jan1.toISOString().slice(0, 10), end_date: end };
  }
  const opt = PERIOD_OPTIONS.find((o) => o.value === value) || PERIOD_OPTIONS[1];
  const start = new Date(today);
  start.setDate(today.getDate() - opt.days);
  return { start_date: start.toISOString().slice(0, 10), end_date: end };
}

const periodLabel = computed(
  () => (PERIOD_OPTIONS.find((o) => o.value === period.value) || PERIOD_OPTIONS[1]).label,
);

async function submitServiceCall() {
  scSubmitting.value = true;
  try {
    await api.post("/api/service-calls", scForm.value);
    toast.add({ severity: "success", summary: "Service Call Created", detail: "Added to dispatch queue", life: 3000 });
    showServiceCallDialog.value = false;
    scForm.value = { customer_name: "", customer_phone: "", problem_description: "", urgency: "normal", preferred_window: "" };
    loadDashboard();
  } catch { toast.add({ severity: "error", summary: "Error", detail: "Failed to create service call", life: 4000 }); }
  finally { scSubmitting.value = false; }
}

const summary = ref({
  revenue_total: 0,
  open_jobs: 0,
  assigned_jobs_today: 0,
  overdue_invoices: 0,
  jobs_completed: 0,
  revenue_trend: 0,
  open_jobs_trend: 0,
  overdue_invoices_trend: 0,
  jobs_completed_trend: 0,
});

const readyForBillingCount = ref(0);

async function loadReadyForBilling() {
  try {
    // suppressErrorToast: a user without `invoices.read_all` will 403 here
    // on every dashboard load — without the suppression they'd get a
    // "Permission denied" toast every time. The Needs-Attention entry
    // gates on count > 0, so a silent 403 just means it doesn't render.
    const rows = await api.get('/api/jobs/ready-for-billing', { suppressErrorToast: true });
    readyForBillingCount.value = Array.isArray(rows) ? rows.length : 0;
  } catch {
    readyForBillingCount.value = 0;
  }
}

// 2026-05-09 (UX sprint Phase 10) — loading flag prevents KPI tiles from
// flashing "0" → real-value, which looked to users like data dropped to zero.
// Skeleton renders in place of the formatted value while summary is in flight.
const summaryLoading = ref(true);

const recentActivity = ref([]);
const todaysJobs = ref([]);

// "Needs Attention" — exception-based items for the owner
const attentionItems = computed(() => {
  const items = [];
  const s = summary.value;
  if (toNumber(s.overdue_invoices) > 0) {
    items.push({ id: 'overdue', type: 'Overdue', severity: 'danger', text: `${s.overdue_invoices} overdue invoices need collection`, link: '/billing?status=Overdue' });
  }
  const unassigned = todaysJobs.value.filter((j) => !j.technician_name || j.technician_name === 'Unassigned').length;
  if (unassigned > 0) {
    items.push({ id: 'unassigned', type: 'Unassigned', severity: 'warning', text: `${unassigned} jobs today have no technician assigned`, link: '/dispatch' });
  }
  if (toNumber(s.open_jobs) > 100) {
    items.push({ id: 'backlog', type: 'Backlog', severity: 'info', text: `${s.open_jobs} open jobs — consider scheduling more capacity`, link: '/jobs' });
  }
  // Ready for Billing — completed jobs that nobody has invoiced. Doug
  // 2026-05-10: a completed job vanished into the completed bucket with
  // no parts/labor logged and no invoice. The ready-for-billing endpoint
  // catches them; surfacing the count here ensures it doesn't get missed.
  const rfb = toNumber(readyForBillingCount.value);
  if (rfb > 0) {
    items.push({
      id: 'ready-for-billing',
      type: 'Ready to Bill',
      severity: 'success',
      text: `${rfb} completed job${rfb === 1 ? '' : 's'} ready to invoice — review and send`,
      link: '/billing',
    });
  }
  return items;
});

function toNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(toNumber(value));
}

function formatPercent(value) {
  const v = toNumber(value);
  return `${(v * 100).toFixed(1)}%`;
}

function toTrendMeta(value) {
  // Backend returns null when prior period is too small to produce a
  // meaningful percentage (Phase D audit fix — was rendering 709,468.6%).
  if (value === null || value === undefined || value === "") {
    return { trendArrow: "", trendDirection: "trend-flat", trendLabel: "—" };
  }
  const trend = toNumber(value);
  return {
    trendArrow: trend >= 0 ? "↑" : "↓",
    trendDirection: trend >= 0 ? "trend-up" : "trend-down",
    trendLabel: `${Math.abs(trend)}%`,
  };
}

const kpis = computed(() => {
  const revenue = toTrendMeta(summary.value.revenue_trend);
  const openJobs = toTrendMeta(summary.value.open_jobs_trend);
  const overdue = toTrendMeta(summary.value.overdue_invoices_trend);
  // jobs_completed_trend dropped — Completed Today below renders a
  // neutral "today" subtitle instead of a period delta (F-005 fix).

  return [
    {
      key: "revenue",
      label: `Revenue Billed (${periodLabel.value})`,
      formattedValue: formatCurrency(summary.value.revenue_total),
      ...revenue,
    },
    {
      key: "open-jobs",
      label: "Open Jobs",
      formattedValue: String(toNumber(summary.value.open_jobs)),
      ...openJobs,
    },
    // UX audit F-27 / 2026-04-29 — distinct from Open Jobs:
    //   Open Jobs = anywhere in scheduled/in_progress (the backlog)
    //   Assigned Jobs = on the board today + assigned to a tech
    // Mirrors the Dispatch view's count so the two pages can never disagree.
    {
      key: "assigned-jobs-today",
      label: "Assigned Today",
      formattedValue: String(toNumber(summary.value.assigned_jobs_today)),
      trendArrow: "",
      trendLabel: "scheduled today + assigned",
      trendDirection: "neutral",
    },
    {
      key: "overdue-invoices",
      label: "Overdue Invoices",
      formattedValue: String(toNumber(summary.value.overdue_invoices || 0)),
      ...overdue,
    },
    {
      key: "completed-today",
      label: "Jobs Completed Today",
      // Phase D audit fix 2026-04-27: was reading summary.jobs_completed
      // (period total — 159 across the last 30 days). For a "today"
      // KPI, daily-snapshot.jobs_completed_today is the correct field.
      formattedValue: String(
        toNumber(summary.value.jobs_completed_today ?? summary.value.jobs_completed ?? 0),
      ),
      // F-005 (audit 2026-04-29): the value is a daily count but
      // jobs_completed_trend is a 30-day period-over-period delta —
      // different time windows. Rendered "100%" next to a today count
      // of 0, which is meaningless. Drop the trend; mirror the neutral
      // subtitle pattern used by Assigned Today above.
      trendArrow: "",
      trendLabel: "today",
      trendDirection: "neutral",
    },
  ];
});

function formatTimestamp(value) {
  if (!value) return "Updated recently";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Updated recently";
  return parsed.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function mapActivity(job) {
  const customer = job.customer_name || job.customer || "Customer";
  const title = job.title || `Job ${job.id}`;
  return {
    id: job.id,
    title: `${title} - ${customer}`,
    meta: formatTimestamp(job.updated_at || job.created_at),
  };
}

async function loadSummary() {
  const { start_date, end_date } = periodRange(period.value);
  try {
    // Try the reports summary endpoint first (gives server-computed KPIs).
    // Period passed through so trend deltas reflect the selected window.
    const data = await api.get(
      `/api/reports/summary?start_date=${start_date}&end_date=${end_date}`,
    );
    if (data) {
      summary.value = { ...summary.value, ...data };
    }
  } catch {
    // reports_advanced module may not be enabled — fall back to client-side
  }

  // Also fetch daily snapshot — provides today-only metrics (revenue,
  // jobs completed today, new jobs today) plus true overdue separate
  // from the open backlog.
  try {
    const snapshot = await api.get("/api/reports/daily-snapshot");
    if (snapshot) {
      summary.value.jobs_completed_today = snapshot.jobs_completed_today ?? 0;
      summary.value.assigned_jobs_today = snapshot.assigned_jobs_today ?? 0;
      if (summary.value.overdue_invoices == null || summary.value.overdue_invoices === 0) {
        summary.value.overdue_invoices =
          snapshot.overdue_invoices_count ?? summary.value.overdue_invoices ?? 0;
      }
    }
  } catch {
    // daily-snapshot may also be gated — ignore
  }

  // If reports endpoints failed or returned zeros, compute from raw data
  if (!summary.value.revenue_total && !summary.value.open_jobs) {
    try {
      const [jobsData, invoicesData] = await Promise.all([
        api.get("/api/jobs"),
        api.get("/api/invoices"),
      ]);
      const jobList = Array.isArray(jobsData)
        ? jobsData
        : jobsData?.items || jobsData?.data || [];
      const invList = Array.isArray(invoicesData)
        ? invoicesData
        : invoicesData?.items || invoicesData?.data || [];

      const now = new Date();
      const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
      const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());

      summary.value.open_jobs = jobList.filter(
        (j) =>
          j.status &&
          !["Complete", "completed", "Invoiced", "Cancelled"].includes(j.status),
      ).length;

      summary.value.jobs_completed = jobList.filter((j) => {
        if (j.status !== "Complete" && j.status !== "completed") return false;
        const ts = j.completed_at || j.updated_at;
        return ts && new Date(ts) >= todayStart;
      }).length;

      summary.value.overdue_invoices = invList.filter((i) => {
        if (i.status === "paid") return false;
        if (i.status === "overdue") return true;
        return i.due_date && new Date(i.due_date) < now && i.status !== "paid";
      }).length;

      summary.value.revenue_total = invList
        .filter((i) => {
          const ts = i.created_at || i.invoice_date;
          return i.status === "paid" && ts && new Date(ts) >= monthStart;
        })
        .reduce((sum, i) => sum + (Number(i.total_amount) || 0), 0);
    } catch {
      // jobs/invoices endpoints unavailable — leave defaults
    }
  }
  summaryLoading.value = false;
}

const ACTIVITY_LABELS = {
  login: "Signed in",
  login_success: "Signed in",
  login_failed: "Failed login attempt",
  logout: "Signed out",
  token_refreshed: "Session renewed",
  customer_created: "New customer added",
  customer_updated: "Customer updated",
  customer_deleted: "Customer removed",
  job_created: "New job created",
  job_updated: "Job updated",
  job_completed: "Job completed",
  estimate_created: "New estimate created",
  estimate_updated: "Estimate updated",
  estimate_accepted: "Estimate accepted",
  invoice_created: "New invoice created",
  invoice_paid: "Invoice paid",
  module_enabled: "Module enabled",
  module_disabled: "Module disabled",
  settings_updated: "Settings changed",
  user_created: "New user added",
  user_updated: "User updated",
};

const ENTITY_ICONS = {
  auth: "pi pi-sign-in",
  customer: "pi pi-users",
  job: "pi pi-briefcase",
  estimate: "pi pi-file-edit",
  invoice: "pi pi-dollar",
  module: "pi pi-th-large",
  settings: "pi pi-cog",
  user: "pi pi-user",
};

function formatActivityTitle(action, entityType) {
  if (ACTIVITY_LABELS[action]) return ACTIVITY_LABELS[action];
  // Fallback: title-case the action and replace underscores
  const label = action.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  if (entityType && entityType !== "auth") return `${label} (${entityType})`;
  return label;
}

// 2026-04-29 nav-cleanup: filter session/token-refresh noise from the
// dashboard's "Recent Activity" — they were drowning real activity 9-of-10.
const ACTIVITY_HIDE_ACTIONS = new Set([
  "token_refreshed",
  "session_renewed",
  "refresh_replay_detected",
  "auth_login",
  "auth_logout",
]);
const ACTIVITY_HIDE_ENTITIES = new Set(["auth", "session"]);

async function loadRecentActivity() {
  // Try audit log first — gives richer cross-entity activity
  try {
    // Pull a wider window so we can filter and still have ~10 user-visible events.
    const data = await api.get("/api/audit/logs?page=1&page_size=50");
    const items = (data?.items || []).filter((evt) => {
      if (ACTIVITY_HIDE_ACTIONS.has(evt.action)) return false;
      if (ACTIVITY_HIDE_ENTITIES.has(evt.entity_type)) return false;
      return true;
    });
    if (items.length) {
      recentActivity.value = items.slice(0, 10).map((evt) => ({
        id: evt.id,
        title: formatActivityTitle(evt.action, evt.entity_type),
        icon: ENTITY_ICONS[evt.entity_type] || "pi pi-circle",
        meta: formatTimestamp(evt.created_at),
      }));
      return;
    }
  } catch {
    // audit endpoint may require admin — fall back to jobs
  }

  // Fallback: recent jobs
  try {
    const data = await api.get("/api/jobs");
    const jobs = Array.isArray(data) ? data : data?.items || data?.data || [];
    recentActivity.value = jobs
      .slice()
      .sort((a, b) => {
        const aTime = new Date(a.updated_at || a.created_at || 0).getTime();
        const bTime = new Date(b.updated_at || b.created_at || 0).getTime();
        return bTime - aTime;
      })
      .slice(0, 10)
      .map(mapActivity);
  } catch {
    // no activity data available
  }
}

// 2026-04-29 nav-cleanup: when title is the QB-import boilerplate
// ("QuickBooks Import" / "QuickBooks Import — <name>"), prefer the actual
// job_type so the dashboard reads "<customer> — Service Call" instead of
// "<customer> — QuickBooks Import" for every imported job.
function jobDisplayTitle(job) {
  const t = (job?.title || "").trim();
  // Match both "QuickBooks Import — <name>" and the older "QB Import" form.
  const isQbBoilerplate = /^(quickbooks|qb)\s+import(\s*[—-].*)?$/i.test(t);
  if (!t || isQbBoilerplate) return job?.job_type || "Service";
  return t;
}

async function loadTodaysJobs() {
  try {
    const today = new Date().toISOString().split("T")[0];
    const data = await api.get(`/api/jobs?date=${today}&page_size=10`);
    const list = Array.isArray(data) ? data : data?.items || data?.data || [];
    todaysJobs.value = list.slice(0, 10);
  } catch {
    todaysJobs.value = [];
  }
}

async function loadPipeline() {
  if (!canSeePipeline.value) return;
  try {
    const data = await api.get("/api/estimates/pipeline-summary");
    if (data) {
      pipeline.value = data;
      pipelineLoaded.value = true;
    }
  } catch {
    // estimates module may be ungranted on this tenant — leave card hidden
  }
}

async function loadFunnel() {
  if (!canSeePipeline.value) return;
  try {
    const data = await api.get('/api/reports/sales-funnel');
    if (data) { funnel.value = data; funnelLoaded.value = true; }
  } catch { /* reports_advanced may be ungranted */ }
}
async function loadOps() {
  if (!canSeePipeline.value) return;
  try {
    const data = await api.get('/api/reports/operations');
    if (data) { ops.value = data; opsLoaded.value = true; }
  } catch { /* reports_advanced may be ungranted */ }
}
async function loadCash() {
  if (!canSeePipeline.value) return;
  try {
    const data = await api.get('/api/reports/cash-risk');
    if (data) { cash.value = data; cashLoaded.value = true; }
  } catch { /* reports_advanced may be ungranted */ }
}

async function loadDashboard() {
  await Promise.all([
    loadSummary(),
    loadRecentActivity(),
    loadTodaysJobs(),
    loadPipeline(),
    loadFunnel(),
    loadOps(),
    loadCash(),
    loadReadyForBilling(),
  ]);
}

onMounted(() => {
  loadDashboard();
});
</script>

<style scoped>
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(160px, 1fr));
  gap: 0.75rem;
}

.kpi-card {
  border: 1px solid color-mix(in srgb, var(--accent) 15%, transparent);
}

.kpi-body {
  display: grid;
  gap: 0.35rem;
}

.kpi-value {
  font-size: 1.6rem;
  font-weight: 700;
}

.kpi-trend {
  font-size: 0.9rem;
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
}

.trend-up {
  color: #1f9d55;
}

.trend-down {
  color: #d93025;
}

.dashboard-actions {
  margin: 1rem 0;
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}

@media (max-width: 900px) {
  .dashboard-grid { grid-template-columns: 1fr; }
}

.activity-item {
  cursor: pointer;
  transition: background 0.15s;
  padding: 0.5rem;
  border-radius: 4px;
}

.activity-item:hover {
  background: var(--p-content-hover-background, rgba(255, 255, 255, 0.03));
}

.activity-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  gap: 0.55rem;
}

.activity-item {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  border-bottom: 1px solid color-mix(in srgb, var(--text) 12%, transparent);
  padding-bottom: 0.45rem;
}

.activity-meta {
  color: var(--muted);
  font-size: 0.85rem;
}

.activity-empty {
  color: var(--muted);
}

@media (max-width: 1100px) {
  .kpi-grid {
    grid-template-columns: repeat(2, minmax(150px, 1fr));
  }
}

@media (max-width: 640px) {
  .kpi-grid {
    grid-template-columns: 1fr;
  }
}

/* Estimates Pipeline */
.pipeline-card { margin-bottom: 1rem; border-left: 4px solid #2563eb; }
.pipeline-title-row { display: flex; justify-content: space-between; align-items: center; }
.pipeline-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(140px, 1fr));
  gap: 1rem;
}
@media (max-width: 700px) {
  .pipeline-grid { grid-template-columns: 1fr; }
}
.pipeline-stat { display: grid; gap: 0.25rem; }
.pipeline-label { font-size: 0.85rem; color: var(--muted, #64748b); }
.pipeline-value { font-size: 1.6rem; font-weight: 700; }
.pipeline-value.negative { color: #d93025; }
.pipeline-sub { font-size: 0.8rem; color: var(--muted, #64748b); }
.pipeline-warn { font-size: 0.8rem; color: #b45309; margin-top: 0.6rem; margin-bottom: 0; }

/* Sales Funnel */
.funnel-card { margin-bottom: 1rem; border-left: 4px solid #16a34a; }
.ops-card { margin-bottom: 1rem; border-left: 4px solid #0ea5e9; }
.cash-card { margin-bottom: 1rem; border-left: 4px solid #dc2626; }

.funnel-section-title { font-size: 0.95rem; margin: 0 0 0.6rem; color: var(--muted, #64748b); text-transform: uppercase; letter-spacing: 0.04em; }
.sold-grid { display: grid; gap: 0.6rem; margin-bottom: 1rem; }
.sold-window { padding: 0.6rem 0.8rem; border: 1px solid color-mix(in srgb, var(--text) 10%, transparent); border-radius: 6px; }
.sold-window-label { font-weight: 600; font-size: 0.85rem; color: var(--muted, #64748b); margin-bottom: 0.4rem; text-transform: uppercase; letter-spacing: 0.03em; }
.sold-window-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.75rem; }
.sold-stat-label { font-size: 0.75rem; color: var(--muted, #64748b); }
.sold-stat-value { font-size: 1.2rem; font-weight: 700; }

.funnel-row, .ops-grid, .cash-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.75rem;
  margin-top: 0.6rem;
}
.cash-grid { grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
.funnel-tile, .cash-tile { padding: 0.6rem 0.8rem; border: 1px solid color-mix(in srgb, var(--text) 10%, transparent); border-radius: 6px; }
.funnel-tile-label { font-size: 0.8rem; color: var(--muted, #64748b); }
.funnel-tile-value { font-size: 1.4rem; font-weight: 700; margin: 0.15rem 0; }
.funnel-tile-sub { font-size: 0.78rem; color: var(--muted, #64748b); }
.funnel-tile.unavailable { opacity: 0.6; }

.aging-bars { display: grid; gap: 0.25rem; margin-top: 0.4rem; }
.aging-bar { display: grid; grid-template-columns: 1fr auto auto; gap: 0.6rem; font-size: 0.8rem; align-items: baseline; }
.aging-bar-label { color: var(--muted, #64748b); }
.aging-bar-count { font-weight: 600; }
.aging-bar-dollars { color: var(--text); }

@media (max-width: 700px) {
  .sold-window-row { grid-template-columns: repeat(2, 1fr); }
}

/* Needs Attention */
.attention-card { margin-bottom: 1rem; border-left: 4px solid #f59e0b; }
.attention-list { display: flex; flex-direction: column; gap: 0.4rem; }
.attention-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem; border-radius: 6px; cursor: pointer; }
.attention-item:hover { background: rgba(245, 158, 11, 0.1); }
.attention-text { font-size: 0.9rem; }
</style>
