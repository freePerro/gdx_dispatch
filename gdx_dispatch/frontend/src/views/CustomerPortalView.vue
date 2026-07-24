<template>
  <div class="portal-wrapper" data-testid="customer-portal-root">
    <header class="portal-header">
      <div class="logo-container">
        <i class="pi pi-building" style="font-size: 1.5rem" />
        <span class="company-name">{{ company.name }}</span>
      </div>
      <div v-if="jwt && !error" class="header-actions">
        <Button icon="pi pi-key" label="Password" text size="small" @click="openSetPassword" data-testid="set-password-btn" />
        <Button icon="pi pi-sign-out" label="Sign out" text size="small" @click="signOut" data-testid="sign-out-btn" />
      </div>
    </header>

    <main class="portal-content">
      <div v-if="loading" class="loading-wrap"><ProgressSpinner /></div>

      <div v-else-if="!jwt" class="login-wrap" data-testid="portal-login">
        <Card class="login-card" data-testid="portal-login-card">
          <template #title>Sign in to your portal</template>
          <template #content>
            <Message v-if="error" severity="warn" class="mb-3" data-testid="login-notice">{{ error }}</Message>
            <div class="login-form">
              <label class="field">
                <span>Email</span>
                <InputText v-model="email" type="email" placeholder="you@example.com" data-testid="login-email" @keyup.enter="passwordLogin" />
              </label>
              <label class="field">
                <span>Password</span>
                <Password v-model="password" :feedback="false" toggleMask inputClass="w-full" placeholder="Your password" data-testid="login-password" @keyup.enter="passwordLogin" />
              </label>
              <label class="remember-row">
                <Checkbox v-model="remember" :binary="true" data-testid="login-remember" />
                <span>Keep me signed in on this device</span>
              </label>
              <Message v-if="loginError" severity="error" class="mb-2" data-testid="login-error">{{ loginError }}</Message>
              <Button label="Sign in" icon="pi pi-sign-in" :loading="signingIn" @click="passwordLogin" data-testid="login-submit" />
            </div>
            <Divider />
            <div class="magic-fallback">
              <p class="meta">Forgot your password, or don't have one yet?</p>
              <Button label="Email me a sign-in link" icon="pi pi-envelope" text :loading="requestSending" @click="requestNewLink" data-testid="request-link-btn" />
              <p v-if="requestSent" class="meta sent-note" data-testid="request-link-sent">If that email is on file, a sign-in link is on its way.</p>
            </div>
          </template>
        </Card>
      </div>

      <div v-else>
        <Message v-if="error" severity="warn" class="mb-3" data-testid="portal-error">{{ error }}</Message>
        <Message v-if="showSetPwPrompt" severity="info" :closable="true" class="mb-3" data-testid="set-pw-prompt" @close="showSetPwPrompt = false">
          Want faster sign-in next time? <a href="#" @click.prevent="openSetPassword">Set a password</a>.
        </Message>
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
                  <div v-else-if="est.deposit?.pay_url" class="action-row">
                    <Button :label="`Pay ${currency(est.deposit.balance_due)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" @click.stop="openPayUrl(est.deposit.pay_url)" data-testid="pay-deposit-btn" />
                  </div>
                </template>
              </Card>
            </div>
          </TabPanel>

          <TabPanel value="invoices">
            <DataTable :value="invoices" responsiveLayout="stack" breakpoint="640px" data-testid="invoices-table">
              <template #empty>No invoices found.</template>
              <Column field="invoice_number" header="Invoice #">
                <template #body="{ data }">
                  {{ data.invoice_number }}
                  <Tag v-if="data.billing_type === 'deposit'" value="Deposit" severity="info" data-testid="portal-deposit-tag" />
                </template>
              </Column>
              <Column field="total" header="Amount"><template #body="{ data }">{{ currency(data.total) }}</template></Column>
              <Column field="balance_due" header="Balance Due"><template #body="{ data }">{{ currency(data.balance_due) }}</template></Column>
              <Column field="payment_status" header="Status"><template #body="{ data }"><Tag :value="data.payment_status" :severity="statusSeverity(data.payment_status)" /></template></Column>
              <Column field="due_date" header="Due"><template #body="{ data }">{{ formatDate(data.due_date) }}</template></Column>
              <Column header="" :style="{ width: '110px' }"><template #body="{ data }">
                <Button v-if="data.pay_url" label="Pay" icon="pi pi-credit-card" size="small" severity="success" data-testid="invoice-pay-btn" @click="openPayUrl(data.pay_url)" />
              </template></Column>
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

            <div v-if="detailImages.length" class="detail-images" data-testid="estimate-images">
              <Image v-for="img in detailImages" :key="img.id" :src="img.src" :alt="img.name" preview
                     image-style="height: 120px; border-radius: 6px; object-fit: cover" />
            </div>

            <DataTable :value="detail.lines" class="detail-lines" data-testid="estimate-lines-table">
              <template #empty>No line items.</template>
              <Column field="description" header="Item" />
              <Column field="quantity" header="Qty" :style="{ width: '70px' }" />
              <Column v-if="!detail.hide_line_prices" field="unit_price" header="Price" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.unit_price) }}</template></Column>
              <Column v-if="!detail.hide_line_prices" field="line_total" header="Total" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.line_total) }}</template></Column>
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
            <div v-else-if="detail.deposit?.pay_url" class="action-row detail-actions">
              <Button :label="`Pay ${currency(detail.deposit.balance_due)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" @click="openPayUrl(detail.deposit.pay_url)" data-testid="detail-pay-deposit-btn" />
            </div>
            <p v-else-if="detail.status === 'declined' && detail.declined_reason" class="meta">Declined: {{ detail.declined_reason }}</p>
          </div>
        </Dialog>

        <Dialog
          v-model:visible="depositPromptOpen"
          header="Deposit Due"
          :modal="true"
          :style="{ width: 'min(440px, 94vw)' }"
          data-testid="deposit-pay-dialog"
        >
          <p class="meta">
            Thanks for accepting! A deposit of <b>{{ currency(depositPrompt?.amount) }}</b> is due now
            to get your job on the schedule (invoice {{ depositPrompt?.invoice_number }}).
          </p>
          <p class="meta">You can pay securely online by card — or pay later from the Invoices tab.</p>
          <template #footer>
            <Button label="Pay later" text @click="depositPrompt = null" data-testid="deposit-pay-later" />
            <Button label="Pay deposit now" icon="pi pi-credit-card" severity="success"
              data-testid="deposit-pay-now" @click="payDepositNow" />
          </template>
        </Dialog>

        <Dialog
          v-model:visible="setPwVisible"
          header="Set a password"
          :modal="true"
          :style="{ width: 'min(420px, 94vw)' }"
          data-testid="set-password-dialog"
        >
          <p class="meta">Set a password so you can sign in without waiting for an email link next time.</p>
          <label class="field">
            <span>New password</span>
            <Password v-model="newPassword" toggleMask inputClass="w-full" :feedback="true" data-testid="new-password" @keyup.enter="setPassword" />
          </label>
          <p class="meta">At least 8 characters.</p>
          <template #footer>
            <Button label="Cancel" text @click="setPwVisible = false" />
            <Button label="Save password" icon="pi pi-check" :loading="settingPw" :disabled="newPassword.length < 8" @click="setPassword" data-testid="save-password-btn" />
          </template>
        </Dialog>
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, reactive, ref, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Image from "primevue/image";
import InputText from "primevue/inputtext";
import Checkbox from "primevue/checkbox";
import Divider from "primevue/divider";
import Message from "primevue/message";
import Password from "primevue/password";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import { formatDate, formatMoney } from "../composables/useFormatters";

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
const detailImages = ref([]);
const email = ref("");
const password = ref("");
const remember = ref(false);
const signingIn = ref(false);
const loginError = ref("");
const requestSending = ref(false);
const requestSent = ref(false);
const setPwVisible = ref(false);
const newPassword = ref("");
const settingPw = ref(false);
const showSetPwPrompt = ref(false);

