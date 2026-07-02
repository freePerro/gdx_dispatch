<template>
    <section class="view-card">
      <Toolbar>
        <template #start>
          <InputText v-model="searchQuery" placeholder="Search parts" data-testid="inventory-search" />
        </template>
        <template #end>
          <Button icon="pi pi-download" label="Export" aria-label="Export CSV" text size="small" @click="exportRows" />
          <Button label="+ New Part" data-testid="new-part-btn" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="inventory-load-error">{{ loadError }}</div>
      <div v-if="successMsg" class="inline-success" data-testid="inventory-success">{{ successMsg }}</div>
      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-if="!loading"
        :value="filteredParts"
        data-testid="inventory-datatable"
        striped-rows
        :paginator="true"
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50, 100]"
        paginatorTemplate="FirstPageLink PrevPageLink CurrentPageReport NextPageLink LastPageLink RowsPerPageDropdown"
        currentPageReportTemplate="{first}–{last} of {totalRecords}"
        @row-click="onRowClick"
      >
        <template #empty>
          <EmptyState icon="pi pi-box" title="No parts yet" message="Add your first inventory part or import from a vendor catalog." actionLabel="+ New Part" @action="openCreateDialog" />
        </template>
        <Column field="part_name" header="Part Name" sortable>
          <template #body="{ data }">{{ data.part_name || data.name || '-' }}</template>
        </Column>
        <Column field="sku" header="SKU" sortable />
        <Column field="quantity" header="Qty on Hand" sortable>
          <template #body="{ data }">
            <span :class="{ 'low-stock': isLowStock(data) }">
              {{ data.quantity ?? data.quantity_on_hand ?? data.qty ?? '-' }}
            </span>
          </template>
        </Column>
        <Column field="reorder_level" header="Reorder Level" sortable>
          <template #body="{ data }">{{ data.reorder_level ?? data.reorder_point ?? '-' }}</template>
        </Column>
        <Column field="unit_cost" header="Unit Cost" sortable>
          <template #body="{ data }">{{ formatMoney(data.unit_cost) }}</template>
        </Column>
        <Column field="supplier" header="Supplier" sortable />
      </DataTable>

      <!-- Create / Edit Dialog -->
      <Dialog v-model:visible="showFormDialog" :header="isEdit ? 'Edit Part' : 'Add Part'" data-testid="inventory-form-dialog" :style="{ width: '32rem' }" :closable="!isDirty" :close-on-escape="!isDirty">
        <form class="dialog-form" @submit.prevent="submitForm">
          <FormField id="inv-name" v-model="form.part_name" label="Part Name" required data-testid="inv-name-input" />
          <div class="form-row-2">
            <FormField id="inv-sku" v-model="form.sku" label="SKU" data-testid="inv-sku-input" />
            <FormField id="inv-supplier" v-model="form.supplier" label="Supplier" data-testid="inv-supplier-input" />
          </div>
          <div class="form-row-3">
            <div class="form-field">
              <label for="inv-qty">Quantity *</label>
              <InputNumber id="inv-qty" v-model="form.quantity" :min="0" data-testid="inv-qty-input" />
            </div>
            <div class="form-field">
              <label for="inv-reorder">Reorder Level</label>
              <InputNumber id="inv-reorder" v-model="form.reorder_level" :min="0" data-testid="inv-reorder-input" />
            </div>
            <div class="form-field">
              <label for="inv-cost">Unit Cost ($)</label>
              <InputNumber id="inv-cost" v-model="form.unit_cost" mode="currency" currency="USD" :min="0" data-testid="inv-cost-input" />
            </div>
          </div>
          <div v-if="formError" class="inline-error" data-testid="inv-form-error">{{ formError }}</div>
          <div class="form-actions">
            <Button v-if="isEdit" type="button" label="Delete" severity="danger" text data-testid="inv-delete-btn" @click="showDeleteDialog = true" />
            <Button type="button" label="Cancel" severity="secondary" text data-testid="inv-cancel-btn" @click="cancelForm" />
            <Button type="submit" :label="isEdit ? 'Save' : 'Create'" :loading="saving" data-testid="inv-submit-btn" />
          </div>
        </form>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog v-model:visible="showDeleteDialog" header="Confirm Delete" data-testid="inv-delete-dialog">
        <p>Delete this part from inventory?</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button label="Delete" severity="danger" :loading="deleting" data-testid="inv-confirm-delete-btn" @click="confirmDelete" />
        </div>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import EmptyState from "../components/EmptyState.vue";
import FormField from "../components/FormField.vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { formatMoney } from "../composables/useFormatters";
import { useDirtyDialog } from "../composables/useDirtyDialog";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Toolbar from "primevue/toolbar";

