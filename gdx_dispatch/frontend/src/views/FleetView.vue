<template>
    <section class="view-card">
      <Toolbar>
        <template #start>
          <InputText v-model="searchQuery" placeholder="Search vehicles" data-testid="fleet-search" />
        </template>
        <template #end>
          <Button label="+ New Vehicle" data-testid="new-vehicle-btn" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="fleet-load-error">{{ loadError }}</div>
      <div v-if="successMsg" class="inline-success" data-testid="fleet-success">{{ successMsg }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-if="!loading"
        :value="filteredVehicles"
        data-testid="fleet-datatable"
        striped-rows
        @row-click="onRowClick"
      >
        <template #empty>
          <EmptyState icon="pi pi-truck" title="No vehicles yet"
            message="Add your work trucks and vans to track service dates, mileage, and drivers."
            action-label="New Vehicle" @action="openCreateDialog" />
        </template>
        <Column header="Vehicle" sortable>
          <template #body="{ data }">{{ [data.year, data.make, data.model].filter(Boolean).join(' ') || data.vehicle_name || '-' }}</template>
        </Column>
        <Column field="plate_number" header="Plate #">
          <template #body="{ data }">{{ data.plate_number || data.license_plate || '-' }}</template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status || 'Unknown'" :severity="vehicleStatusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="assigned_driver" header="Assigned To">
          <template #body="{ data }">{{ data.assigned_driver || data.assigned_to || data.driver_name || '-' }}</template>
        </Column>
        <Column field="mileage" header="Mileage">
          <template #body="{ data }">{{ data.mileage != null ? Number(data.mileage).toLocaleString() : '-' }}</template>
        </Column>
        <Column field="next_service_date" header="Next Service" sortable>
          <template #body="{ data }">
            <span :class="{ 'overdue': isOverdue(data.next_service_date) }">
              {{ data.next_service_date?.split('T')[0] || '—' }}
            </span>
          </template>
        </Column>
        <Column field="last_inspection" header="Last Inspection">
          <template #body="{ data }">{{ data.last_inspection?.split('T')[0] || '—' }}</template>
        </Column>
      </DataTable>

      <!-- Create / Edit Dialog -->
      <Dialog v-model:visible="showFormDialog" :header="isEdit ? 'Edit Vehicle' : 'Add Vehicle'" data-testid="fleet-form-dialog" :style="{ width: '32rem' }">
        <form class="dialog-form" @submit.prevent="submitForm">
          <div class="form-field">
            <label for="fl-name">Vehicle Name</label>
            <InputText id="fl-name" v-model="form.vehicle_name" data-testid="fl-name-input" />
          </div>
          <div class="form-row-3">
            <div class="form-field">
              <label for="fl-year">Year</label>
              <InputNumber id="fl-year" v-model="form.year" :use-grouping="false" data-testid="fl-year-input" />
            </div>
            <div class="form-field">
              <label for="fl-make">Make *</label>
              <InputText id="fl-make" v-model="form.make" data-testid="fl-make-input" />
            </div>
            <div class="form-field">
              <label for="fl-model">Model *</label>
              <InputText id="fl-model" v-model="form.model" data-testid="fl-model-input" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="fl-plate">Plate Number</label>
              <InputText id="fl-plate" v-model="form.plate_number" data-testid="fl-plate-input" />
            </div>
            <div class="form-field">
              <label for="fl-status">Status</label>
              <Select id="fl-status" v-model="form.status" :options="statusOptions" data-testid="fl-status-dropdown" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="fl-mileage">Mileage</label>
              <InputNumber id="fl-mileage" v-model="form.mileage" :use-grouping="true" data-testid="fl-mileage-input" />
            </div>
            <div class="form-field">
              <label for="fl-driver">Assigned Driver</label>
              <InputText id="fl-driver" v-model="form.assigned_driver" data-testid="fl-driver-input" />
            </div>
          </div>
          <div v-if="formError" class="inline-error" data-testid="fl-form-error">{{ formError }}</div>
          <div class="form-actions">
            <Button v-if="isEdit" type="button" label="Delete" severity="danger" text data-testid="fl-delete-btn" @click="showDeleteDialog = true" />
            <Button type="submit" :label="isEdit ? 'Save' : 'Create'" :loading="saving" data-testid="fl-submit-btn" />
          </div>
        </form>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog v-model:visible="showDeleteDialog" header="Confirm Delete" data-testid="fl-delete-dialog">
        <p>Delete this vehicle?</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button label="Delete" severity="danger" :loading="deleting" data-testid="fl-confirm-delete-btn" @click="confirmDelete" />
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import EmptyState from "../components/EmptyState.vue";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";

const api = useApi();
const vehicles = ref([]);
const loading = ref(false);
const saving = ref(false);
const deleting = ref(false);
const loadError = ref("");
const formError = ref("");
const successMsg = ref("");
const searchQuery = ref("");
const showFormDialog = ref(false);
const showDeleteDialog = ref(false);
const formMode = ref("create");
const statusOptions = ["Active", "In Service", "Out of Service", "Retired"];

const isEdit = computed(() => formMode.value === "edit");

const defaultForm = () => ({
  id: null,
  vehicle_name: "",
  plate_number: "",
  make: "",
  model: "",
  year: new Date().getFullYear(),
  mileage: 0,
  status: "Active",
  assigned_driver: "",
});
const form = ref(defaultForm());

function vehicleStatusSeverity(status) {
  const map = { Active: "success", "In Service": "warning", "Out of Service": "danger", Retired: "secondary" };
  return map[status] || "info";
}

function isOverdue(dateStr) {
  if (!dateStr) return false;
  return new Date(dateStr) < new Date();
}

const filteredVehicles = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return vehicles.value;
  return vehicles.value.filter(
    (v) =>
      (v.vehicle_name || "").toLowerCase().includes(q) ||
      (v.make || "").toLowerCase().includes(q) ||
      (v.model || "").toLowerCase().includes(q) ||
      (v.plate_number || v.license_plate || "").toLowerCase().includes(q) ||
      (v.assigned_driver || v.assigned_to || "").toLowerCase().includes(q)
  );
});

