<template>
    <section class="warranties-view view-card" data-testid="warranties-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="warranties-title">Warranties</h2>
        </template>
        <template #end>
          <Button
            label="New Warranty"
            icon="pi pi-plus"
            @click="openCreate"
            data-testid="warranties-new"
          />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap" data-testid="warranties-loading">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else
        :value="warranties"
        striped-rows
        responsiveLayout="scroll"
        emptyMessage="No warranties"
        class="clickable-row"
        data-testid="warranties-table"
      >
        <Column field="product" header="Product" />
        <Column field="customer" header="Customer" />
        <Column field="job_id" header="Job" />
        <Column field="start_date" header="Start" :body="({ data }) => formatDate(data.start_date)" />
        <Column field="expiry_date" header="Expiry" :body="({ data }) => formatDate(data.expiry_date)" />
        <Column field="status" header="Status" />
        <Column header="Actions">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              size="small"
              class="mr-2"
              @click.stop="openEdit(data)"
              data-testid="warranties-edit"
            />
            <Button
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              size="small"
              @click.stop="deleteWarranty(data)"
              data-testid="warranties-delete"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :header="editingWarranty ? `Edit ${editingWarranty.product}` : 'Create Warranty'"
        modal
        :style="{ width: '520px' }"
        data-testid="warranties-dialog"
      >
        <div class="form-grid" data-testid="warranties-form">
          <div class="form-field">
            <label>Product</label>
            <InputText v-model="form.product" class="w-full" data-testid="warranties-product" />
          </div>
          <div class="form-field">
            <label>Customer</label>
            <InputText v-model="form.customer" class="w-full" data-testid="warranties-customer" />
          </div>
          <div class="form-field">
            <label>Job ID</label>
            <InputText v-model="form.job_id" class="w-full" data-testid="warranties-job" />
          </div>
          <div class="form-field">
            <label>Start Date</label>
            <DatePicker v-model="form.start_date" dateFormat="yy-mm-dd" showIcon class="w-full" data-testid="warranties-start" />
          </div>
          <div class="form-field">
            <label>Expiry Date</label>
            <DatePicker v-model="form.expiry_date" dateFormat="yy-mm-dd" showIcon class="w-full" data-testid="warranties-expiry" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="form.status" :options="statusOptions" class="w-full" data-testid="warranties-status" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" data-testid="warranties-cancel" />
          <Button
            :label="editingWarranty ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveWarranty"
            data-testid="warranties-save"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const warranties = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const editingWarranty = ref(null);
const saving = ref(false);
const form = ref(emptyForm());

const statusOptions = [
  { label: "Active", value: "active" },
  { label: "Expired", value: "expired" },
  { label: "Claimed", value: "claimed" },
];

function emptyForm() {
  return {
    product: "",
    customer: "",
    job_id: "",
    start_date: null,
    expiry_date: null,
    status: "active",
  };
}

function formatDate(value) {
  if (!value) return "—";
  if (typeof value === "string" && value.includes("T")) {
    return value.split("T")[0];
  }
  return value;
}

function toDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toIso(value) {
  if (!value) return null;
  return value.toISOString().slice(0, 10);
}

async function loadWarranties() {
  loading.value = true;
  try {
    const data = await api.get("/api/warranties");
    warranties.value = Array.isArray(data) ? data : data?.items ?? [];
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editingWarranty.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(item) {
  editingWarranty.value = item;
  form.value = {
    product: item.product ?? "",
    customer: item.customer ?? "",
    job_id: item.job_id ?? "",
    start_date: toDate(item.start_date),
    expiry_date: toDate(item.expiry_date),
    status: item.status ?? "active",
  };
  showDialog.value = true;
}

function closeDialog() {
  showDialog.value = false;
  saving.value = false;
  editingWarranty.value = null;
  form.value = emptyForm();
}

async function saveWarranty() {
  if (!form.value.product.trim()) return;
  saving.value = true;
  const payload = {
    product: form.value.product,
    customer: form.value.customer,
    job_id: form.value.job_id,
    start_date: toIso(form.value.start_date),
    expiry_date: toIso(form.value.expiry_date),
    status: form.value.status,
  };
  try {
    if (editingWarranty.value) {
      const id = editingWarranty.value.id ?? editingWarranty.value.warranty_id;
      await api.patch(`/api/warranties/${encodeURIComponent(id)}`, payload, { successMessage: "Warranty updated" });
    } else {
      await api.post("/api/warranties", payload, { successMessage: "Warranty created" });
    }
    await loadWarranties();
    closeDialog();
  } finally {
    saving.value = false;
  }
}

async function deleteWarranty(item) {
  const id = item.id ?? item.warranty_id;
  if (!id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: "Remove this warranty?" }))) return;
  saving.value = true;
  try {
    await api.del(`/api/warranties/${encodeURIComponent(id)}`, { successMessage: "Warranty deleted" });
    await loadWarranties();
  } finally {
    saving.value = false;
  }
}

onMounted(loadWarranties);
</script>
