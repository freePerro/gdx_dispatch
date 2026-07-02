<template>
    <section class="vendors-view view-card">
      <Toolbar>
        <template #start>
          <InputText v-model="searchQuery" placeholder="Search vendors..." data-testid="vendor-search" />
        </template>
        <template #end>
          <Button icon="pi pi-download" label="Export" aria-label="Export CSV" text size="small" @click="exportRows" />
          <Button label="+ New Vendor" icon="pi pi-plus" data-testid="new-vendor-btn" @click="openCreate" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error">{{ loadError }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" :rowsPerPageOptions="[10, 20, 50, 100]" striped-rows
        data-testid="vendors-table" @row-click="openEdit($event.data)" >
        <template #empty>
          <EmptyState icon="pi pi-building" title="No Vendors Yet" message="Add your suppliers to track purchase orders and catalogs." actionLabel="+ Add First Vendor" @action="openCreate" />
        </template>
        <Column field="name" header="Name" sortable />
        <Column field="contact_name" header="Contact" sortable />
        <Column field="phone" header="Phone" sortable />
        <Column field="email" header="Email" sortable />
        <Column field="payment_terms" header="Terms" sortable />
        <Column field="active" header="Status" sortable style="width:100px">
          <template #body="{ data }">
            <Tag :value="data.active ? 'Active' : 'Inactive'" :severity="data.active ? 'success' : 'secondary'" />
          </template>
        </Column>
        <Column header="Actions" style="width:100px">
          <template #body="{ data }">
            <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openEdit(data)" />
            <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- Dialog -->
      <Dialog v-model:visible="showDialog" :header="editing ? 'Edit Vendor' : 'New Vendor'" modal :style="{width: '600px'}" :closable="!isDirty" :close-on-escape="!isDirty">
        <div class="form-grid">
          <FormField v-model="form.name" label="Name" required class="full-width" data-testid="vendor-name" />
          <FormField v-model="form.account_number" label="Account #" />
          <FormField v-model="form.payment_terms" label="Payment Terms" as="select" :options="termOptions" placeholder="Net 30" />
          <FormField v-model="form.contact_name" label="Contact Name" />
          <FormField v-model="form.phone" label="Phone" />
          <FormField v-model="form.email" label="Email" type="email" class="full-width" />
          <FormField v-model="form.website" label="Website" placeholder="https://" class="full-width" />
          <FormField v-model="form.address" label="Address" class="full-width" />
          <FormField v-model="form.city" label="City" />
          <FormField v-model="form.state" label="State" />
          <FormField v-model="form.zip" label="Zip" />
          <FormField v-model="form.tax_id" label="Tax ID" />
          <FormField v-model="form.notes" label="Notes" as="textarea" :rows="2" class="full-width" />
          <div class="form-field">
            <div class="checkbox-row">
              <Checkbox v-model="form.active" :binary="true" inputId="vendor-active" />
              <label for="vendor-active">Active</label>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="cancelDialog" />
          <Button :label="editing ? 'Save' : 'Create'" icon="pi pi-check" @click="saveVendor" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import EmptyState from "../components/EmptyState.vue";
import FormField from "../components/FormField.vue";
import { useDirtyDialog } from "../composables/useDirtyDialog";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const vendors = ref([]);
const loading = ref(true);
const loadError = ref("");
const searchQuery = ref("");
const showDialog = ref(false);
const editing = ref(null);
const saving = ref(false);

useListPrefs(
  "vendors",
  { searchQuery },
  { searchQuery: { default: "", valid: (v) => typeof v === "string" } },
);

// {label,value} shape for FormField's Select wrapper.
const termOptions = ["Net 15", "Net 30", "Net 45", "Net 60", "COD", "Prepaid", "Due on Receipt"]
  .map((t) => ({ label: t, value: t }));

const emptyForm = () => ({
  name: "", account_number: "", contact_name: "", phone: "", email: "",
  website: "", address: "", city: "", state: "", zip: "", notes: "",
  payment_terms: "Net 30", tax_id: "", active: true,
});
const form = ref(emptyForm());

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value);
const { exportCsv } = useTableExport();

function exportRows() {
  exportCsv(filtered.value, [
    { field: "name", header: "Name" },
    { field: "contact_name", header: "Contact" },
    { field: "phone", header: "Phone" },
    { field: "email", header: "Email" },
    { field: "payment_terms", header: "Terms" },
    { field: "active", header: "Active" },
  ], "vendors");
}

const filtered = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return vendors.value;
  return vendors.value.filter((v) =>
    (v.name || "").toLowerCase().includes(q) ||
    (v.contact_name || "").toLowerCase().includes(q) ||
    (v.email || "").toLowerCase().includes(q)
  );
});

async function loadVendors() {
  loading.value = true;
  try {
    const data = await api.get("/api/vendors");
    vendors.value = Array.isArray(data) ? data : data?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load vendors";
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editing.value = null;
  form.value = emptyForm();
  snapshot();
  showDialog.value = true;
}

function openEdit(vendor) {
  editing.value = vendor;
  form.value = { ...vendor };
  snapshot();
  showDialog.value = true;
}

function cancelDialog() {
  if (confirmDiscard()) showDialog.value = false;
}

async function saveVendor() {
  if (!form.value.name.trim()) return;
  saving.value = true;
  try {
    if (editing.value) {
      await api.patch(`/api/vendors/${editing.value.id}`, form.value);
    } else {
      await api.post("/api/vendors", form.value);
    }
    showDialog.value = false;
    await loadVendors();
  } catch (err) {
    console.error('save_vendor_failed', err?.message || err);
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(vendor) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${vendor.name}?` }))) return;
  await api.delete(`/api/vendors/${vendor.id}`);
  await loadVendors();
}

onMounted(loadVendors);
</script>

<style scoped>
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.form-field label { font-size: 0.82rem; font-weight: 600; color: var(--p-text-muted-color); }
.full-width { grid-column: 1 / -1; }
.w-full { width: 100%; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; padding-top: 1.5rem; }
.clickable-row { cursor: pointer; }
.inline-error { color: #ef4444; padding: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
