<!--
  InvoiceCreateView — full-page invoice creation surface (S122).

  Replaces the 600px dialog that lived inside BillingView.vue. Renders the
  shared <LineItemEditor>, surfaces the per-invoice tax_rate (was hardcoded
  to 8.25% in the dialog), and pulls the parts-from-job checklist when a job
  is selected. POSTs `{customer_id, job_id, line_items, tax_rate, due_date,
  notes, from_part_ids}` — backend marks pulled parts as billed in the same
  transaction.

  tax_rate is ALWAYS sent as the number displayed in the field, including an
  explicit 0. Sending null instead of 0 makes the backend re-resolve the
  tenant default and put the tax back — the "tax keeps coming back" bug.
  The field seeds from /api/tax/resolve (customer-aware: 0 for exempt
  customers), not the raw tenant default.

  Mounted at /billing/new. The "+ New Invoice" button on /billing and the
  per-row "Create Invoice" button push here with optional ?job_id=&customer_id=.
-->
<template>
  <section class="invoice-create-view view-card">
    <header class="page-header">
      <Button
        icon="pi pi-arrow-left"
        label="Back to Billing"
        text
        size="small"
        data-testid="back-to-billing"
        @click="$router.push('/billing')"
      />
      <div class="title-row">
        <h2 class="page-title">{{ isCounterSale ? 'New Counter Sale' : 'New Invoice' }}</h2>
        <span v-if="isCounterSale" class="counter-badge" data-testid="counter-sale-badge">
          Counter Sale · No Job
        </span>
      </div>
    </header>

    <Card v-if="!loading">
      <template #content>
        <div class="form-grid">
          <div class="form-field">
            <label for="inv-customer">Customer *</label>
            <Select
              id="inv-customer"
              v-model="form.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select customer"
              filter
              showClear
              class="w-full"
              data-testid="invoice-customer-dropdown"
              @change="onCustomerChange"
            />
          </div>

          <div class="form-field" v-if="!counterMode">
            <label for="inv-job">Job <small class="muted">(optional)</small></label>
            <Select
              id="inv-job"
              v-model="form.job_id"
              :options="jobOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select job (or leave blank for counter sale)"
              filter
              showClear
              :disabled="!form.customer_id"
              class="w-full"
              data-testid="invoice-job-dropdown"
              @change="onJobChange"
            />
          </div>

          <div class="form-field">
            <label for="inv-date">Invoice Date</label>
            <DatePicker
              id="inv-date"
              v-model="form.invoice_date"
              dateFormat="yy-mm-dd"
              :showIcon="true"
              class="w-full"
              data-testid="invoice-date"
            />
          </div>

          <div class="form-field">
            <label for="inv-due">Due Date</label>
            <DatePicker
              id="inv-due"
              v-model="form.due_date"
              dateFormat="yy-mm-dd"
              :showIcon="true"
              class="w-full"
              data-testid="invoice-due-date"
            />
          </div>

          <div class="form-field">
            <label for="inv-tax-rate">Tax Rate (%)</label>
            <InputNumber
              id="inv-tax-rate"
              v-model="form.tax_rate_pct"
              suffix="%"
              :min="0"
              :max="100"
              :minFractionDigits="0"
              :maxFractionDigits="3"
              class="w-full"
              data-testid="invoice-tax-rate"
            />
            <small class="muted">Auto-filled for the selected customer; override per-invoice. 0% = no tax.</small>
          </div>

          <div class="form-field full-width">
            <label>Line Items</label>
            <LineItemEditor
              v-model:lines="form.line_items"
              v-model:fromPartIds="form.from_part_ids"
              :job-id="form.job_id || null"
              :categories="lineCategories"
              show-taxable
              show-cost
              show-margin
              data-testid="invoice-line-editor"
            />
          </div>

          <!-- PR3-billing-capture: approved change orders on this job that
               were never billed. Checked COs are stamped + copied to invoice
               lines server-side in the same transaction. -->
          <div class="form-field full-width" v-if="jobChangeOrders.length" data-testid="invoice-co-checklist">
            <label>Approved change orders on this job</label>
            <div v-for="co in jobChangeOrders" :key="co.id" class="co-checklist-row">
              <label class="co-checklist-label">
                <input
                  type="checkbox"
                  :value="co.id"
                  v-model="form.from_change_order_ids"
                  :data-testid="`invoice-co-${co.co_number}`"
                />
                <span class="co-number">{{ co.co_number }}</span>
                <span class="co-title">{{ co.title }}</span>
                <span class="co-amount">{{ currency(Number(co.amount) || 0) }}</span>
              </label>
            </div>
            <small class="muted" v-if="form.from_change_order_ids.length">
              {{ form.from_change_order_ids.length }} change order(s) —
              {{ currency(selectedChangeOrderTotal) }} + applicable tax will be
              added as invoice lines on create.
            </small>
          </div>

          <div class="form-field full-width">
            <label for="inv-notes">Notes</label>
            <Textarea
              id="inv-notes"
              v-model="form.notes"
              rows="3"
              class="w-full"
              data-testid="invoice-notes"
            />
          </div>
        </div>

        <Divider />
        <div class="totals">
          <div class="totals-row">
            <span>Subtotal</span>
            <span data-testid="invoice-subtotal">{{ currency(subtotal) }}</span>
          </div>
          <div class="totals-row" v-if="form.tax_rate_pct">
            <span>Tax ({{ form.tax_rate_pct }}%)</span>
            <span data-testid="invoice-tax">{{ currency(taxAmount) }}</span>
          </div>
          <div class="totals-row total">
            <span>Total</span>
            <span data-testid="invoice-total">{{ currency(total) }}</span>
          </div>
        </div>

        <div class="actions">
          <Button
            label="Cancel"
            severity="secondary"
            @click="$router.push('/billing')"
            data-testid="invoice-cancel"
          />
          <Button
            label="Create Invoice"
            icon="pi pi-check"
            :loading="creating"
            :disabled="!canCreate"
            data-testid="invoice-create-submit"
            @click="createInvoice"
          />
        </div>
      </template>
    </Card>

    <div v-else class="loading-spinner"><p>Loading…</p></div>
  </section>