const api = useApi();
const parts = ref([]);
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

useListPrefs(
  "inventory",
  { searchQuery },
  { searchQuery: { default: "", valid: (v) => typeof v === "string" } },
);

const isEdit = computed(() => formMode.value === "edit");

const defaultForm = () => ({
  id: null,
  part_name: "",
  sku: "",
  quantity: 0,
  reorder_level: 5,
  unit_cost: 0,
  supplier: "",
});
const form = ref(defaultForm());

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value);
const { exportCsv } = useTableExport();

function exportRows() {
  const rows = filteredParts.value.map((p) => ({
    part_name: p.part_name || p.name || "",
    sku: p.sku || "",
    quantity: p.quantity ?? p.quantity_on_hand ?? p.qty ?? "",
    reorder_level: p.reorder_level ?? p.reorder_point ?? "",
    unit_cost: p.unit_cost ?? "",
    supplier: p.supplier || "",
  }));
  exportCsv(rows, [
    { field: "part_name", header: "Part Name" },
    { field: "sku", header: "SKU" },
    { field: "quantity", header: "Qty on Hand" },
    { field: "reorder_level", header: "Reorder Level" },
    { field: "unit_cost", header: "Unit Cost" },
    { field: "supplier", header: "Supplier" },
  ], "inventory");
}

function isLowStock(item) {
  const qty = item.quantity ?? item.quantity_on_hand ?? item.qty ?? 0;
  const reorder = item.reorder_level ?? item.reorder_point ?? 0;
  return qty <= reorder && reorder > 0;
}

const filteredParts = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return parts.value;
  return parts.value.filter(
    (p) =>
      (p.part_name || p.name || "").toLowerCase().includes(q) ||
      (p.sku || "").toLowerCase().includes(q) ||
      (p.supplier || "").toLowerCase().includes(q)
  );
});

function openCreateDialog() {
  formMode.value = "create";
  form.value = defaultForm();
  formError.value = "";
  snapshot();
  showFormDialog.value = true;
}

function cancelForm() {
  if (confirmDiscard()) showFormDialog.value = false;
}

function onRowClick(event) {
  const p = event?.data;
  if (!p?.id) return;
  formMode.value = "edit";
  formError.value = "";
  form.value = {
    id: p.id,
    part_name: p.part_name || p.name || "",
    sku: p.sku || "",
    quantity: p.quantity ?? p.quantity_on_hand ?? p.qty ?? 0,
    reorder_level: p.reorder_level ?? p.reorder_point ?? 5,
    unit_cost: p.unit_cost ?? 0,
    supplier: p.supplier || "",
  };
  snapshot();
  showFormDialog.value = true;
}

async function submitForm() {
  formError.value = "";
  successMsg.value = "";
  if (!form.value.part_name.trim()) {
    formError.value = "Part name is required.";
    return;
  }
  const payload = {
    part_name: form.value.part_name.trim(),
    name: form.value.part_name.trim(),
    sku: form.value.sku,
    quantity: form.value.quantity,
    quantity_on_hand: form.value.quantity,
    reorder_level: form.value.reorder_level,
    reorder_point: form.value.reorder_level,
    unit_cost: form.value.unit_cost,
    supplier: form.value.supplier,
  };
  saving.value = true;
  try {
    if (isEdit.value) {
      await api.patch(`/api/inventory/parts/${form.value.id}`, payload);
      successMsg.value = "Part updated.";
    } else {
      await api.post("/api/inventory/parts", payload);
      successMsg.value = "Part created.";
    }
    showFormDialog.value = false;
    await fetchParts();
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
    await api.del(`/api/inventory/parts/${form.value.id}`);
    successMsg.value = "Part deleted.";
    showDeleteDialog.value = false;
    showFormDialog.value = false;
    await fetchParts();
  } catch (err) {
    formError.value = err.message || "Delete failed.";
  } finally {
    deleting.value = false;
  }
}

async function fetchParts() {
  loading.value = true;
  loadError.value = "";
  try {
    const result = await api.get("/api/inventory/parts");
    const payload = result?.data || result;
    parts.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch (e) {
    loadError.value = e.message || "Failed to load inventory";
  } finally {
    loading.value = false;
  }
}

onMounted(fetchParts);
</script>

<style scoped>
.dialog-form { display: grid; gap: 0.75rem; }
.form-field { display: grid; gap: 0.25rem; }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.form-row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.75rem; }
.form-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.5rem; }
.spinner-wrap { display: flex; justify-content: center; margin: 1rem 0; }
.inline-error { color: #b42318; margin: 0.5rem 0; }
.inline-success { color: #027a48; margin: 0.5rem 0; }
.low-stock { color: #dc2626; font-weight: 700; }
</style>