function openCreateDialog() {
  formMode.value = "create";
  form.value = defaultForm();
  formError.value = "";
  showFormDialog.value = true;
}

function onRowClick(event) {
  const v = event?.data;
  if (!v?.id) return;
  formMode.value = "edit";
  formError.value = "";
  form.value = {
    id: v.id,
    vehicle_name: v.vehicle_name || "",
    plate_number: v.plate_number || v.license_plate || "",
    make: v.make || "",
    model: v.model || "",
    year: v.year || new Date().getFullYear(),
    mileage: v.mileage || 0,
    status: v.status || "Active",
    assigned_driver: v.assigned_driver || v.assigned_to || v.driver_name || "",
  };
  showFormDialog.value = true;
}

async function submitForm() {
  formError.value = "";
  successMsg.value = "";
  if (!form.value.make.trim() || !form.value.model.trim()) {
    formError.value = "Make and model are required.";
    return;
  }
  const payload = {
    vehicle_name: form.value.vehicle_name || `${form.value.year} ${form.value.make} ${form.value.model}`,
    plate_number: form.value.plate_number,
    make: form.value.make.trim(),
    model: form.value.model.trim(),
    year: form.value.year,
    mileage: form.value.mileage,
    status: form.value.status,
    assigned_driver: form.value.assigned_driver,
  };
  saving.value = true;
  try {
    if (isEdit.value) {
      await api.patch(`/api/fleet/vehicles/${form.value.id}`, payload);
      successMsg.value = "Vehicle updated.";
    } else {
      await api.post("/api/fleet/vehicles", payload);
      successMsg.value = "Vehicle created.";
    }
    showFormDialog.value = false;
    await fetchVehicles();
  } catch (err) {
    formError.value = err.message || "Save failed.";
  } finally {
    saving.value = false;
  }
}

async function confirmDelete() {
  if (!form.value.id) return;
  deleting.value = true;
  try {
    await api.del(`/api/fleet/vehicles/${form.value.id}`);
    successMsg.value = "Vehicle deleted.";
    showDeleteDialog.value = false;
    showFormDialog.value = false;
    await fetchVehicles();
  } catch (err) {
    formError.value = err.message || "Delete failed.";
  } finally {
    deleting.value = false;
  }
}

async function fetchVehicles() {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await api.get("/api/fleet/vehicles");
    const payload = result?.data || result;
    vehicles.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load fleet vehicles";
  } finally {
    loading.value = false;
  }
}

onMounted(fetchVehicles);
</script>

<style scoped>
.dialog-form { display: grid; gap: 0.75rem; }
.form-field { display: grid; gap: 0.25rem; }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.form-row-3 { display: grid; grid-template-columns: 80px 1fr 1fr; gap: 0.75rem; }
.form-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; margin: 1rem 0; }
.inline-error { color: #b42318; margin: 0.5rem 0; }
.inline-success { color: #027a48; margin: 0.5rem 0; }
.overdue { color: #ef4444; font-weight: 600; }
</style>
