<template>
  <div class="portal-wrapper" data-testid="customer-portal-root">
    <header class="portal-header">
      <div class="logo-container">
        <i class="pi pi-building" style="font-size: 1.5rem" />
        <span class="company-name">{{ company.name }}</span>
      </div>
    </header>

    <main class="portal-content">
      <div v-if="loading" class="loading-wrap"><ProgressSpinner /></div>

      <div v-else-if="error" class="auth-error" data-testid="error-message">
        <Message severity="warn" class="m-3">{{ error }}</Message>
        <Card class="request-link-card" data-testid="request-link-card">
          <template #title>Get a new sign-in link</template>
          <template #content>
            <p class="meta">Enter the email address your service provider has on file and we'll send you a fresh link.</p>
            <div class="request-link-row">
              <InputText v-model="requestEmail" placeholder="you@example.com" class="flex-1" data-testid="request-link-email" @keyup.enter="requestNewLink" />
              <Button label="Send Link" icon="pi pi-envelope" :loading="requestSending" @click="requestNewLink" data-testid="request-link-btn" />
            </div>
            <p v-if="requestSent" class="meta sent-note" data-testid="request-link-sent">If that email is on file, a sign-in link is on its way.</p>
          </template>
        </Card>
      </div>

      <div v-else>
        <Tabs value="estimates" data-testid="portal-tabs">
          <TabList>
            <Tab value="estimates">Estimates</Tab>
            <Tab value="invoices">Invoices</Tab>
            <Tab value="jobs">Jobs</Tab>
            <Tab value="contact">Contact</Tab>
          </TabList>
          <TabPanels>
          <TabPanel value="estimates">
            <div v-if="!estimates.length" class="empty-msg">No estimates available.</div>
            <div v-else class="card-grid">
              <Card v-for="est in estimates" :key="est.id" class="portal-card clickable" data-testid="estimate-card" @click="openEstimate(est.id)">
                <template #title>
                  <div class="card-title-row">
                    <span>{{ est.label || est.estimate_number }}</span>
                    <Tag :value="est.status" :severity="statusSeverity(est.status)" />
                  </div>
                </template>
                <template #content>
                  <p class="amount">{{ currency(est.total) }}</p>
                  <p v-if="est.description" class="meta">{{ est.description }}</p>
                  <p class="meta">Sent: {{ formatDate(est.sent_at || est.created_at) }}</p>
                  <p v-if="est.valid_until" class="meta">Valid until: {{ formatDate(est.valid_until) }}</p>
                  <p class="meta view-hint"><i class="pi pi-eye" /> View details</p>
                </template>
                <template #footer>
                  <div v-if="est.status === 'sent'" class="action-row">
                    <Button label="Accept" icon="pi pi-check" severity="success" class="flex-1" :loading="actionBusy[est.id]" @click.stop="acceptEstimate(est.id)" data-testid="accept-btn" />
                    <Button label="Decline" icon="pi pi-times" severity="danger" outlined class="flex-1" :loading="actionBusy[est.id]" @click.stop="declineEstimate(est.id)" data-testid="decline-btn" />
                  </div>
                </template>
              </Card>
            </div>
          </TabPanel>

          <TabPanel value="invoices">
            <DataTable :value="invoices" responsiveLayout="stack" breakpoint="640px" data-testid="invoices-table">
              <template #empty>No invoices found.</template>
              <Column field="invoice_number" header="Invoice #" />
              <Column field="total" header="Amount"><template #body="{ data }">{{ currency(data.total) }}</template></Column>
              <Column field="balance_due" header="Balance Due"><template #body="{ data }">{{ currency(data.balance_due) }}</template></Column>
              <Column field="payment_status" header="Status"><template #body="{ data }"><Tag :value="data.payment_status" :severity="statusSeverity(data.payment_status)" /></template></Column>
              <Column field="due_date" header="Due"><template #body="{ data }">{{ formatDate(data.due_date) }}</template></Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="jobs">
            <DataTable :value="jobs" responsiveLayout="stack" breakpoint="640px" data-testid="jobs-table">
              <template #empty>No jobs found.</template>
              <Column field="title" header="Job" />
              <Column field="lifecycle_stage" header="Status"><template #body="{ data }"><Tag :value="jobStatusLabel(data)" :severity="statusSeverity(data.lifecycle_stage)" /></template></Column>
              <Column field="scheduled_at" header="Scheduled"><template #body="{ data }">{{ formatDate(data.scheduled_at) }}</template></Column>
              <Column field="completed_at" header="Completed"><template #body="{ data }">{{ formatDate(data.completed_at) }}</template></Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="contact">
            <Card data-testid="contact-card">
              <template #title>Contact Us</template>
              <template #content>
                <div class="contact-list">
                  <div v-if="company.phone"><i class="pi pi-phone" /> {{ company.phone }}</div>
                  <div v-if="company.email"><i class="pi pi-envelope" /> {{ company.email }}</div>
                  <div v-if="company.address"><i class="pi pi-map-marker" /> {{ company.address }}</div>
                  <div v-if="!company.phone && !company.email && !company.address" class="empty-msg">
                    Contact details are not available yet.
                  </div>
                </div>
              </template>
            </Card>
          </TabPanel>
          </TabPanels>
        </Tabs>

        <Dialog
          v-model:visible="detailVisible"
          :header="detail ? (detail.label || detail.estimate_number) : 'Estimate'"
          :modal="true"
          :style="{ width: 'min(640px, 94vw)' }"
          data-testid="estimate-detail-dialog"
        >
          <div v-if="detailLoading" class="loading-wrap"><ProgressSpinner /></div>
          <div v-else-if="detail" class="detail-body">
            <div class="detail-status-row">
              <Tag :value="detail.status" :severity="statusSeverity(detail.status)" />
              <span class="meta">Sent: {{ formatDate(detail.sent_at || detail.created_at) }}</span>
              <span v-if="detail.valid_until" class="meta">Valid until: {{ formatDate(detail.valid_until) }}</span>
            </div>
            <p v-if="detail.description" class="meta">{{ detail.description }}</p>
            <p v-if="detail.jobsite_address" class="meta"><i class="pi pi-map-marker" /> {{ detail.jobsite_address }}</p>

            <DataTable :value="detail.lines" class="detail-lines" data-testid="estimate-lines-table">
              <template #empty>No line items.</template>
              <Column field="description" header="Item" />
              <Column field="quantity" header="Qty" :style="{ width: '70px' }" />
              <Column field="unit_price" header="Price" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.unit_price) }}</template></Column>
              <Column field="line_total" header="Total" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.line_total) }}</template></Column>
            </DataTable>

            <div class="totals-block" v-if="detail.totals" data-testid="estimate-totals">
              <div class="totals-row"><span>Subtotal</span><span>{{ currency(detail.totals.subtotal) }}</span></div>
              <div class="totals-row" v-if="detail.totals.discount"><span>Discount</span><span>-{{ currency(detail.totals.discount) }}</span></div>
              <div class="totals-row" v-if="detail.totals.tax"><span>Tax ({{ detail.totals.tax_rate_pct }}%)</span><span>{{ currency(detail.totals.tax) }}</span></div>
              <div class="totals-row grand"><span>Total</span><span>{{ currency(detail.totals.total) }}</span></div>
              <p v-if="detail.totals.tax_unavailable" class="meta" data-testid="tax-unavailable-note">
                <i class="pi pi-info-circle" /> Tax could not be calculated — the final total may differ.
              </p>
            </div>

            <div v-if="detail.status === 'sent'" class="action-row detail-actions">
              <Button label="Accept" icon="pi pi-check" severity="success" class="flex-1" :loading="actionBusy[detail.id]" @click="acceptFromDetail" data-testid="detail-accept-btn" />
              <Button label="Decline" icon="pi pi-times" severity="danger" outlined class="flex-1" :loading="actionBusy[detail.id]" @click="declineFromDetail" data-testid="detail-decline-btn" />
            </div>
            <p v-else-if="detail.status === 'declined' && detail.declined_reason" class="meta">Declined: {{ detail.declined_reason }}</p>
          </div>
        </Dialog>
      </div>
    </main>
  </div>
