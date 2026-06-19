<template>
    <section class="co-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Change Orders</h2>
        </template>
        <template #end>
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
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" striped-rows
        @row-click="openEdit($event.data)" >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-file-edit" style="font-size:3rem; color:#64748b;"></i>
            <h3>No Change Orders</h3>
            <p>Create a change order when job scope or price changes mid-job.</p>
            <Button label="+ Create First" @click="openCreate" />
          </div>
        </template>
        <Column field="co_number" header="CO #" style="width:120px" />
        <Column field="customer_name" header="Customer" />
        <Column field="title" header="Description" />
        <Column field="amount" header="Amount" sortable style="width:130px">
          <template #body="{ data }">${{ Number(data.amount || 0).toFixed(2) }}</template>
        </Column>
        <Column field="status" header="Status" sortable style="width:160px">
          <template #body="{ data }">
            <Tag :value="data.status?.replace('_', ' ')" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="created_at" header="Created" style="width:130px">
          <template #body="{ data }">{{ data.created_at?.split('T')[0] || '—' }}</template>
        </Column>
        <Column header="Actions" style="width:160px">
          <template #body="{ data }">
            <Button v-if="data.status === 'pending_approval'" icon="pi pi-check" severity="success" text size="small"
              v-tooltip="'Approve'" @click.stop="approveCo(data)" />
            <Button v-if="data.status === 'pending_approval'" icon="pi pi-times" aria-label="Remove" severity="danger" text size="small"
              v-tooltip="'Decline'" @click.stop="declineCo(data)" />
            <Button icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openEdit(data)" />
            <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" :header="editingCo ? `Edit ${editingCo.co_number}` : 'New Change Order'"
        modal :style="{width: '600px'}">
        <div class="form-grid">
          <div class="form-field">
            <label>Job</label>
            <Select v-model="form.job_id" :options="jobs" optionLabel="label" optionValue="id"
              placeholder="Select job" filter showClear @change="onJobSelect" class="w-full" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="form.status" :options="statusOptions" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Customer Name</label>
            <InputText v-model="form.customer_name" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Title *</label>
            <InputText v-model="form.title" placeholder="Additional outlet install" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Description / Scope Change</label>
            <Textarea v-model="form.description" rows="3" class="w-full" />
          </div>
          <div class="form-field">
            <label>Reason</label>
            <Select v-model="form.reason" :options="reasonOptions" class="w-full" />
          </div>
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
              <strong>${{ lineSubtotal.toFixed(2) }}</strong>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button :label="editingCo ? 'Save' : 'Create'" icon="pi pi-check" @click="saveCo" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
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

const statusOptions = ["draft", "pending_approval", "approved", "declined", "completed"];
const reasonOptions = ["customer_request", "scope_added", "damage_found", "code_compliance", "material_change", "other"];

const emptyForm = () => ({
  job_id: null, customer_id: null, customer_name: "",
  title: "", description: "", reason: "customer_request",
  amount: 0, status: "draft",
  // D-S122-change-orders-create-flow — line-items array consumed by
  // <LineItemEditor>. Backend sums these into `amount` server-side.
  line_items: [{ description: '', quantity: 1, unit_price: 0 }],
});
const form = ref(emptyForm());

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
  showDialog.value = true;
}

function openEdit(co) {
  editingCo.value = co;
  form.value = { ...co };
  showDialog.value = true;
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
.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); }
.empty-state h3 { margin: 1rem 0 0.5rem; color: var(--text-color); }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