function currency(v) { return formatMoney(Number(v) || 0); }
function statusSeverity(s) {
  const map = { sent: "info", accepted: "success", paid: "success", declined: "danger", unpaid: "warn", overdue: "danger", expired: "secondary", scheduled: "info", in_progress: "info", completed: "success" };
  return map[(s || "").toLowerCase()] || "secondary";
}
function jobStatusLabel(job) {
  return (job.lifecycle_stage || "").replace(/_/g, " ") || "-";
}

// "Remember me" persists the session in localStorage (survives a browser
// restart); otherwise it lives in sessionStorage and ends with the tab.
function storeJwt(token, rememberMe) {
  if (!token) { clearStoredJwt(); return; }
  jwt.value = token;
  if (rememberMe) {
    localStorage.setItem(JWT_STORAGE_KEY, token);
    sessionStorage.removeItem(JWT_STORAGE_KEY);
  } else {
    sessionStorage.setItem(JWT_STORAGE_KEY, token);
    localStorage.removeItem(JWT_STORAGE_KEY);
  }
}
function readStoredJwt() {
  return localStorage.getItem(JWT_STORAGE_KEY) || sessionStorage.getItem(JWT_STORAGE_KEY) || "";
}
function clearStoredJwt() {
  jwt.value = "";
  localStorage.removeItem(JWT_STORAGE_KEY);
  sessionStorage.removeItem(JWT_STORAGE_KEY);
}

