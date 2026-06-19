<template>
    <section class="estimates-view view-card">
      <div class="page-header">
        <h2 class="page-title">Estimate Documents</h2>
        <p class="page-subtitle">Quotes you've drafted, sent, accepted, or declined. For jobs that are in the <em>estimate</em> workflow phase, use the <router-link to="/jobs?stage=Estimate">Jobs board</router-link>.</p>
      </div>

      <!-- Toolbar -->
      <div class="estimates-toolbar">
        <span class="search-wrap">
          <InputText
            v-model="searchQuery"
            placeholder="Search estimates..."
            data-testid="estimates-search"
          />
        </span>
        <Button
          label="Create Estimate"
          icon="pi pi-plus"
          data-testid="create-estimate-btn"
          @click="router.push('/estimates/new')"
        />
      </div>

      <!-- Status Filters -->
      <div class="status-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab.value"
          :label="`${tab.label} (${tabCount(tab.value)})`"
          :severity="activeStatus === tab.value ? undefined : 'secondary'"
          :data-testid="`estimates-status-${tab.value.toLowerCase()}`"
          size="small"
          @click="activeStatus = tab.value"
        />
      </div>

      <!-- Estimates Table -->
      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        :value="paginatedEstimates"
        :loading="loading"
        data-testid="estimates-datatable"
        stripedRows
        :rowHover="true"
        :sortField="sortField"
        :sortOrder="sortOrder"
        @sort="onSort"
        @row-click="onRowClick"
      >
        <template #empty>{{ searchQuery || activeStatus !== 'All' ? 'No matching estimates. Try clearing your filters.' : 'No estimates yet. Click "Create Estimate" to start.' }}</template>
        <Column field="estimate_number" header="Estimate #" sortable>
          <template #body="{ data }">
            <span class="link-text">{{ data.estimate_number }}</span>
          </template>
        </Column>
        <Column field="label" header="Job Name" sortable>
          <template #body="{ data }">
            <span v-if="data.label">{{ data.label }}</span>
            <span v-else class="muted">—</span>
          </template>
        </Column>
        <Column field="customer_name" header="Customer" sortable>
          <template #body="{ data }">
            <span v-if="data.customer_name">{{ data.customer_name }}</span>
            <span v-else class="muted" title="Estimate has no linked customer">— (no customer)</span>
          </template>
        </Column>
        <Column field="total_amount" header="Total" sortable>
          <template #body="{ data }">{{ currency(data.total_amount) }}</template>
        </Column>
        <Column field="status" header="Status" sortable>
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" data-testid="estimate-status-tag" />
          </template>
        </Column>
        <Column field="created_at" header="Created" sortable>
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width: 160px">
          <template #body="{ data }">
            <Button
              v-if="data.status === 'Draft'"
              icon="pi pi-send"
              text
              size="small"
              title="Send to customer"
              :data-testid="`send-estimate-${data.id}`"
              @click.stop="sendEstimate(data)"
            />
            <Button
              v-if="data.status === 'Accepted'"
              icon="pi pi-briefcase"
              text
              size="small"
              title="Convert to Job"
              severity="success"
              :data-testid="`convert-estimate-${data.id}`"
              @click.stop="convertToJob(data)"
            />
            <Button
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              :data-testid="`delete-estimate-${data.id}`"
              @click.stop="confirmDelete(data)"
            />
          </template>
        </Column>
      </DataTable>

      <!-- Pagination -->
      <div class="pagination-bar" v-if="totalPages > 1">
        <Button icon="pi pi-angle-left" severity="secondary" text :disabled="currentPage <= 1" @click="currentPage--" />
        <span class="page-info">Page {{ currentPage }} of {{ totalPages }}</span>
        <Button icon="pi pi-angle-right" severity="secondary" text :disabled="currentPage >= totalPages" @click="currentPage++" />
      </div>

      <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 mounts one globally. -->
      <Toast data-testid="estimates-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useConfirm } from "primevue/useconfirm";
import { useToast } from "primevue/usetoast";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import InputText from "primevue/inputtext";
import Tag from "primevue/tag";
import Toast from "primevue/toast";

const router = useRouter();
const api = useApi();
const confirm = useConfirm();
const toast = useToast();

// --- State ---
const loading = ref(false);
const estimates = ref([]);
const customers = ref([]);
const searchQuery = ref("");
const activeStatus = ref("All");
const currentPage = ref(1);
const perPage = 20;

// Sort state, owned externally so sort applies to the FULL filtered set
// before pagination slices it. Same shape as BillingView's fix
// (2026-05-11): without this, PrimeVue's `sortable` only sorts the visible
// page of `paginatedEstimates`, leaving Accepted/Declined scattered.
const sortField = ref(null);
const sortOrder = ref(null);
function onSort(event) {
  sortField.value = event.sortField || null;
  sortOrder.value = event.sortOrder || null;
  currentPage.value = 1;
}

const statusTabs = [
  { label: "All", value: "All" },
  { label: "Draft", value: "Draft" },
  { label: "Sent", value: "Sent" },
  { label: "Accepted", value: "Accepted" },
  { label: "Declined", value: "Declined" },
];

