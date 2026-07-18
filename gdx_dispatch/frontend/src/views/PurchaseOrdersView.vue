<template>
    <section class="po-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Purchase Orders</h2>
        </template>
        <template #end>
          <Button icon="pi pi-download" label="Export" aria-label="Export CSV" text size="small" @click="exportRows" />
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
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" :rowsPerPageOptions="[10, 20, 50, 100]" striped-rows
        data-testid="pos-table" @row-click="openDetail($event.data)" >
        <template #empty>
          <EmptyState icon="pi pi-inbox" title="No Purchase Orders" message="Create a PO to order parts from a vendor." actionLabel="+ Create First PO" @action="openCreate" />
        </template>
        <Column field="po_number" header="PO #" sortable style="width:130px" />
        <Column field="vendor_name" header="Vendor" sortable />
        <Column field="order_date" header="Order Date" sortable style="width:130px">
          <template #body="{ data }">{{ data.order_date || '—' }}</template>
        </Column>
        <Column field="expected_date" header="Expected" sortable style="width:130px">
          <template #body="{ data }">{{ data.expected_date || '—' }}</template>
        </Column>
        <Column field="status" header="Status" sortable style="width:120px">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="total" header="Total" sortable style="width:120px">
          <template #body="{ data }">{{ formatCurrency(data.total || 0) }}</template>
        </Column>
        <Column header="Actions" style="width:130px">
          <template #body="{ data }">
            <Button v-if="data.status === 'sent'" icon="pi pi-check" aria-label="Receive" severity="success" text size="small"
              v-tooltip="'Receive'" @click.stop="receivePo(data)" />
            <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openDetail(data)" />
            <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small"
              v-if="data.status !== 'received'" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- Create/Edit Dialog -->
      <Dialog v-model:visible="showDialog" :header="editingPo ? `Edit ${editingPo.po_number}` : 'New Purchase Order'"
        modal :style="{width: '800px'}" :closable="!isDirty" :close-on-escape="!isDirty">
        <div class="form-grid">
          <div class="form-field">
            <label>Vendor *</label>
            <Select v-model="form.vendor_id" :options="vendors" optionLabel="name" optionValue="id"
              placeholder="Select vendor" filter showClear @change="onVendorSelect" class="w-full" />
          </div>
          <div class="form-field">
            <!-- Ties the PO to a job so receiving can see what door should
                 arrive. Optional — vendor-stock POs leave it blank. -->
            <label>For job</label>
            <Select v-model="form.job_id" :options="jobs" optionLabel="label" optionValue="id"
              placeholder="(optional) link a job" filter showClear class="w-full" data-testid="po-job-select" />
          </div>
          <FormField v-model="form.status" label="Status" as="select" :options="poStatusOptions" />
          <div class="form-field">
            <label>Order Date</label>
            <DatePicker v-model="form.order_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <div class="form-field">
            <label>Expected Date</label>
            <DatePicker v-model="form.expected_date" dateFormat="yy-mm-dd" class="w-full" />
          </div>
          <FormField v-model="form.notes" label="Notes" as="textarea" :rows="2" class="full-width" />
        </div>

        <!-- What door(s) this PO should bring in, from the linked job's captured
             spec — so whoever checks in the delivery knows what to expect and how
             heavy. Only shows for a job-linked PO with a captured door. -->
        <div v-if="detailReceiving.door_specs.length" class="doors-expected" data-testid="po-doors-expected">
          <h3>
            Doors expected
            <span v-if="detailReceiving.job_number" class="doors-ref">· {{ detailReceiving.job_number }}</span>
            <span v-if="detailReceiving.estimate_number" class="doors-ref">· {{ detailReceiving.estimate_number }}</span>
          </h3>
          <div v-for="(door, di) in detailReceiving.door_specs" :key="door.line_id || di" class="door-expected">
            <div class="door-expected-title">
              {{ door.identity.Model || door.label || 'Door' }}
              <span v-if="door.quantity > 1" class="door-expected-qty">×{{ door.quantity }}</span>
            </div>
            <dl class="door-expected-grid">
              <template v-for="(val, key) in { ...door.identity, ...door.receiving }" :key="key">
                <dt>{{ key }}</dt>
                <dd>{{ fmtVal(val) }}</dd>
              </template>
              <template v-if="door.window_count">
                <dt>Windows</dt>
                <dd>{{ door.window_count }} section(s) — check glass</dd>
              </template>
            </dl>
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
              {{ formatCurrency(Number(data.unit_cost || 0) * Number(data.quantity_ordered || 0)) }}
            </template>
          </Column>
          <Column style="width:50px">
            <template #body="{ index }">
              <Button v-tooltip="'Remove'" icon="pi pi-times" aria-label="Remove" text severity="danger" size="small" @click="removeLine(index)" />
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
            <div class="total-amount">{{ formatCurrency(calculatedTotal) }}</div>
          </div>
        </div>

        <template #footer>
          <Button label="Cancel" severity="secondary" @click="cancelDialog" />
          <Button :label="editingPo ? 'Save' : 'Create'" icon="pi pi-check" @click="savePo" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatMoney as formatCurrency } from "../composables/useFormatters";
import EmptyState from "../components/EmptyState.vue";
import FormField from "../components/FormField.vue";
import { useDirtyDialog } from "../composables/useDirtyDialog";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
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