async function authedFetch(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${jwt.value}` },
  });
  if (res.status === 401) {
    clearStoredJwt();
    throw Object.assign(new Error("unauthorized"), { auth: true });
  }
  if (!res.ok) {
    // Surface the server's detail string — "already accepted" is a far
    // better message than "request failed: 409".
    let detail = `request failed: ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string" && body.detail) detail = body.detail;
    } catch { /* non-JSON error body */ }
    throw Object.assign(new Error(detail), { status: res.status });
  }
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
        storeJwt(body.access_token || "", false); // magic-link → per-session, not "remember"
        showSetPwPrompt.value = true; // nudge them to set a password for next time
      } else {
        error.value = "This sign-in link is invalid or has expired.";
      }
    } catch {
      error.value = "This sign-in link is invalid or has expired.";
    }
    router.replace({ query: {} });
  }
  if (!jwt.value) jwt.value = readStoredJwt();
  if (!jwt.value) { loading.value = false; return; } // not signed in → login card
  await loadPortal();
}

async function loadPortal() {
  try {
    await fetchAll();
    error.value = null;
  } catch (e) {
    if (e?.auth) { clearStoredJwt(); error.value = "Your portal session has expired."; }
    else error.value = "Failed to load portal data. Please try again later.";
  } finally {
    loading.value = false;
  }
}

async function requestNewLink() {
  const em = email.value.trim();
  if (!em) { loginError.value = "Enter your email first."; return; }
  requestSending.value = true;
  try {
    await fetch("/portal/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: em }),
    });
    requestSent.value = true;
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Could not send link. Try again.", life: 4000 });
  } finally {
    requestSending.value = false;
  }
}

async function passwordLogin() {
  const em = email.value.trim();
  if (!em || !password.value) { loginError.value = "Enter your email and password."; return; }
  signingIn.value = true;
  loginError.value = "";
  try {
    const res = await fetch("/portal/login/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: em, password: password.value, remember: remember.value }),
    });
    if (!res.ok) { loginError.value = "Invalid email or password."; return; }
    const body = await res.json();
    storeJwt(body.access_token || "", remember.value);
    password.value = "";
    error.value = null;
    loading.value = true;
    await loadPortal();
  } catch {
    loginError.value = "Could not sign in. Please try again.";
  } finally {
    signingIn.value = false;
  }
}

function openSetPassword() {
  newPassword.value = "";
  setPwVisible.value = true;
}

async function setPassword() {
  if (newPassword.value.length < 8) return;
  settingPw.value = true;
  try {
    await authedFetch("/portal/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword.value }),
    });
    setPwVisible.value = false;
    showSetPwPrompt.value = false;
    newPassword.value = "";
    toast.add({ severity: "success", summary: "Password saved", detail: "You can now sign in with your email and password.", life: 4000 });
  } catch (e) {
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not save password.", life: 4000 });
  } finally {
    settingPw.value = false;
  }
}

function signOut() {
  clearStoredJwt();
  error.value = null;
  showSetPwPrompt.value = false;
  estimates.value = [];
  invoices.value = [];
  jobs.value = [];
}

