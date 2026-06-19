<template>
    <section class="po-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Purchase Orders</h2>
        </template>
        <template #end>
          <Button label="+ New PO" icon="pi pi-plus" data-testid="new-po-btn" @click="openCreate" />
        </template>
      </Toolbar>

      <!-- Status tabs -->
      <div class="filter-tabs">
        <Button v-for="s in ['all', 'draft', 'sent', 'received', 'cancelled']" :key="s"
          :label="s + (counts[s] ? ` (${counts[s]})` : '')"
          :severity="statusFilter === s ? undefined : 'secondary'" size="small"
          @click="statusFilter = s" :data-testid="`filter-${s}`" />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" striped-rows
        data-testid="pos-table" @row-click="openDetail($event.data)" >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-inbox" style="font-size:3rem; color:#64748b;"></i>
            <h3>No Purchase Orders</h3>
            <p>Create a PO to order parts from a vendor.</p>
            <Button label="+ Create First PO" @click="openCreate" />
          </div>
        </template>
        <Column field="po_number" header="PO #" sortable style="width:130px" />
        <Column field="vendor_name" header="Vendor" sortable />
        <Column field="order_date" header="Order Date" sortable style="width:130px">
          <template #body="{ data }">{{ data.order_date || '—' }}</template>
        </Column>
        <Column field="expected_date" header="Expected" style="width:130px">
          <template #body="{ data }">{{ data.expected_date || '—' }}</template>
        </Column>
        <Column field="status" header="Status" sortable style="width:120px">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="total" header="Total" sortable style="width:120px">
          <template #body="{ data }">${{ Number(data.total || 0).toFixed(2) }}</template>
        </Column>
        <Column header="Actions" style="width:130px">
          <template #body="{ data }">
            <Button v-if="data.status === 'sent'" icon="pi pi-check" severity="success" text size="small"
              v-tooltip="'Receive'" @click.stop="receivePo(data)" />
            <Button icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openDetail(data)" />
            <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small"
              v-if="data.status !== 'received'" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- Create/Edit Dialog -->
      <Dialog v-model:visible="showDialog" :header="editingPo ? `Edit ${editingPo.po_number}` : 'New Purchase Order'"
        modal :style="{width: '800px'}">
        <div class="form-grid">
          <div class="form-field">
            <label>Vendor *</label>
            <Select v-model="form.vendor_id" :options="vendors" optionLabel="name" optionValue="id"
              placeholder="Select vendor" filter showClear @change="onVendorSelect" class="w-full" />
          </div>
          <div class="form-field">
            <label>Status</label>
            <Select v-model="form.status" :options="['draft', 'sent']" class="w-full" />
          </div>
          <div class="form-field">
            <label>Order Date</label>
            <DatePicker v-model="form.order_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field">
            <label>Expected Date</label>
            <DatePicker v-model="form.expected_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea v-model="form.notes" rows="2" class="w-full" />
          </div>
        </div>

        <h3 style="margin-top:1.5rem;">Line Items</h3>
        <DataTable
      responsiveLayout="scroll" :value="form.lines">
          <Column header="Description">
            <template #body="{ data, index }">
              <InputText v-model="data.description" class="w-full" placeholder="Part name or description" />
            </template>
          </Column>
          <Column header="SKU" style="width:140px">
            <template #body="{ data }">
              <InputText v-model="data.sku" class="w-full" />
            </template>
          </Column>
          <Column header="Qty" style="width:100px">
            <template #body="{ data }">
              <InputNumber v-model="data.quantity_ordered" :min="1" class="w-full" />
            </template>
          </Column>
          <Column header="Unit Cost" style="width:130px">
            <template #body="{ data }">
              <InputNumber v-model="data.unit_cost" mode="currency" currency="USD" class="w-full" />
            </template>
          </Column>
          <Column header="Total" style="width:110px">
            <template #body="{ data }">
              ${{ ((Number(data.unit_cost || 0)) * (Number(data.quantity_ordered || 0))).toFixed(2) }}
            </template>
          </Column>
          <Column style="width:50px">
            <template #body="{ index }">
              <Button icon="pi pi-times" aria-label="Remove" text severity="danger" size="small" @click="removeLine(index)" />
            </template>
          </Column>
        </DataTable>
        <Button label="+ Add Line" icon="pi pi-plus" text size="small" @click="addLine" style="margin-top:0.5rem;" />

        <div class="totals-row">
          <div>
            <label>Tax</label>
            <InputNumber v-model="form.tax" mode="currency" currency="USD" />
          </div>
          <div>
            <label>Shipping</label>
            <InputNumber v-model="form.shipping" mode="currency" currency="USD" />
          </div>
          <div class="total-display">
            <label>Total</label>
            <div class="total-amount">${{ calculatedTotal.toFixed(2) }}</div>
          </div>
        </div>

        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button :label="editingPo ? 'Save' : 'Create'" icon="pi pi-check" @click="savePo" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
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
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const pos = ref([]);
const vendors = ref([]);
const loading = ref(true);
const statusFilter = ref("all");
const showDialog = ref(false);
const editingPo = ref(null);
const saving = ref(false);

