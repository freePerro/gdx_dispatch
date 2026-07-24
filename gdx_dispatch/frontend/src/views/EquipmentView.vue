<template>
    <section class="view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title" style="margin:0">Customer Equipment</h2>
          <InputText v-model="searchQuery" placeholder="Search by make, model, serial..." data-testid="equipment-search" style="margin-left:1rem" />
        </template>
        <template #end>
          <Button label="+ New Equipment" data-testid="new-equipment-btn" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="equipment-load-error">{{ loadError }}</div>
      <div v-if="successMsg" class="inline-success" data-testid="equipment-success">{{ successMsg }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <!-- Tier-6 rework (contract-gap sweep): the old table rendered
           name/status/last_service/warranty_expiry — none of which
           /api/equipment serves — and the form sent customer_name/make/type
           where the API requires customer_id/manufacturer/equipment_type, so
           creating equipment from this page had never once succeeded and
           edits silently didn't save. Columns and payloads now match
           routers/equipment_tracking.py exactly. -->
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
            message="Track customer doors, openers, and parts to see warranty coverage at a glance."
            action-label="New Equipment" @action="openCreateDialog" />
        </template>
        <Column field="equipment_type" header="Type" sortable>
          <template #body="{ data }">{{ typeLabel(data.equipment_type) }}</template>
        </Column>
        <Column field="manufacturer" header="Make" sortable>
          <template #body="{ data }">{{ data.manufacturer || '—' }}</template>
        </Column>
        <Column field="model" header="Model" />
        <Column field="serial_number" header="Serial #" />
        <Column field="install_date" header="Install Date">
          <template #body="{ data }">{{ (data.install_date || '').split('T')[0] || '—' }}</template>
        </Column>
        <Column field="warranty_expires_on" header="Warranty" sortable>
          <template #body="{ data }">
            <span v-if="data.warranty_expires_on" :class="{ 'warranty-expired': new Date(data.warranty_expires_on) < new Date() }">
              {{ data.warranty_expires_on.split('T')[0] }}
            </span>
            <span v-else>—</span>
          </template>
        </Column>
        <Column field="customer_id" header="Customer">
          <template #body="{ data }">{{ customerName(data.customer_id) }}</template>
        </Column>
      </DataTable>

      <!-- Create / Edit Dialog -->
      <Dialog v-model:visible="showFormDialog" :header="isEdit ? 'Edit Equipment' : 'Add Equipment'" data-testid="equipment-form-dialog" :style="{ width: '32rem' }">
        <form class="dialog-form" @submit.prevent="submitForm">
          <div class="form-field">
            <label for="eq-customer">Customer *</label>
            <!-- customer_id is the equipment's identity; the update endpoint
                 deliberately has no customer field, so it locks on edit. -->
            <Select
              id="eq-customer"
              v-model="form.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              filter
              placeholder="Select customer"
              :disabled="isEdit"
              class="w-full"
              data-testid="eq-customer-input"
            />
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-type">Type *</label>
              <Select id="eq-type" v-model="form.equipment_type" :options="equipmentTypeOptions" optionLabel="label" optionValue="value" data-testid="eq-type-dropdown" />
            </div>
            <div class="form-field">
              <label for="eq-make">Make</label>
              <InputText id="eq-make" v-model="form.manufacturer" data-testid="eq-make-input" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-model">Model</label>
              <InputText id="eq-model" v-model="form.model" data-testid="eq-model-input" />
            </div>
            <div class="form-field">
              <label for="eq-serial">Serial Number</label>
              <InputText id="eq-serial" v-model="form.serial_number" data-testid="eq-serial-input" />
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-field">
              <label for="eq-install-date">Install Date</label>
              <DatePicker id="eq-install-date" v-model="form.install_date" date-format="yy-mm-dd" data-testid="eq-install-date" />
            </div>
            <div class="form-field">
              <label for="eq-warranty">Warranty Expires</label>
              <DatePicker id="eq-warranty" v-model="form.warranty_expires_on" date-format="yy-mm-dd" data-testid="eq-warranty" />
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
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";

const api = useApi();
const items = ref([]);
const customers = ref([]);
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

// The backend's ALLOWED_EQUIPMENT_TYPES enum — not free text.
const equipmentTypeOptions = [
  { label: "Torsion Spring", value: "torsion_spring" },
  { label: "Extension Spring", value: "extension_spring" },
  { label: "Opener", value: "opener" },
  { label: "Door Panel", value: "door_panel" },
  { label: "Track", value: "track" },
  { label: "Roller", value: "roller" },
];

const isEdit = computed(() => formMode.value === "edit");

const customerOptions = computed(() =>
  customers.value.map((c) => ({ label: c.name || String(c.id), value: String(c.id) })),
);
const customerNames = computed(() => {
  const map = {};
  for (const c of customers.value) map[String(c.id)] = c.name || String(c.id);
  return map;
});

function customerName(id) {
  return customerNames.value[String(id)] || String(id || "—").slice(0, 8);
}

function typeLabel(value) {
  return equipmentTypeOptions.find((o) => o.value === value)?.label || value || "—";
}

const defaultForm = () => ({
  id: null,
  customer_id: null,
  equipment_type: "opener",
  manufacturer: "",
  model: "",
  serial_number: "",
  install_date: null,
  warranty_expires_on: null,
  notes: "",
});
const form = ref(defaultForm());

const filteredItems = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return items.value;
  return items.value.filter(
    (i) =>
      (i.manufacturer || "").toLowerCase().includes(q) ||
      (i.model || "").toLowerCase().includes(q) ||
      (i.serial_number || "").toLowerCase().includes(q) ||
      customerName(i.customer_id).toLowerCase().includes(q),
  );
});

function toDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function toIso(value) {
  if (!value) return null;
  return value instanceof Date ? value.toISOString().split("T")[0] : value;
}

async function loadItems() {
  loading.value = true;
  loadError.value = "";
  try {
    const data = await api.get("/api/equipment");
    items.value = Array.isArray(data) ? data : data?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load equipment.";
  } finally {
    loading.value = false;
  }
}

async function loadCustomers() {
  try {
    const data = await api.get("/api/customers?per_page=1000", { suppressErrorToast: true });
    customers.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    customers.value = [];
  }
}

function openCreateDialog() {
  formMode.value = "create";
  form.value = defaultForm();
  formError.value = "";
  showFormDialog.value = true;
}

function onRowClick(event) {
  const item = event.data;
  formMode.value = "edit";
  form.value = {
    id: item.id,
    customer_id: item.customer_id ? String(item.customer_id) : null,
    equipment_type: item.equipment_type || "opener",
    manufacturer: item.manufacturer || "",
    model: item.model || "",
    serial_number: item.serial_number || "",
    install_date: toDate(item.install_date),
    warranty_expires_on: toDate(item.warranty_expires_on),
    notes: item.notes || "",
  };
  formError.value = "";
  showFormDialog.value = true;
}

async function submitForm() {
  formError.value = "";
  successMsg.value = "";
  if (!form.value.customer_id && !isEdit.value) {
    formError.value = "Customer is required.";
    return;
  }
  if (!form.value.equipment_type) {
    formError.value = "Type is required.";
    return;
  }
  const payload = {
    equipment_type: form.value.equipment_type,
    manufacturer: form.value.manufacturer?.trim() || null,
    model: form.value.model?.trim() || null,
    serial_number: form.value.serial_number?.trim() || null,
    install_date: toIso(form.value.install_date),
    warranty_expires_on: toIso(form.value.warranty_expires_on),
    notes: form.value.notes || null,
  };
  saving.value = true;
  try {
    if (isEdit.value) {
      await api.patch(`/api/equipment/${form.value.id}`, payload);
      successMsg.value = "Equipment updated.";
    } else {
      await api.post("/api/equipment", { ...payload, customer_id: form.value.customer_id });
      successMsg.value = "Equipment created.";
    }
    showFormDialog.value = false;
    await loadItems();
  } catch (e) {
    formError.value = e.message || "Save failed.";
  } finally {
    saving.value = false;
  }
}

async function confirmDelete() {
  deleting.value = true;
  try {
    await api.del(`/api/equipment/${form.value.id}`, { successMessage: "Equipment deleted" });
    showDeleteDialog.value = false;
    showFormDialog.value = false;
    await loadItems();
  } catch {
    /* api helper toasts */
  } finally {
    deleting.value = false;
  }
}

onMounted(() => {
  loadItems();
  loadCustomers();
});
</script>

<style scoped>
.inline-error { color: var(--color-danger-500, #dc2626); margin: 0.5rem 0; }
.inline-success { color: var(--color-success-500, #16a34a); margin: 0.5rem 0; }
.warranty-expired { color: var(--color-danger-500, #dc2626); font-weight: 600; }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
</style>