// One-motion deposit (2026-07-23): the accept response can carry a deposit
// invoice + its public Stripe pay URL. Accepting flows straight into the
// payment prompt; "Pay later" is always available — acceptance is never
// blocked by payment, and the Pay button re-surfaces on the estimate card,
// the detail dialog, and the Invoices tab until the deposit is settled.
const depositPrompt = ref(null);
const depositPromptOpen = computed({
  get: () => !!depositPrompt.value,
  set: (v) => { if (!v) depositPrompt.value = null; },
});

function openPayUrl(url) {
  if (url) window.open(url, "_blank", "noopener");
}

function payDepositNow() {
  openPayUrl(depositPrompt.value?.pay_url);
  depositPrompt.value = null;
}

async function estimateAction(id, action, successMsg) {
  actionBusy[id] = true;
  try {
    const resp = await authedFetch(`/portal/estimates/${id}/${action}`, { method: "POST" });
    toast.add({ severity: action === "accept" ? "success" : "warn", summary: successMsg, life: 3000 });
    if (action === "accept" && resp?.deposit?.pay_url) depositPrompt.value = resp.deposit;
    estimates.value = await authedFetch("/portal/estimates");
    // The deposit invoice lands on the Invoices tab immediately.
    try { invoices.value = await authedFetch("/portal/invoices"); } catch { /* tab refresh is best-effort */ }
  } catch (e) {
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    const msg = e?.message && !e.message.startsWith("request failed")
      ? e.message
      : `Could not ${action} estimate`;
    toast.add({ severity: "error", summary: "Error", detail: msg, life: 4000 });
  } finally {
    actionBusy[id] = false;
  }
}

const acceptEstimate = (id) => estimateAction(id, "accept", "Estimate accepted");
const declineEstimate = (id) => estimateAction(id, "decline", "Estimate declined");

function clearDetailImages() {
  detailImages.value.forEach((img) => URL.revokeObjectURL(img.src));
  detailImages.value = [];
}

async function loadDetailImages(images) {
  // <img src> can't carry the Bearer header — pull each image as an
  // authenticated blob and hand the dialog object URLs instead.
  const loaded = await Promise.all(
    (images || []).map(async (img) => {
      try {
        const res = await fetch(img.url, { headers: { Authorization: `Bearer ${jwt.value}` } });
        if (!res.ok) return null;
        return { id: img.id, name: img.original_name, src: URL.createObjectURL(await res.blob()) };
      } catch { return null; }
    })
  );
  detailImages.value = loaded.filter(Boolean);
}

// Free the blob object URLs whenever the dialog closes, not just on reopen.
watch(detailVisible, (open) => { if (!open) clearDetailImages(); });

async function openEstimate(id) {
  detailVisible.value = true;
  detailLoading.value = true;
  clearDetailImages();
  try {
    detail.value = await authedFetch(`/portal/estimates/${id}`);
    // Not awaited: the dialog renders immediately and photos pop in as
    // their blobs arrive, instead of holding the spinner on big images.
    loadDetailImages(detail.value.images);
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
.portal-header { position: relative; background: var(--p-content-background, #fff); padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; justify-content: center; border-bottom: 1px solid var(--p-content-border-color, transparent); }
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
.detail-images { display: flex; gap: 0.5rem; flex-wrap: wrap; }
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
.header-actions { position: absolute; right: 1.25rem; top: 50%; transform: translateY(-50%); display: flex; gap: 0.25rem; align-items: center; }
.login-wrap { max-width: 460px; margin: 2rem auto; }
.login-form { display: flex; flex-direction: column; gap: 0.9rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; }
.field > span { font-size: 0.85rem; font-weight: 600; color: var(--p-text-color, #374151); }
.field :deep(.p-inputtext) { width: 100%; }
.field :deep(.p-password), .field :deep(.p-password input) { width: 100%; }
.remember-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; cursor: pointer; }
.magic-fallback { display: flex; flex-direction: column; align-items: flex-start; gap: 0.4rem; }
@media (max-width: 640px) { .card-grid { grid-template-columns: 1fr; } .company-name { font-size: 1rem; } }
</style>