</template>

<script setup>
import { computed, ref, onMounted, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Select from 'primevue/select';
import DatePicker from 'primevue/datepicker';
import InputNumber from 'primevue/inputnumber';
import Textarea from 'primevue/textarea';
import Divider from 'primevue/divider';
import { useToast } from 'primevue/usetoast';
import { useApi } from '../composables/useApi';
import { formatMoney as currency } from '../composables/useFormatters';
import LineItemEditor from '../components/LineItemEditor.vue';

const route = useRoute();
const router = useRouter();
const api = useApi();
const toast = useToast();

const loading = ref(true);
const creating = ref(false);

// S122-b — same category set EstimateView uses (EstimateView.vue:563), so
// the same Select options render on /billing/new and /estimates/new.
const lineCategories = [
  { label: 'Doors', value: 'Doors' },
  { label: 'Openers', value: 'Openers' },
  { label: 'Springs', value: 'Springs' },
  { label: 'Labor', value: 'Labor' },
  { label: 'Parts', value: 'Parts' },
  { label: 'Other', value: 'Other' },
];

const form = ref({
  customer_id: null,
  job_id: null,
  invoice_date: null,
  due_date: null,
  // Stored as a percent integer in the form (8.25), converted to decimal
  // (0.0825) at POST time. Form-friendly; backend-canonical.
  tax_rate_pct: 0,
  notes: '',
  line_items: [{
    description: '',
    quantity: 1,
    unit_price: 0,
    taxable: true,
    category: null,
    cost: null,
    margin_pct_override: null,
  }],
  from_part_ids: [],
  from_change_order_ids: [],
});

const customers = ref([]);
const jobs = ref([]);
// PR3-billing-capture — approved, never-billed change orders on the picked
// job. Selected COs are stamped + their lines copied SERVER-side in the same
// transaction (the stamp gates the copy), so they are not client-side rows.
const jobChangeOrders = ref([]);

async function loadJobChangeOrders(jobId) {
  jobChangeOrders.value = [];
  if (!jobId) return;
  try {
    jobChangeOrders.value = await api.get(
      `/api/change-orders?job_id=${encodeURIComponent(jobId)}&unbilled=true`,
    );
  } catch (_) {
    jobChangeOrders.value = [];
  }
}

const selectedChangeOrderTotal = computed(() =>
  jobChangeOrders.value
    .filter((co) => form.value.from_change_order_ids.includes(co.id))
    .reduce((sum, co) => sum + (Number(co.amount) || 0), 0),
);

const customerOptions = computed(() =>
  customers.value.map((c) => ({
    label: c.name + (c.phone ? ` · ${c.phone}` : ''),
    value: c.id,
  })),
);

const jobOptions = computed(() => {
  const cid = form.value.customer_id;
  return jobs.value
    .filter((j) => !cid || j.customer_id === cid)
    .map((j) => ({
      label: `${j.title || 'Job'} (${j.id.slice(0, 8)})`,
      value: j.id,
      customer_id: j.customer_id,
    }));
});

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

const subtotal = computed(() =>
  form.value.line_items.reduce((s, l) => s + toNum(l.quantity) * toNum(l.unit_price), 0),
);

const taxableSubtotal = computed(() =>
  form.value.line_items
    .filter((l) => l.taxable !== false)
    .reduce((s, l) => s + toNum(l.quantity) * toNum(l.unit_price), 0),
);

const taxAmount = computed(() =>
  Math.round(taxableSubtotal.value * (toNum(form.value.tax_rate_pct) / 100) * 100) / 100,
);

const total = computed(() => subtotal.value + taxAmount.value);

const canCreate = computed(() => {
  // Customer is the AR target — always required. Job is optional so
  // counter-sale (parts/over-the-counter) invoices can exist.
  if (!form.value.customer_id) return false;
  return form.value.line_items.some((l) => l.description && toNum(l.unit_price) > 0);
});

// `?counter=1` hides the Job picker entirely (entered via the Counter Sale
// shortcut). Otherwise the Job picker is visible-but-optional; either way,
// "no job selected" surfaces as a Counter Sale so users can see what they're
// creating before submit.
const counterMode = computed(() => String(route.query.counter || '') === '1');
const isCounterSale = computed(() => counterMode.value || !form.value.job_id);

async function loadCustomers() {
  try {
    // Customers endpoint uses `per_page`, not `page_size` — the wrong key
    // silently fell back to the default 50, hiding older customers from
    // the dropdown so the Ready-for-Billing pre-fill rendered blank.
    const r = await api.get('/api/customers?per_page=1000');
    const list = Array.isArray(r) ? r : r?.data || r?.items || [];
    customers.value = list;
  } catch (e) {
    customers.value = [];
  }
}

async function loadJobs() {
  try {
    const r = await api.get('/api/jobs?page_size=500');
    const list = Array.isArray(r) ? r : r?.data || r?.items || [];
    jobs.value = list;
  } catch (e) {
    jobs.value = [];
  }
}

async function ensureCustomerLoaded(customerId) {
  if (!customerId) return;
  if (customers.value.some((c) => c.id === customerId)) return;
  try {
    const c = await api.get(`/api/customers/${customerId}`, { suppressErrorToast: true });
    if (c && c.id) customers.value = [c, ...customers.value];
  } catch (e) {
    // best-effort — dropdown will stay blank but the v-model UUID still posts
  }
}

async function ensureJobLoaded(jobId) {
  if (!jobId) return;
  if (jobs.value.some((j) => j.id === jobId)) return;
  try {
    const j = await api.get(`/api/jobs/${jobId}`, { suppressErrorToast: true });
    if (j && j.id) jobs.value = [j, ...jobs.value];
  } catch (e) {
    // best-effort
  }
}

// Guards against out-of-order responses when the customer changes quickly.
let taxResolveSeq = 0;
async function resolveTaxRate() {
  const seq = ++taxResolveSeq;
  try {
    const cid = form.value.customer_id;
    // Customer-aware: returns 0 for tax-exempt customers, else the tenant
    // default. The field stays editable — this only seeds it.
    const url = cid
      ? `/api/tax/resolve?customer_id=${encodeURIComponent(cid)}`
      : '/api/tax/resolve';
    const r = await api.get(url, { suppressErrorToast: true });
    const rate = Number(r?.rate);
    if (seq === taxResolveSeq && Number.isFinite(rate) && rate >= 0) {
      // Backend stores as a decimal fraction (0.0825); the form is in %.
      form.value.tax_rate_pct = Math.round(rate * 10000) / 100;
    }
  } catch (e) {
    // tax resolve optional — leave the field as-is
  }
}

function onCustomerChange() {
  // Clear job if it no longer belongs to the new customer.
  if (form.value.job_id) {
    const j = jobs.value.find((row) => row.id === form.value.job_id);
    if (j && form.value.customer_id && j.customer_id !== form.value.customer_id) {
      form.value.job_id = null;
    }
  }
  resolveTaxRate();
}

function onJobChange() {
  // When job changes, derive customer if the picker is empty.
  if (form.value.job_id && !form.value.customer_id) {
    const j = jobs.value.find((row) => row.id === form.value.job_id);
    if (j) form.value.customer_id = j.customer_id;
  }
  // Reset parts-pull tracking — different job means different parts.
  form.value.from_part_ids = [];
  // PR3 — same for change orders; reload the job's unbilled CO checklist.
  form.value.from_change_order_ids = [];
  loadJobChangeOrders(form.value.job_id);
  prefillFromJobEstimate(form.value.job_id);
}

async function prefillFromJobEstimate(jobId) {
  if (!jobId) return;
  try {
    const list = await api.get(
      `/api/estimates?job_id=${encodeURIComponent(jobId)}`,
      { suppressErrorToast: true },
    );
    const estimates = Array.isArray(list) ? list : Array.isArray(list?.data) ? list.data : [];
    if (!estimates.length) return;
    const latest = estimates[0];
    const detail = await api.get(`/api/estimates/${latest.id}`, { suppressErrorToast: true });
    const est = detail?.data || detail || {};
    const lines = Array.isArray(est.lines) ? est.lines : [];
    if (!lines.length) return;
    form.value.line_items = lines.map((ln) => ({
      description: ln.description || '',
      quantity: Number(ln.quantity || 1) || 1,
      unit_price: Number(ln.unit_price || 0),
      taxable: ln.category && ln.category.toLowerCase() === 'labor' ? false : true,
      // S122-b — forward estimate-parity fields when present on the estimate
      // line (category select, cost snapshot, margin override).
      category: ln.category || null,
      cost: ln.cost_snapshot != null ? Number(ln.cost_snapshot) : null,
      margin_pct_override:
        ln.margin_pct_override != null ? Number(ln.margin_pct_override) * 100 : null,
    }));
    if (!form.value.notes) form.value.notes = est.description || est.notes || '';
  } catch (e) {
    // estimate prefill is best-effort
  }
}

async function createInvoice() {
  creating.value = true;
  try {
    const lineItems = form.value.line_items
      .filter((l) => l.description && toNum(l.unit_price) > 0)
      .map((l) => {
        const out = {
          description: l.description,
          quantity: toNum(l.quantity) > 0 ? Number(l.quantity) : 1,
          unit_price: toNum(l.unit_price),
          taxable: l.taxable !== false,
        };
        // S122-b — forward estimate-parity fields only when set, so the
        // contract's `extra="forbid"` validators don't choke on nulls.
        if (l.category) out.category = l.category;
        if (l.cost != null && toNum(l.cost) > 0) out.cost = toNum(l.cost);
        if (l.margin_pct_override != null && toNum(l.margin_pct_override) > 0) {
          // Form is in percent (e.g. 35), backend expects decimal (0.35).
          out.margin_pct_override = toNum(l.margin_pct_override) / 100;
        }
        // D-S122-line-removal-unbill — forward the part_id so the backend
        // can store the linkage and release the part on line-delete.
        if (l.part_id) out.part_id = l.part_id;
        return out;
      });

    // Send exactly what the field shows, INCLUDING 0 — the server honors an
    // explicit 0 as "exempt sale", whereas null makes it re-resolve the
    // tenant default and re-apply tax the user just removed.
    const taxRateDecimal = toNum(form.value.tax_rate_pct) / 100;

    const payload = {
      customer_id: form.value.customer_id,
      job_id: form.value.job_id,
      invoice_date: form.value.invoice_date instanceof Date
        ? form.value.invoice_date.toISOString().slice(0, 10)
        : form.value.invoice_date || null,
      due_date: form.value.due_date instanceof Date
        ? form.value.due_date.toISOString().slice(0, 10)
        : form.value.due_date || null,
      notes: form.value.notes || null,
      line_items: lineItems,
      tax_rate: Number.isFinite(taxRateDecimal) ? taxRateDecimal : 0,
      from_part_ids: form.value.from_part_ids || [],
      from_change_order_ids: form.value.from_change_order_ids || [],
    };

    let created;
    try {
      created = await api.post('/api/invoices', payload);
    } catch (e) {
      // 2026-07-23 double-billing guard: now that invoicing isn't gated on
      // job completion, the backend 409s when the job already has a real
      // invoice. Confirm and re-submit with force — deliberate second
      // invoices (progress billing, re-bill) stay one click away.
      if (e.status === 409 && /already billed/i.test(e.message || '')) {
        if (!window.confirm(`${e.message}\n\nCreate another invoice for this job anyway?`)) {
          return;
        }
        created = await api.post('/api/invoices', { ...payload, force: true });
      } else {
        throw e;
      }
    }
    // PR1-billing-capture: surface zero-price policy warnings — the server
    // emits these in F-75 warn-mode, but nothing rendered them before.
    if (Array.isArray(created.warnings) && created.warnings.length) {
      toast.add({
        severity: 'warn',
        summary: 'Review pricing',
        detail: created.warnings.join('; '),
        life: 8000,
      });
    }
    // Deposit netting (2026-07-23): tell the operator what came off the
    // bill and whether anything is left over for a human to resolve.
    const net = created.deposit_netting;
    if (net && (net.deposit_paid_applied > 0 || (net.superseded || []).length || (net.voided || []).length)) {
      const bits = [];
      if (net.deposit_paid_applied > 0) bits.push(`$${net.deposit_paid_applied.toFixed(2)} deposit applied`);
      if ((net.superseded || []).length) bits.push(`superseded ${net.superseded.join(', ')}`);
      if ((net.voided || []).length) bits.push(`voided unpaid ${net.voided.join(', ')}`);
      if (net.deposit_unapplied > 0) bits.push(`$${net.deposit_unapplied.toFixed(2)} deposit UNAPPLIED — resolve manually`);
      toast.add({
        severity: net.deposit_unapplied > 0 ? 'warn' : 'info',
        summary: 'Deposit netted',
        detail: bits.join(' · '),
        life: 8000,
      });
    }
    toast.add({
      severity: 'success',
      summary: 'Invoice created',
      detail: created.invoice_number || '',
      life: 3000,
    });
    router.push(`/billing/${created.id}`);
  } catch (e) {
    toast.add({
      severity: 'error',
      summary: 'Error',
      detail: e.message || 'Failed to create invoice',
      life: 5000,
    });
  } finally {
    creating.value = false;
  }
}

onMounted(async () => {
  loading.value = true;
  await Promise.all([loadCustomers(), loadJobs(), resolveTaxRate()]);
  // Apply ?job_id= / ?customer_id= from query (BillingView's pre-fill path).
  const q = route.query || {};
  const qJobId = q.job_id ? String(q.job_id) : '';
  const qCustomerId = q.customer_id ? String(q.customer_id) : '';
  // Guarantee the job is in the local list before deriving its customer_id —
  // older jobs paginate out of the bulk `/api/jobs?page_size=500` response.
  if (qJobId) await ensureJobLoaded(qJobId);
  if (qCustomerId) form.value.customer_id = qCustomerId;
  if (qJobId) {
    form.value.job_id = qJobId;
    if (!form.value.customer_id) {
      const j = jobs.value.find((row) => row.id === qJobId);
      if (j) form.value.customer_id = j.customer_id;
    }
    await prefillFromJobEstimate(qJobId);
  }
  // Final guarantee — if a customer_id is selected but its option isn't in
  // the bulk-loaded list, fetch it by ID so the dropdown can render the name.
  await ensureCustomerLoaded(form.value.customer_id);
  loading.value = false;
});

watch(() => form.value.customer_id, () => onCustomerChange());
</script>

<style scoped>
.invoice-create-view {
  padding: 1rem;
}
.page-header {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin-bottom: 1rem;
}
.title-row {
  display: flex;
  align-items: center;
  gap: 1rem;
}
.page-title {
  margin: 0;
}
.counter-badge {
  display: inline-flex;
  align-items: center;
  padding: 0.15rem 0.5rem;
  background: var(--p-primary-100, #e0f2fe);
  color: var(--p-primary-700, #0369a1);
  border-radius: 999px;
  font-size: 0.8em;
  font-weight: 600;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.w-full {
  width: 100%;
}
.muted {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.85em;
}
.totals {
  margin-left: auto;
  max-width: 320px;
}
.totals-row {
  display: flex;
  justify-content: space-between;
  padding: 0.25rem 0;
}
.totals-row.total {
  font-weight: 700;
  font-size: 1.1em;
  border-top: 2px solid var(--p-content-border-color, #ddd);
  padding-top: 0.5rem;
  margin-top: 0.25rem;
}
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1rem;
}
.co-checklist-row {
  padding: 0.25rem 0;
}
.co-checklist-label {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  cursor: pointer;
}
.co-number {
  font-family: monospace;
  color: var(--p-text-muted-color, #6b7280);
}
.co-title {
  flex: 1;
}
.co-amount {
  font-weight: 600;
}
</style>
