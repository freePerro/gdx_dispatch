<template>
    <section class="customers-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title" style="margin:0;margin-right:1rem">
            Customers <span class="customer-count" v-if="totalCustomers != null">({{ totalCustomers }})</span>
          </h2>
          <InputText
            id="customers-search"
            name="customers-search"
            v-model="searchQuery"
            data-testid="customers-search"
            placeholder="Search customers by name, email, or phone..."
            class="search-input"
            @input="onSearchChange"
          />
        </template>
        <template #end>
          <Button
            label="Export"
            icon="pi pi-download"
            aria-label="Export CSV"
            text
            data-testid="customers-export-btn"
            class="mr-2"
            @click="exportCustomers"
          />
          <Button
            label="Duplicates"
            icon="pi pi-clone"
            severity="secondary"
            data-testid="duplicates-btn"
            class="mr-2"
            @click="goToDuplicates"
          />
          <Button
            label="+ New Customer"
            icon="pi pi-plus"
            data-testid="new-customer-btn"
            @click="openCreateDialog"
          />
        </template>
      </Toolbar>

      <div v-if="isLoading" class="spinner-wrap" data-testid="customers-loading">
        <ProgressSpinner />
      </div>

      <DataTable
        v-if="!isLoading"
        :value="filteredCustomers"
        :paginator="true"
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50, 100]"
        paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink RowsPerPageDropdown CurrentPageReport"
        currentPageReportTemplate="{first}-{last} of {totalRecords}"
        data-testid="customers-datatable"
        stripedRows
        responsiveLayout="scroll"
        class="customers-table"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-users"
            :title="searchQuery ? 'No customers match your search' : 'No customers yet'"
            :message="searchQuery ? 'Try a different name, email, or phone.' : 'Click &quot;+ New Customer&quot; to add one.'"
          />
        </template>
        <Column field="name" header="Name" sortable>
          <template #body="{ data }">
            <router-link
              :to="`/customers/${data.id}`"
              class="customer-name-link"
              :data-testid="`customer-link-${data.id}`"
              @click.stop
            >
              {{ data.name }}
            </router-link>
          </template>
        </Column>
        <Column field="phone" header="Phone" sortable style="width: 150px">
          <template #body="{ data }">
            <a v-if="data.phone" :href="`tel:${data.phone}`" class="contact-link" @click.stop>{{ formatPhone(data.phone) }}</a>
            <span v-else class="text-muted">—</span>
          </template>
        </Column>
        <Column field="email" header="Email" sortable>
          <template #body="{ data }">
            <a v-if="data.email" :href="`mailto:${data.email}`" class="contact-link" @click.stop>{{ data.email }}</a>
            <span v-else class="text-muted">—</span>
          </template>
        </Column>
        <Column field="address" header="Address" sortable>
          <template #body="{ data }">
            <span v-if="data.address">{{ data.address }}</span>
            <span v-else class="text-muted">—</span>
            <!--
              Sprint customer-multi-location (2026-05-21) — badge only when
              the customer has more than one site. Hidden on the 90% case.
            -->
            <Tag
              v-if="(data.location_count || 0) > 1"
              :value="`${data.location_count} sites`"
              severity="info"
              class="ml-2"
              :data-testid="`customer-sites-badge-${data.id}`"
            />
          </template>
        </Column>
        <Column field="customer_type" header="Type" sortable style="width: 130px">
          <template #body="{ data }">
            <Tag :value="normalizeCustomerType(data.customer_type)" :severity="normalizeCustomerType(data.customer_type) === 'Commercial' ? 'warning' : 'info'" />
          </template>
        </Column>
        <Column header="Actions" style="width: 100px; text-align: center">
          <template #body="{ data }">
            <Button
              v-tooltip="'Edit'"
              icon="pi pi-pencil" aria-label="Edit"
              text
              rounded
              size="small"
              :data-testid="`customer-edit-${data.id}`"
              @click.stop="openEditDialog(data)"
            />
            <Button
              v-tooltip="'Delete'"
              icon="pi pi-trash" aria-label="Delete"
              text
              rounded
              severity="danger"
              size="small"
              :data-testid="`customer-delete-${data.id}`"
              @click.stop="promptDelete(data)"
            />
          </template>
        </Column>
      </DataTable>

      <!-- Create/Edit Dialog (extracted 2026-05-21 — see
           components/CustomerFormDialog.vue). Reused on InvoiceDetailView. -->
      <CustomerFormDialog
        v-model:visible="showFormDialog"
        :mode="formMode"
        :customer="editingCustomer"
        @saved="onCustomerSaved"
      />

      <!-- Delete Confirmation -->
      <Dialog
        v-model:visible="showDeleteDialog"
        header="Confirm Delete"
        :style="{ width: '400px' }"
        modal
        data-testid="customer-delete-dialog"
      >
        <p>Are you sure you want to delete <strong>{{ deleteTarget?.name }}</strong>?</p>
        <p style="color: var(--p-text-muted-color)">This will also remove all associated data. This action cannot be undone.</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button
            label="Delete"
            severity="danger"
            :loading="isDeleting"
            data-testid="customer-confirm-delete-btn"
            @click="confirmDelete"
          />
        </div>
      </Dialog>

      <Toast data-testid="customers-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
import { formatPhone } from "../composables/useFormatters";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toast from "primevue/toast";
import Toolbar from "primevue/toolbar";
import CustomerFormDialog from "../components/CustomerFormDialog.vue";
import EmptyState from "../components/EmptyState.vue";

const api = useApiWithToast();
const toast = useToast();
const router = useRouter();
const route = useRoute();

