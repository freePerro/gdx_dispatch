<template>
    <section class="co-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Change Orders</h2>
        </template>
        <template #end>
          <Button icon="pi pi-download" label="Export" aria-label="Export CSV" text size="small" @click="exportRows" />
          <Button label="+ New Change Order" icon="pi pi-plus" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="filter-tabs">
        <Button v-for="s in ['all', 'draft', 'pending_approval', 'approved', 'declined', 'completed']" :key="s"
          :label="s.replace('_', ' ') + (counts[s] ? ` (${counts[s]})` : '')"
          :severity="statusFilter === s ? undefined : 'secondary'" size="small"
          @click="statusFilter = s" />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" :rowsPerPageOptions="[10, 20, 50, 100]" striped-rows
        @row-click="openEdit($event.data)" >
        <template #empty>
          <EmptyState icon="pi pi-file-edit" title="No Change Orders" message="Create a change order when job scope or price changes mid-job." actionLabel="+ Create First" @action="openCreate" />
        </template>
        <Column field="co_number" header="CO #" sortable style="width:120px" />
        <Column field="customer_name" header="Customer" sortable />
        <Column field="title" header="Description" sortable />
        <Column field="amount" header="Amount" sortable style="width:130px">
          <template #body="{ data }">{{ formatMoney(data.amount || 0) }}</template>
        </Column>
        <Column field="status" header="Status" sortable style="width:160px">
          <template #body="{ data }">
            <Tag :value="data.status?.replace('_', ' ')" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="created_at" header="Created" sortable style="width:130px">
          <template #body="{ data }">{{ data.created_at?.split('T')[0] || '—' }}</template>
        </Column>
        <Column header="Actions" style="width:160px">
          <template #body="{ data }">
            <Button v-if="data.status === 'pending_approval'" icon="pi pi-check" aria-label="Approve" severity="success" text size="small"
              v-tooltip="'Approve'" @click.stop="approveCo(data)" />
            <Button v-if="data.status === 'pending_approval'" icon="pi pi-times" aria-label="Remove" severity="danger" text size="small"
              v-tooltip="'Decline'" @click.stop="declineCo(data)" />
            <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openEdit(data)" />
            <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" :header="editingCo ? `Edit ${editingCo.co_number}` : 'New Change Order'"
        modal :style="{width: '600px'}" :closable="!isDirty" :close-on-escape="!isDirty">
        <div class="form-grid">
          <div class="form-field">
            <label>Job</label>
            <Select v-model="form.job_id" :options="jobs" optionLabel="label" optionValue="id"
              placeholder="Select job" filter showClear @change="onJobSelect" class="w-full" />
          </div>
          <FormField v-model="form.status" label="Status" as="select" :options="statusOptions" />
          <FormField v-model="form.customer_name" label="Customer Name" class="full-width" />
          <FormField v-model="form.title" label="Title" required placeholder="Additional outlet install" class="full-width" />
          <FormField v-model="form.description" label="Description / Scope Change" as="textarea" :rows="3" class="full-width" />
          <FormField v-model="form.reason" label="Reason" as="select" :options="reasonOptions" />
          <!-- D-S122-change-orders-create-flow — line-item editor.
               Total auto-computes from lines; the flat amount field is the
               sum (kept for legacy list-page rendering). -->
          <div class="form-field full-width">
            <label>Line Items</label>
            <LineItemEditor
              v-model:lines="form.line_items"
              data-testid="co-line-editor"
            />
            <div class="totals-row">
              <strong>Total:</strong>
              <strong>{{ formatMoney(lineSubtotal) }}</strong>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="cancelDialog" />
          <Button :label="editingCo ? 'Save' : 'Create'" icon="pi pi-check" @click="saveCo" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatMoney } from "../composables/useFormatters";
import EmptyState from "../components/EmptyState.vue";
import FormField from "../components/FormField.vue";
import { useDirtyDialog } from "../composables/useDirtyDialog";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import LineItemEditor from "../components/LineItemEditor.vue";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const orders = ref([]);
const jobs = ref([]);
const loading = ref(true);
const statusFilter = ref("all");
const showDialog = ref(false);
const editingCo = ref(null);
const saving = ref(false);

useListPrefs(
  "change-orders",
  { statusFilter },
  { statusFilter: { default: "all", valid: (v) => ["all", "draft", "pending_approval", "approved", "declined", "completed"].includes(v) } },
);

// {label,value} shape for FormField's Select wrapper.
const statusOptions = ["draft", "pending_approval", "approved", "declined", "completed"]
  .map((v) => ({ label: v.replace(/_/g, " "), value: v }));
const reasonOptions = ["customer_request", "scope_added", "damage_found", "code_compliance", "material_change", "other"]
  .map((v) => ({ label: v.replace(/_/g, " "), value: v }));

