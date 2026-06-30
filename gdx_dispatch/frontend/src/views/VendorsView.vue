<template>
    <section class="vendors-view view-card">
      <Toolbar>
        <template #start>
          <InputText v-model="searchQuery" placeholder="Search vendors..." data-testid="vendor-search" />
        </template>
        <template #end>
          <Button label="+ New Vendor" icon="pi pi-plus" data-testid="new-vendor-btn" @click="openCreate" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error">{{ loadError }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" striped-rows
        data-testid="vendors-table" @row-click="openEdit($event.data)" >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-building" style="font-size:3rem; color:#64748b;"></i>
            <h3>No Vendors Yet</h3>
            <p>Add your suppliers to track purchase orders and catalogs.</p>
            <Button label="+ Add First Vendor" @click="openCreate" />
          </div>
        </template>
        <Column field="name" header="Name" sortable />
        <Column field="contact_name" header="Contact" />
        <Column field="phone" header="Phone" />
        <Column field="email" header="Email" />
        <Column field="payment_terms" header="Terms" />
        <Column field="active" header="Status" style="width:100px">
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
      <Dialog v-model:visible="showDialog" :header="editing ? 'Edit Vendor' : 'New Vendor'" modal :style="{width: '600px'}">
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Name *</label>
            <InputText v-model="form.name" data-testid="vendor-name" class="w-full" />
          </div>
          <div class="form-field">
            <label>Account #</label>
            <InputText v-model="form.account_number" class="w-full" />
          </div>
          <div class="form-field">
            <label>Payment Terms</label>
            <Select v-model="form.payment_terms" :options="termOptions" placeholder="Net 30" class="w-full" />
          </div>
          <div class="form-field">
            <label>Contact Name</label>
            <InputText v-model="form.contact_name" class="w-full" />
          </div>
          <div class="form-field">
            <label>Phone</label>
            <InputText v-model="form.phone" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Email</label>
            <InputText v-model="form.email" type="email" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Website</label>
            <InputText v-model="form.website" placeholder="https://" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Address</label>
            <InputText v-model="form.address" class="w-full" />
          </div>
          <div class="form-field">
            <label>City</label>
            <InputText v-model="form.city" class="w-full" />
          </div>
          <div class="form-field">
            <label>State</label>
            <InputText v-model="form.state" class="w-full" />
          </div>
          <div class="form-field">
            <label>Zip</label>
            <InputText v-model="form.zip" class="w-full" />
          </div>
          <div class="form-field">
            <label>Tax ID</label>
            <InputText v-model="form.tax_id" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea v-model="form.notes" rows="2" class="w-full" />
          </div>
          <div class="form-field">
            <div class="checkbox-row">
              <Checkbox v-model="form.active" :binary="true" inputId="vendor-active" />
              <label for="vendor-active">Active</label>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button :label="editing ? 'Save' : 'Create'" icon="pi pi-check" @click="saveVendor" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
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

const termOptions = ["Net 15", "Net 30", "Net 45", "Net 60", "COD", "Prepaid", "Due on Receipt"];

const emptyForm = () => ({
  name: "", account_number: "", contact_name: "", phone: "", email: "",
  website: "", address: "", city: "", state: "", zip: "", notes: "",
  payment_terms: "Net 30", tax_id: "", active: true,
});
const form = ref(emptyForm());

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
  showDialog.value = true;
}

function openEdit(vendor) {
  editing.value = vendor;
  form.value = { ...vendor };
  showDialog.value = true;
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
.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); }
.empty-state h3 { margin: 1rem 0 0.5rem; color: var(--text-color); }
.inline-error { color: #ef4444; padding: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