function goToDuplicates() {
  router.push("/customers/duplicates");
}

const searchQuery = ref("");
// Persist the search text across reloads (JobsView/BillingView pattern).
// Restored before onMounted runs, so the initial fetchCustomers() already
// applies the saved server-side `q` filter.
useListPrefs(
  "customers",
  { searchQuery },
  {
    searchQuery: { default: "", valid: (v) => typeof v === "string" },
  },
);
const customers = ref([]);
const totalCustomers = ref(null);
const isLoading = ref(false);
const isDeleting = ref(false);
const showFormDialog = ref(false);
const showDeleteDialog = ref(false);
const formMode = ref("create");
const editingCustomer = ref(null);
const deleteTarget = ref(null);

function normalizeCustomerType(type) {
  const text = (type || "").toString().trim().toLowerCase();
  if (text === "commercial") return "Commercial";
  if (text === "retail") return "Retail";
  if (text === "contractor") return "Contractor";
  if (text === "wholesale") return "Wholesale";
  if (text === "property_manager" || text === "property manager") return "Property Manager";
  if (text === "residential") return "Residential";
  // Unknown values surface verbatim instead of being silently masked as
  // "Residential" — that hid 322 of 326 GDX customer rows from view 2026-04-29.
  return type ? String(type) : "—";
}

const filteredCustomers = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  if (!query) return customers.value;
  return customers.value.filter((c) =>
    (c.name || "").toLowerCase().includes(query) ||
    (c.email || "").toLowerCase().includes(query) ||
    (c.phone || "").toLowerCase().includes(query) ||
    (c.address || "").toLowerCase().includes(query)
  );
});

// CSV export — dumps the CURRENTLY FILTERED rows (search applied),
// matching the visible table columns.
const { exportCsv } = useTableExport();
function exportCustomers() {
  exportCsv(
    filteredCustomers.value,
    [
      { field: "name", header: "Name" },
      { field: "phone", header: "Phone" },
      { field: "email", header: "Email" },
      { field: "address", header: "Address" },
      { field: "customer_type", header: "Type" },
    ],
    "customers",
  );
}

function openCreateDialog() {
  formMode.value = "create";
  editingCustomer.value = null;
  showFormDialog.value = true;
}

function openEditDialog(customer) {
  formMode.value = "edit";
  editingCustomer.value = customer;
  showFormDialog.value = true;
}

function promptDelete(customer) {
  deleteTarget.value = customer;
  showDeleteDialog.value = true;
}

async function onCustomerSaved() {
  await fetchCustomers();
}

async function confirmDelete() {
  if (!deleteTarget.value?.id) return;
  isDeleting.value = true;
  try {
    await api.del(`/api/customers/${deleteTarget.value.id}`);
    toast.add({ severity: "success", summary: "Customer Deleted", detail: `${deleteTarget.value.name} deleted.`, life: 3000 });
    showDeleteDialog.value = false;
    deleteTarget.value = null;
    await fetchCustomers();
  } catch (error) {
    toast.add({ severity: "error", summary: "Error", detail: error?.message || "Failed to delete customer.", life: 5000 });
  } finally {
    isDeleting.value = false;
  }
}

async function fetchCustomers() {
  isLoading.value = true;
  try {
    const q = searchQuery.value.trim();
    const url = q ? `/api/customers?q=${encodeURIComponent(q)}&per_page=1000` : "/api/customers?per_page=1000";
    const result = await api.get(url);
    customers.value = Array.isArray(result) ? result : result?.items || result?.data || [];
    totalCustomers.value = result?.total ?? customers.value.length;
  } catch (error) {
    toast.add({ severity: "error", summary: "Load Error", detail: error?.message || "Failed to load customers.", life: 5000 });
  } finally {
    isLoading.value = false;
  }
}

// Debounced server-side search
let _searchTimer = null;
function onSearchChange() {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => fetchCustomers(), 300);
}

onMounted(() => {
  fetchCustomers();
  // Dashboard "+ New Customer" passes ?new=1 to auto-open the form.
  // Pre-fix the handler called router.replace immediately after
  // openCreateDialog. Empirically the dialog never rendered — likely a
  // race between Vue Router's URL change and the showFormDialog ref
  // update inside openCreateDialog. Drop the strip; ?new=1 in the URL
  // is harmless and clears on next navigation.
  if (route.query.new === "1") {
    openCreateDialog();
  }
});

onUnmounted(() => {
  clearTimeout(_searchTimer);
});

// Exposed for vitest specs (Sprint customer-multi-location 2026-05-21) —
// regression checks need to read the customers ref after fetch to verify
// location_count merged through correctly.
defineExpose({ customers });
</script>

<style scoped>
.customers-view {
  max-width: 1200px;
}

.search-input {
  width: 300px;
}

.customers-table {
  margin-top: 1rem;
}

.customer-name-link {
  color: var(--p-primary-color);
  font-weight: 600;
  text-decoration: none;
}

.customer-name-link:hover {
  text-decoration: underline;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.dialog-form {
  display: grid;
  gap: 0.75rem;
}

.form-field {
  display: grid;
  gap: 0.25rem;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}
.tax-toggle .p-inputswitch {
  margin-top: 0.2rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}

.empty-message {
  text-align: center;
  padding: 2rem;
  color: var(--p-text-muted-color);
}

.w-full {
  width: 100%;
}

.contact-link {
  color: var(--interactive-primary, #3b82f6);
  text-decoration: none;
}

.contact-link:hover {
  text-decoration: underline;
}

.text-muted {
  color: var(--text-muted, #94a3b8);
}
</style>
