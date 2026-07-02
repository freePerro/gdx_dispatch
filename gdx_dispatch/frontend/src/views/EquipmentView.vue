<template>
    <section class="view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title" style="margin:0">Customer Equipment</h2>
          <InputText v-model="searchQuery" placeholder="Search by name, model, serial..." data-testid="equipment-search" style="margin-left:1rem" />
        </template>
        <template #end>
          <Button label="+ New Equipment" data-testid="new-equipment-btn" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="equipment-load-error">{{ loadError }}</div>
      <div v-if="successMsg" class="inline-success" data-testid="equipment-success">{{ successMsg }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-if="!loading"
        :value="filteredItems"
        data-testid="equipment-datatable"
        striped-rows
        @row-click="onRowClick"
      >
        <template #empty>
          <EmptyState icon="pi pi-wrench" title="No equipment yet"
            message="Track customer doors, openers, and parts to see warranty and service history at a glance."
            action-label="New Equipment" @action="openCreateDialog" />
        </template>
        <Column field="name" header="Name" sortable />
        <Column field="make" header="Make" />
        <Column field="model" header="Model" />
        <Column field="type" header="Type">
          <template #body="{ data }">{{ data.type || data.equipment_type || '-' }}</template>
        </Column>
        <Column field="serial_number" header="Serial #" />
        <Column field="install_date" header="Install Date">
          <template #body="{ data }">{{ data.install_date ? data.install_date.split('T')[0] : '-' }}</template>
        </Column>
        <Column field="warranty_expiry" header="Warranty" sortable>
          <template #body="{ data }">
            <span v-if="data.warranty_expiry" :class="{ 'warranty-expired': new Date(data.warranty_expiry) < new Date() }">
              {{ data.warranty_expiry.split('T')[0] }}
            </span>
            <span v-else>—</span>
          </template>
        </Column>
        <Column field="last_service_date" header="Last Service" sortable>
          <template #body="{ data }">{{ data.last_service_date ? data.last_service_date.split('T')[0] : '—' }}</template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status || 'active'" :severity="data.status === 'retired' ? 'secondary' : data.status === 'needs_service' ? 'warning' : 'success'" />
          </template>
        </Column>
        <Column field="customer_name" header="Customer" />
      </DataTable>

      <!-- Create / Edit Dialog -->
      <Dialog v-model:visible="showFormDialog" :header="isEdit ? 'Edit Equipment' : 'Add Equipment'" data-testid="equipment-form-dialog" :style="{ width: '32rem' }">
        <form class="dialog-form" @submit.prevent="submitForm">
          <div class="form-field">
            <label for="eq-customer">Customer</label>
            <InputText id="eq-customer" v-model="form.customer_name" data-testid="eq-customer-input" />
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-make">Make *</label>
              <InputText id="eq-make" v-model="form.make" data-testid="eq-make-input" />
            </div>
            <div class="form-field">
              <label for="eq-model">Model *</label>
              <InputText id="eq-model" v-model="form.model" data-testid="eq-model-input" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-serial">Serial Number</label>
              <InputText id="eq-serial" v-model="form.serial_number" data-testid="eq-serial-input" />
            </div>
            <div class="form-field">
              <label for="eq-type">Type</label>
              <Select id="eq-type" v-model="form.type" :options="equipmentTypes" data-testid="eq-type-dropdown" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-install-date">Install Date</label>
              <DatePicker id="eq-install-date" v-model="form.install_date" date-format="yy-mm-dd" data-testid="eq-install-date" />
            </div>
            <div class="form-field">
              <label for="eq-warranty">Warranty Expiry</label>
              <DatePicker id="eq-warranty" v-model="form.warranty_expiry" date-format="yy-mm-dd" data-testid="eq-warranty" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-last-service">Last Service Date</label>
              <DatePicker id="eq-last-service" v-model="form.last_service_date" date-format="yy-mm-dd" data-testid="eq-last-service" />
            </div>
            <div class="form-field">
              <label for="eq-status">Status</label>
              <Select id="eq-status" v-model="form.status" :options="['active', 'needs_service', 'retired']" data-testid="eq-status" />
            </div>
          </div>
          <div class="form-field">
            <label for="eq-notes">Notes</label>
            <Textarea id="eq-notes" v-model="form.notes" rows="3" data-testid="eq-notes-input" />
          </div>
          <div v-if="formError" class="inline-error" data-testid="eq-form-error">{{ formError }}</div>
          <div class="form-actions">
            <Button v-if="isEdit" type="button" label="Delete" severity="danger" text data-testid="eq-delete-btn" @click="showDeleteDialog = true" />
            <Button type="submit" :label="isEdit ? 'Save' : 'Create'" :loading="saving" data-testid="eq-submit-btn" />
          </div>
        </form>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog v-model:visible="showDeleteDialog" header="Confirm Delete" data-testid="eq-delete-dialog">
        <p>Delete this equipment record?</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button label="Delete" severity="danger" :loading="deleting" data-testid="eq-confirm-delete-btn" @click="confirmDelete" />
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import EmptyState from "../components/EmptyState.vue";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApi();
const items = ref([]);
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
const equipmentTypes = ["Garage Door", "Opener", "Spring", "Panel", "Track", "Sensor", "Remote", "Other"];

const isEdit = computed(() => formMode.value === "edit");

const defaultForm = () => ({
  id: null,
  customer_name: "",
  make: "",
  model: "",
  serial_number: "",
  type: "Garage Door",
  install_date: null,
  warranty_expiry: null,
  last_service_date: null,
  status: "active",
  notes: "",
});
const form = ref(defaultForm());

const filteredItems = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return items.value;
  return items.value.filter(
    (i) =>
      (i.name || "").toLowerCase().includes(q) ||
      (i.make || "").toLowerCase().includes(q) ||
      (i.model || "").toLowerCase().includes(q) ||
      (i.serial_number || "").toLowerCase().includes(q) ||
      (i.customer_name || "").toLowerCase().includes(q)
  );
});