</template>

<script setup>
import { reactive, ref, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";

const JWT_STORAGE_KEY = "gdx_portal_jwt";

const route = useRoute();
const router = useRouter();
const toast = useToast();
const jwt = ref("");
const loading = ref(true);
const error = ref(null);
const company = ref({ name: "Customer Portal", phone: "", email: "", address: "" });
const estimates = ref([]);
const invoices = ref([]);
const jobs = ref([]);
const actionBusy = reactive({});
const detail = ref(null);
const detailVisible = ref(false);
const detailLoading = ref(false);
const requestEmail = ref("");
const requestSending = ref(false);
const requestSent = ref(false);

function currency(v) { return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(v) || 0); }
function formatDate(d) { return d ? new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "-"; }
function statusSeverity(s) {
  const map = { sent: "info", accepted: "success", paid: "success", declined: "danger", unpaid: "warn", overdue: "danger", expired: "secondary", scheduled: "info", in_progress: "info", completed: "success" };
  return map[(s || "").toLowerCase()] || "secondary";
}
function jobStatusLabel(job) {
  return (job.lifecycle_stage || "").replace(/_/g, " ") || "-";
}

async function authedFetch(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${jwt.value}` },
  });
  if (res.status === 401) {
    sessionStorage.removeItem(JWT_STORAGE_KEY);
    throw Object.assign(new Error("unauthorized"), { auth: true });
  }
  if (!res.ok) throw new Error(`request failed: ${res.status}`);
  return res.json();
}

async function fetchAll() {
  const [ctx, est, inv, job] = await Promise.all([
    authedFetch("/portal/context"),
    authedFetch("/portal/estimates"),
    authedFetch("/portal/invoices"),
    authedFetch("/portal/jobs"),
  ]);
  if (ctx?.company) company.value = ctx.company;
  estimates.value = Array.isArray(est) ? est : [];
  invoices.value = Array.isArray(inv) ? inv : [];
  jobs.value = Array.isArray(job) ? job : [];
}

async function init() {
  loading.value = true;
  const magicToken = route.query.token;
  if (magicToken) {
    // Exchange the one-time emailed token for a session JWT, then drop it
    // from the URL so a refresh doesn't retry the already-consumed token.
    try {
      const res = await fetch(`/portal/verify?token=${encodeURIComponent(magicToken)}`);
      if (res.ok) {
        const body = await res.json();
        jwt.value = body.access_token || "";
        sessionStorage.setItem(JWT_STORAGE_KEY, jwt.value);
      }
    } catch { /* fall through to any stored session */ }
    router.replace({ query: {} });
  }
  if (!jwt.value) jwt.value = sessionStorage.getItem(JWT_STORAGE_KEY) || "";
  if (!jwt.value) {
    error.value = "This sign-in link is invalid or has expired.";
    loading.value = false;
    return;
  }
  try {
    await fetchAll();
    error.value = null;
  } catch (e) {
    error.value = e?.auth
      ? "Your portal session has expired."
      : "Failed to load portal data. Please try again later.";
  } finally {
    loading.value = false;
  }
}

async function requestNewLink() {
  const email = requestEmail.value.trim();
  if (!email) return;
  requestSending.value = true;
  try {
    await fetch("/portal/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    requestSent.value = true;
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Could not send link. Try again.", life: 4000 });
  } finally {
    requestSending.value = false;
  }
}

async function estimateAction(id, action, successMsg) {
  actionBusy[id] = true;
  try {
    await authedFetch(`/portal/estimates/${id}/${action}`, { method: "POST" });
    toast.add({ severity: action === "accept" ? "success" : "warn", summary: successMsg, life: 3000 });
    estimates.value = await authedFetch("/portal/estimates");
  } catch (e) {
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: `Could not ${action} estimate`, life: 4000 });
  } finally {
    actionBusy[id] = false;
  }
}

const acceptEstimate = (id) => estimateAction(id, "accept", "Estimate accepted");
const declineEstimate = (id) => estimateAction(id, "decline", "Estimate declined");

async function openEstimate(id) {
  detailVisible.value = true;
  detailLoading.value = true;
  try {
    detail.value = await authedFetch(`/portal/estimates/${id}`);
  } catch (e) {
    detailVisible.value = false;
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not load estimate", life: 4000 });
  } finally {
    detailLoading.value = false;
  }
}

async function actFromDetail(action, successMsg) {
  if (!detail.value) return;
  const id = detail.value.id;
  await estimateAction(id, action, successMsg);
  // Refresh the open dialog so the status/actions reflect the change.
  try { detail.value = await authedFetch(`/portal/estimates/${id}`); } catch { detailVisible.value = false; }
}

const acceptFromDetail = () => actFromDetail("accept", "Estimate accepted");
const declineFromDetail = () => actFromDetail("decline", "Estimate declined");

onMounted(init);
</script>

<style scoped>
/* PrimeVue v4 --p-* tokens flip with data-theme; the --surface-* names do not exist here. */
.portal-wrapper { min-height: 100vh; background: color-mix(in srgb, var(--p-content-background, #f3f4f6) 96%, var(--p-text-color, #000)); color: var(--p-text-color, #1e293b); }
.portal-header { background: var(--p-content-background, #fff); padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; justify-content: center; border-bottom: 1px solid var(--p-content-border-color, transparent); }
.logo-container { display: flex; align-items: center; gap: 0.75rem; }
.company-name { font-size: 1.25rem; font-weight: 700; color: var(--p-text-color, #1e293b); }
.portal-content { max-width: 900px; margin: 0 auto; padding: 1rem; }
.loading-wrap { display: flex; justify-content: center; padding: 3rem; }
.empty-msg { text-align: center; padding: 2rem; color: var(--p-text-muted-color, #6b7280); }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
.portal-card { transition: transform 0.15s; }
.portal-card.clickable { cursor: pointer; }
.portal-card:hover { transform: translateY(-2px); }
.view-hint { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.5rem; }
.detail-body { display: flex; flex-direction: column; gap: 0.75rem; }
.detail-status-row { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.totals-block { margin-left: auto; min-width: 240px; display: flex; flex-direction: column; gap: 0.35rem; }
.totals-row { display: flex; justify-content: space-between; font-size: 0.95rem; }
.totals-row.grand { font-weight: 700; font-size: 1.1rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb); padding-top: 0.35rem; color: var(--p-primary-color); }
.detail-actions { margin-top: 0.25rem; }
.card-title-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.amount { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); margin: 0.5rem 0; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.action-row { display: flex; gap: 0.5rem; }
.contact-list { display: flex; flex-direction: column; gap: 1rem; }
.contact-list div { display: flex; align-items: center; gap: 0.75rem; }
.contact-list i { color: var(--p-primary-color); }
.auth-error { max-width: 480px; margin: 2rem auto; }
.request-link-card { margin-top: 1rem; }
.request-link-row { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.sent-note { margin-top: 0.75rem; color: var(--p-primary-color); }
@media (max-width: 640px) { .card-grid { grid-template-columns: 1fr; } .company-name { font-size: 1rem; } }
</style>