// --- Computed ---
const filteredEstimates = computed(() => {
  let list = estimates.value;
  if (activeStatus.value !== "All") {
    list = list.filter((e) => e.status === activeStatus.value);
  }
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase();
    list = list.filter((e) => {
      const hay = [e.estimate_number, e.customer_name, e.label, e.notes].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }
  return list;
});

const totalPages = computed(() => Math.max(1, Math.ceil(filteredEstimates.value.length / perPage)));

const sortedEstimates = computed(() => {
  if (!sortField.value) return filteredEstimates.value;
  const field = sortField.value;
  const dir = sortOrder.value || 1;
  return [...filteredEstimates.value].sort((a, b) => {
    const av = a?.[field];
    const bv = b?.[field];
    const an = av == null || av === "";
    const bn = bv == null || bv === "";
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    if (typeof av === "number" && typeof bv === "number") {
      return (av - bv) * dir;
    }
    return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" }) * dir;
  });
});

const paginatedEstimates = computed(() => {
  const start = (currentPage.value - 1) * perPage;
  return sortedEstimates.value.slice(start, start + perPage);
});


// --- Helpers ---
function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function currency(amount) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(toNum(amount));
}

function formatDate(d) {
  if (!d) return "-";
  return new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function statusSeverity(status) {
  const map = { Draft: "secondary", Sent: "info", Accepted: "success", Declined: "danger", Converted: "contrast" };
  return map[status] || "secondary";
}

function tabCount(status) {
  if (status === "All") return estimates.value.length;
  return estimates.value.filter((e) => e.status === status).length;
}

function normalizeEstimate(raw, customerMap = {}) {
  return {
    id: raw.id,
    estimate_number: raw.estimate_number || raw.estimateNumber || `EST-${String(raw.id).substring(0, 8).toUpperCase()}`,
    customer_name: raw.customer_name || raw.customer || (raw.customer && typeof raw.customer === "object" ? raw.customer.name : "") || customerMap[String(raw.customer_id)] || "",
    customer_id: raw.customer_id,
    total_amount: toNum(raw.total_amount || raw.total || 0),
    status: capitalize(raw.status) || "Draft",
    created_at: raw.created_at || raw.created || "",
    label: raw.label || "",
    notes: raw.notes || "",
    job_id: raw.job_id || null,
  };
}

// --- Actions ---
async function loadData() {
  loading.value = true;
  try {
    const [estRes, custRes] = await Promise.allSettled([
      api.get("/api/estimates"),
      api.get("/api/customers?per_page=500"),
    ]);

    const customerMap = {};
    if (custRes.status === "fulfilled") {
      const raw = custRes.value;
      const list = Array.isArray(raw) ? raw : raw?.items || raw?.data || [];
      customers.value = list;
      for (const c of list) customerMap[String(c.id)] = c.name;
    }

    if (estRes.status === "fulfilled") {
      const raw = estRes.value;
      const list = Array.isArray(raw) ? raw : raw?.items || raw?.data || [];
      estimates.value = list.map((e) => normalizeEstimate(e, customerMap));
    }
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load estimates", life: 3000 });
  } finally {
    loading.value = false;
  }
}

async function sendEstimate(est) {
  try {
    await api.patch(`/api/estimates/${est.id}`, { status: "Sent" });
    est.status = "Sent";
    toast.add({ severity: "success", summary: "Sent", detail: "Estimate marked as sent", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to send", life: 3000 });
  }
}

async function convertToJob(est) {
  try {
    const result = await api.post(`/api/estimates/${est.id}/convert-to-job`, {});
    est.status = "Converted";
    const jobId = result?.job_id || result?.data?.job_id;
    toast.add({ severity: "success", summary: "Converted", detail: "Estimate converted to job", life: 3000 });
    if (jobId) {
      router.push(`/jobs/${jobId}`);
    }
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to convert", life: 3000 });
  }
}

function confirmDelete(est) {
  confirm.require({
    message: `Delete estimate ${est.estimate_number}? This cannot be undone.`,
    header: "Confirm Delete",
    icon: "pi pi-exclamation-triangle",
    acceptClass: "p-button-danger",
    accept: () => deleteEstimate(est),
  });
}

async function deleteEstimate(est) {
  try {
    await api.del(`/api/estimates/${est.id}`);
    estimates.value = estimates.value.filter((e) => e.id !== est.id);
    toast.add({ severity: "success", summary: "Deleted", detail: "Estimate deleted", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to delete", life: 3000 });
  }
}

function onRowClick(event) {
  const id = event?.data?.id;
  if (id) router.push(`/estimates/${id}`);
}

onMounted(loadData);
</script>

<style scoped>
.page-header { margin-bottom: 1rem; }
.page-header .page-title { margin: 0; }
.page-subtitle {
  margin: 0.25rem 0 0;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
  max-width: 56rem;
}
.page-subtitle a { color: var(--p-primary-color); text-decoration: none; }
.page-subtitle a:hover { text-decoration: underline; }

.estimates-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.search-wrap {
  flex: 1;
  max-width: 360px;
}
.search-wrap .p-inputtext {
  width: 100%;
}

.status-tabs {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.clickable-table :deep(tr) {
  cursor: pointer;
}

.link-text {
  color: var(--p-primary-color, #3b82f6);
  font-weight: 600;
}

.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  margin-top: 1rem;
}
.page-info {
  font-size: 0.875rem;
  color: var(--p-text-muted-color, #6b7280);
}

@media (max-width: 900px) {
  .estimates-toolbar {
    flex-direction: column;
    align-items: stretch;
  }
  .search-wrap {
    max-width: 100%;
  }
}
</style>