const emptyForm = () => ({
  job_id: null, customer_id: null, customer_name: "",
  title: "", description: "", reason: "customer_request",
  amount: 0, status: "draft",
  // D-S122-change-orders-create-flow — line-items array consumed by
  // <LineItemEditor>. Backend sums these into `amount` server-side.
  line_items: [{ description: '', quantity: 1, unit_price: 0 }],
});
const form = ref(emptyForm());

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value);
const { exportCsv } = useTableExport();

function exportRows() {
  exportCsv(filtered.value, [
    { field: "co_number", header: "CO #" },
    { field: "customer_name", header: "Customer" },
    { field: "title", header: "Description" },
    { field: "amount", header: "Amount" },
    { field: "status", header: "Status" },
    { field: "created_at", header: "Created" },
  ], "change-orders");
}

const lineSubtotal = computed(() =>
  (form.value.line_items || []).reduce(
    (s, li) => s + Number(li.quantity || 0) * Number(li.unit_price || 0),
    0,
  )
);

const counts = computed(() => {
  const c = { all: orders.value.length };
  orders.value.forEach((o) => { c[o.status] = (c[o.status] || 0) + 1; });
  return c;
});

const filtered = computed(() => {
  if (statusFilter.value === "all") return orders.value;
  return orders.value.filter((o) => o.status === statusFilter.value);
});

function statusSeverity(s) {
  return { draft: "secondary", pending_approval: "warning", approved: "success",
           declined: "danger", completed: "info" }[s] || "secondary";
}

async function loadOrders() {
  loading.value = true;
  try {
    const data = await api.get("/api/change-orders");
    orders.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    console.error('load_change_orders_failed', err?.message || err);
    orders.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadJobs() {
  try {
    const data = await api.get("/api/jobs?page_size=200");
    const list = Array.isArray(data) ? data : data?.items || [];
    jobs.value = list.map((j) => ({
      id: j.id,
      label: `${j.job_number || j.id?.toString().slice(0, 8)} — ${j.customer_name || ''}`,
      customer_id: j.customer_id,
      customer_name: j.customer_name,
    }));
  } catch { jobs.value = []; }
}

function onJobSelect() {
  const job = jobs.value.find((j) => j.id === form.value.job_id);
  if (job) {
    form.value.customer_id = job.customer_id;
    form.value.customer_name = job.customer_name;
  }
}

function openCreate() {
  editingCo.value = null;
  form.value = emptyForm();
  snapshot();
  showDialog.value = true;
}

function openEdit(co) {
  editingCo.value = co;
  form.value = { ...co };
  snapshot();
  showDialog.value = true;
}

function cancelDialog() {
  if (confirmDiscard()) showDialog.value = false;
}

async function saveCo() {
  if (!form.value.title.trim()) return;
  saving.value = true;
  try {
    // D-S122-change-orders-create-flow — filter blank rows + send line_items
    // when present so the backend writes ChangeOrderLine rows + sums amount.
    const cleanLines = (form.value.line_items || [])
      .filter((li) => li.description?.trim() && Number(li.unit_price || 0) > 0)
      .map((li) => ({
        description: li.description.trim(),
        quantity: Math.max(1, Number(li.quantity) || 1),
        unit_price: Number(li.unit_price),
      }));
    const payload = {
      ...form.value,
      line_items: cleanLines,
      // When there ARE line items, force amount=0 — backend recomputes from
      // line subtotal. When there aren't, send the flat amount.
      amount: cleanLines.length ? 0 : Number(form.value.amount || 0),
    };
    if (editingCo.value) {
      await api.patch(`/api/change-orders/${editingCo.value.id}`, payload);
    } else {
      await api.post("/api/change-orders", payload);
    }
    showDialog.value = false;
    await loadOrders();
  } catch (err) {
    console.error('save_change_order_failed', err?.message || err);
  } finally {
    saving.value = false;
  }
}

async function approveCo(co) {
  await api.post(`/api/change-orders/${co.id}/approve`, {});
  await loadOrders();
}

async function declineCo(co) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Decline ${co.co_number}?` }))) return;
  await api.post(`/api/change-orders/${co.id}/decline`, {});
  await loadOrders();
}

async function confirmDelete(co) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${co.co_number}?` }))) return;
  await api.delete(`/api/change-orders/${co.id}`);
  await loadOrders();
}

onMounted(async () => {
  await Promise.all([loadOrders(), loadJobs()]);
});
</script>

<style scoped>
.page-title { margin: 0; }
.filter-tabs { display: flex; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.form-field label { font-size: 0.82rem; font-weight: 600; color: var(--p-text-muted-color); }
.full-width { grid-column: 1 / -1; }
.w-full { width: 100%; }
.clickable-row { cursor: pointer; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