useListPrefs(
  "purchase-orders",
  { statusFilter },
  { statusFilter: { default: "all", valid: (v) => ["all", "draft", "sent", "received", "cancelled"].includes(v) } },
);

// {label,value} shape for FormField's Select wrapper.
const poStatusOptions = ["draft", "sent"].map((s) => ({ label: s, value: s }));

const emptyLine = () => ({ description: "", sku: "", quantity_ordered: 1, unit_cost: 0 });
const emptyForm = () => ({
  vendor_id: null, vendor_name: "", job_id: null, status: "draft",
  order_date: new Date(), expected_date: null, notes: "",
  tax: 0, shipping: 0, lines: [emptyLine()],
});
const form = ref(emptyForm());

// Jobs for the "For job" picker, and the door receiving info for the PO
// currently open in the dialog (fetched on open — the list omits it).
const jobs = ref([]);
const detailReceiving = ref({ door_specs: [], job_number: null, estimate_number: null });

// A captured spec value is usually a string; Load Information arrives as an
// object. Render it readably rather than "[object Object]".
function fmtVal(val) {
  if (val == null) return "—";
  if (typeof val === "object") return Object.entries(val).map(([k, v]) => `${k}: ${v}`).join(", ");
  return String(val);
}

const { snapshot, isDirty, confirmDiscard } = useDirtyDialog(() => form.value);
const { exportCsv } = useTableExport();

function exportRows() {
  exportCsv(filtered.value, [
    { field: "po_number", header: "PO #" },
    { field: "vendor_name", header: "Vendor" },
    { field: "order_date", header: "Order Date" },
    { field: "expected_date", header: "Expected" },
    { field: "status", header: "Status" },
    { field: "total", header: "Total" },
  ], "purchase-orders");
}

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

async function loadJobs() {
  try {
    const data = await api.get("/api/jobs?per_page=500");
    const arr = Array.isArray(data) ? data : data?.jobs || data?.items || [];
    jobs.value = arr.map((j) => ({
      id: j.id,
      // What the operator recognizes it by: job number first, then title/customer.
      label: [j.job_number, j.title || j.customer_name || j.customer?.name].filter(Boolean).join(" · ") || j.id,
    }));
  } catch {
    jobs.value = [];
  }
}

function onVendorSelect() {
  const v = vendors.value.find((x) => x.id === form.value.vendor_id);
  if (v) form.value.vendor_name = v.name;
}

function openCreate() {
  editingPo.value = null;
  form.value = emptyForm();
  detailReceiving.value = { door_specs: [], job_number: null, estimate_number: null };
  snapshot();
  showDialog.value = true;
}

function cancelDialog() {
  if (confirmDiscard()) showDialog.value = false;
}

async function openDetail(po) {
  editingPo.value = po;
  form.value = {
    vendor_id: po.vendor_id, vendor_name: po.vendor_name, job_id: po.job_id, status: po.status,
    order_date: po.order_date ? new Date(po.order_date) : new Date(),
    expected_date: po.expected_date ? new Date(po.expected_date) : null,
    notes: po.notes, tax: po.tax, shipping: po.shipping,
    lines: po.lines?.length ? [...po.lines] : [emptyLine()],
  };
  // The list omits door specs; fetch the full PO so the receiver sees what's
  // expected. Best-effort — the dialog still works if this fails.
  detailReceiving.value = { door_specs: [], job_number: null, estimate_number: null };
  try {
    const full = await api.get(`/api/purchase-orders/${po.id}`);
    detailReceiving.value = {
      door_specs: full?.door_specs || [],
      job_number: full?.job_number || null,
      estimate_number: full?.estimate_number || null,
    };
  } catch { /* leave empty */ }
  snapshot();
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
  await Promise.all([loadPos(), loadVendors(), loadJobs()]);
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

/* "Doors expected" — themed via PrimeVue vars so it reads in light + dark. */
.doors-expected {
  margin-top: 1.25rem; padding: 0.85rem 1rem;
  border: 1px solid var(--p-content-border-color); border-radius: 8px;
  background: var(--p-content-hover-background);
}
.doors-expected h3 { margin: 0 0 0.6rem; font-size: 0.95rem; }
.doors-ref { font-weight: 500; color: var(--p-text-muted-color); font-size: 0.85rem; }
.door-expected + .door-expected { margin-top: 0.6rem; padding-top: 0.6rem; border-top: 1px solid var(--p-content-border-color); }
.door-expected-title { font-weight: 700; font-size: 0.9rem; color: var(--p-text-color); margin-bottom: 0.3rem; }
.door-expected-qty { font-weight: 600; color: var(--p-text-muted-color); }
.door-expected-grid { margin: 0; display: grid; grid-template-columns: minmax(6rem, auto) 1fr; gap: 0.12rem 0.75rem; }
.door-expected-grid dt { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.02em; color: var(--p-text-muted-color); }
.door-expected-grid dd { margin: 0; font-size: 0.88rem; color: var(--p-text-color); word-break: break-word; }

.totals-row {
  display: flex; gap: 2rem; justify-content: flex-end;
  margin-top: 1.5rem; padding: 1rem;
  background: var(--p-content-hover-background); border-radius: 8px;
}
.totals-row > div { display: flex; flex-direction: column; gap: 0.3rem; }
.total-display label { font-size: 0.72rem; text-transform: uppercase; }
.total-amount { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); }

.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
