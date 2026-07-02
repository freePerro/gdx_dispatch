<template>
  <div class="portal-wrapper" data-testid="customer-portal-root">
    <header class="portal-header">
      <div class="logo-container">
        <i class="pi pi-building" style="font-size: 1.5rem" />
        <span class="company-name">{{ companyName }}</span>
      </div>
    </header>

    <main class="portal-content">
      <div v-if="loading" class="loading-wrap"><ProgressSpinner /></div>

      <Message v-else-if="error" severity="error" class="m-3" data-testid="error-message">{{ error }}</Message>

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
              <Card v-for="est in estimates" :key="est.id" class="portal-card" data-testid="estimate-card">
                <template #title>
                  <div class="card-title-row">
                    <span>{{ est.label || est.estimate_number }}</span>
                    <Tag :value="est.status" :severity="statusSeverity(est.status)" />
                  </div>
                </template>
                <template #content>
                  <p class="amount">{{ currency(est.total) }}</p>
                  <p class="meta">Created: {{ formatDate(est.created_at) }}</p>
                </template>
                <template #footer>
                  <div v-if="est.status === 'Sent' || est.status === 'sent'" class="action-row">
                    <Button label="Accept" icon="pi pi-check" severity="success" class="flex-1" @click="acceptEstimate(est.id)" data-testid="accept-btn" />
                    <Button label="Decline" icon="pi pi-times" aria-label="Remove" severity="danger" class="flex-1" @click="declineEstimate(est.id)" data-testid="decline-btn" />
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
              <Column field="status" header="Status"><template #body="{ data }"><Tag :value="data.status" :severity="statusSeverity(data.status)" /></template></Column>
              <Column field="due_date" header="Due"><template #body="{ data }">{{ formatDate(data.due_date) }}</template></Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="jobs">
            <DataTable :value="jobs" responsiveLayout="stack" breakpoint="640px" data-testid="jobs-table">
              <template #empty>No jobs found.</template>
              <Column field="title" header="Job" />
              <Column field="status" header="Status"><template #body="{ data }"><Tag :value="data.status" :severity="statusSeverity(data.status)" /></template></Column>
              <Column field="scheduled_at" header="Scheduled"><template #body="{ data }">{{ formatDate(data.scheduled_at) }}</template></Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="contact">
            <Card data-testid="contact-card">
              <template #title>Contact Us</template>
              <template #content>
                <div class="contact-list">
                  <div><i class="pi pi-phone" /> (218) 555-0100</div>
                  <div><i class="pi pi-envelope" /> owner@example.com</div>
                  <div><i class="pi pi-map-marker" /> 123 Main St, Anytown USA</div>
                  <div><i class="pi pi-clock" /> Mon-Fri 8AM-5PM</div>
                </div>
              </template>
            </Card>
          </TabPanel>
          </TabPanels>
        </Tabs>
      </div>
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import { useToast } from "primevue/usetoast";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Message from "primevue/message";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import { formatDate, formatMoney } from "../composables/useFormatters";

const route = useRoute();
const toast = useToast();
const token = ref(route.query.token || "");
const loading = ref(true);
const error = ref(null);
const companyName = ref("Example Garage Doors");
const estimates = ref([]);
const invoices = ref([]);
const jobs = ref([]);

function currency(v) { return formatMoney(Number(v) || 0); }
function statusSeverity(s) {
  const map = { sent: "info", accepted: "success", paid: "success", declined: "danger", overdue: "danger", draft: "secondary", scheduled: "info", complete: "success" };
  return map[(s || "").toLowerCase()] || "secondary";
}

async function fetchPortal() {
  if (!token.value) { error.value = "Invalid access link. Contact your service provider."; loading.value = false; return; }
  try {
    const [estRes, invRes, jobRes] = await Promise.allSettled([
      fetch(`/api/portal/estimates?token=${token.value}`).then(r => r.json()),
      fetch(`/api/portal/invoices?token=${token.value}`).then(r => r.json()),
      fetch(`/api/portal/jobs?token=${token.value}`).then(r => r.json()),
    ]);
    estimates.value = estRes.status === "fulfilled" ? (Array.isArray(estRes.value) ? estRes.value : []) : [];
    invoices.value = invRes.status === "fulfilled" ? (Array.isArray(invRes.value) ? invRes.value : []) : [];
    jobs.value = jobRes.status === "fulfilled" ? (Array.isArray(jobRes.value) ? jobRes.value : []) : [];
  } catch { error.value = "Failed to load portal data."; }
  finally { loading.value = false; }
}

async function acceptEstimate(id) {
  try { await fetch(`/api/portal/estimates/${id}/accept?token=${token.value}`, { method: "POST" }); toast.add({ severity: "success", summary: "Accepted", life: 3000 }); await fetchPortal(); }
  catch { toast.add({ severity: "error", summary: "Error", detail: "Could not accept", life: 3000 }); }
}

async function declineEstimate(id) {
  try { await fetch(`/api/portal/estimates/${id}/decline?token=${token.value}`, { method: "POST" }); toast.add({ severity: "warn", summary: "Declined", life: 3000 }); await fetchPortal(); }
  catch { toast.add({ severity: "error", summary: "Error", detail: "Could not decline", life: 3000 }); }
}

onMounted(fetchPortal);
</script>

<style scoped>
.portal-wrapper { min-height: 100vh; background: #f3f4f6; }
.portal-header { background: white; padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; justify-content: center; }
.logo-container { display: flex; align-items: center; gap: 0.75rem; }
.company-name { font-size: 1.25rem; font-weight: 700; color: #1e293b; }
.portal-content { max-width: 900px; margin: 0 auto; padding: 1rem; }
.loading-wrap { display: flex; justify-content: center; padding: 3rem; }
.empty-msg { text-align: center; padding: 2rem; color: #6b7280; }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
.portal-card { transition: transform 0.15s; }
.portal-card:hover { transform: translateY(-2px); }
.card-title-row { display: flex; justify-content: space-between; align-items: center; }
.amount { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); margin: 0.5rem 0; }
.meta { font-size: 0.85rem; color: #6b7280; }
.action-row { display: flex; gap: 0.5rem; }
.contact-list { display: flex; flex-direction: column; gap: 1rem; }
.contact-list div { display: flex; align-items: center; gap: 0.75rem; }
.contact-list i { color: var(--p-primary-color); }
@media (max-width: 640px) { .card-grid { grid-template-columns: 1fr; } .company-name { font-size: 1rem; } }
</style>