function openCreateDialog() {
  formMode.value = "create";
  form.value = defaultForm();
  formError.value = "";
  showFormDialog.value = true;
}

function onRowClick(event) {
  const item = event?.data;
  if (!item?.id) return;
  formMode.value = "edit";
  formError.value = "";
  form.value = {
    id: item.id,
    customer_name: item.customer_name || "",
    make: item.make || "",
    model: item.model || "",
    serial_number: item.serial_number || "",
    type: item.type || item.equipment_type || "Garage Door",
    install_date: item.install_date ? new Date(item.install_date) : null,
    notes: item.notes || "",
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
    customer_name: form.value.customer_name,
    make: form.value.make.trim(),
    model: form.value.model.trim(),
    serial_number: form.value.serial_number,
    type: form.value.type,
    install_date: form.value.install_date instanceof Date ? form.value.install_date.toISOString().split("T")[0] : form.value.install_date,
    notes: form.value.notes,
    name: `${form.value.make.trim()} ${form.value.model.trim()}`,
  };
  saving.value = true;
  try {
    if (isEdit.value) {
      await api.patch(`/api/equipment/${form.value.id}`, payload);
      successMsg.value = "Equipment updated.";
    } else {
      await api.post("/api/equipment", payload);
      successMsg.value = "Equipment created.";
    }
    showFormDialog.value = false;
    await fetchItems();
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
    await api.del(`/api/equipment/${form.value.id}`);
    successMsg.value = "Equipment deleted.";
    showDeleteDialog.value = false;
    showFormDialog.value = false;
    await fetchItems();
  } catch (err) {
    formError.value = err.message || "Delete failed.";
  } finally {
    deleting.value = false;
  }
}

async function fetchItems() {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await api.get("/api/equipment");
    const payload = result?.data || result;
    items.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load equipment";
  } finally {
    loading.value = false;
  }
}

onMounted(fetchItems);
</script>

<style scoped>
.dialog-form { display: grid; gap: 0.75rem; }
.form-field { display: grid; gap: 0.25rem; }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.form-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; margin: 1rem 0; }
.inline-error { color: #b42318; margin: 0.5rem 0; }
.inline-success { color: #027a48; margin: 0.5rem 0; }
.warranty-expired { color: #ef4444; font-weight: 600; }
</style>