const emptyLine = () => ({ description: "", sku: "", quantity_ordered: 1, unit_cost: 0 });
const emptyForm = () => ({
  vendor_id: null, vendor_name: "", status: "draft",
  order_date: new Date(), expected_date: null, notes: "",
  tax: 0, shipping: 0, lines: [emptyLine()],
});
const form = ref(emptyForm());

const counts = computed(() => {
  const c = { all: pos.value.length, draft: 0, sent: 0, received: 0, cancelled: 0 };
  pos.value.forEach((p) => { if (c[p.status] !== undefined) c[p.status]++; });
  return c;
});

const filtered = computed(() => {
  if (statusFilter.value === "all") return pos.value;
  return pos.value.filter((p) => p.status === statusFilter.value);
});

const calculatedTotal = computed(() => {
  const subtotal = form.value.lines.reduce((s, l) =>
    s + (Number(l.unit_cost || 0) * Number(l.quantity_ordered || 0)), 0);
  return subtotal + Number(form.value.tax || 0) + Number(form.value.shipping || 0);
});

function statusSeverity(status) {
  return { draft: "secondary", sent: "info", received: "success", cancelled: "danger" }[status] || "secondary";
}

async function loadPos() {
  loading.value = true;
  try {
    const data = await api.get("/api/purchase-orders");
    pos.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    console.error('load_purchase_orders_failed', err?.message || err);
    pos.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadVendors() {
  try {
    const data = await api.get("/api/vendors");
    vendors.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    vendors.value = [];
  }
}

function onVendorSelect() {
  const v = vendors.value.find((x) => x.id === form.value.vendor_id);
  if (v) form.value.vendor_name = v.name;
}

function openCreate() {
  editingPo.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openDetail(po) {
  editingPo.value = po;
  form.value = {
    vendor_id: po.vendor_id, vendor_name: po.vendor_name, status: po.status,
    order_date: po.order_date ? new Date(po.order_date) : new Date(),
    expected_date: po.expected_date ? new Date(po.expected_date) : null,
    notes: po.notes, tax: po.tax, shipping: po.shipping,
    lines: po.lines?.length ? [...po.lines] : [emptyLine()],
  };
  showDialog.value = true;
}

function addLine() {
  form.value.lines.push(emptyLine());
}

function removeLine(index) {
  form.value.lines.splice(index, 1);
  if (form.value.lines.length === 0) form.value.lines.push(emptyLine());
}

async function savePo() {
  saving.value = true;
  try {
    const payload = {
      ...form.value,
      order_date: form.value.order_date instanceof Date ? form.value.order_date.toISOString().split('T')[0] : form.value.order_date,
      expected_date: form.value.expected_date instanceof Date ? form.value.expected_date.toISOString().split('T')[0] : form.value.expected_date,
    };
    if (editingPo.value) {
      await api.patch(`/api/purchase-orders/${editingPo.value.id}`, payload);
    } else {
      await api.post("/api/purchase-orders", payload);
    }
    showDialog.value = false;
    await loadPos();
  } catch (err) {
    console.error('save_purchase_order_failed', err?.message || err);
  } finally {
    saving.value = false;
  }
}

async function receivePo(po) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Mark ${po.po_number} as received? This will increase inventory stock.` }))) return;
  await api.post(`/api/purchase-orders/${po.id}/receive`, {});
  await loadPos();
}

async function confirmDelete(po) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${po.po_number}?` }))) return;
  await api.delete(`/api/purchase-orders/${po.id}`);
  await loadPos();
}

onMounted(async () => {
  await Promise.all([loadPos(), loadVendors()]);
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

.totals-row {
  display: flex; gap: 2rem; justify-content: flex-end;
  margin-top: 1.5rem; padding: 1rem;
  background: var(--p-content-hover-background); border-radius: 8px;
}
.totals-row > div { display: flex; flex-direction: column; gap: 0.3rem; }
.total-display label { font-size: 0.72rem; text-transform: uppercase; }
.total-amount { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); }

.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); }
.empty-state h3 { margin: 1rem 0 0.5rem; color: var(--text-color); }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
