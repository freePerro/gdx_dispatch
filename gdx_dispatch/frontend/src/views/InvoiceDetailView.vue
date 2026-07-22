<template>
    <section class="invoice-detail view-card">
      <div v-if="loading" class="loading-spinner"><p>Loading invoice...</p></div>
      <template v-else>
        <!-- Header -->
        <header class="detail-header">
          <div>
            <Button
              icon="pi pi-arrow-left"
              label="Back to Billing"
              text
              size="small"
              @click="$router.push('/billing')"
              data-testid="back-to-billing"
            />
            <h2 data-testid="invoice-number">{{ invoice.invoice_number }}</h2>
            <p data-testid="invoice-customer" class="customer-name">
              <router-link v-if="invoice.customer_id" :to="`/customers/${invoice.customer_id}`" class="link">
                {{ invoice.customer_name }}
              </router-link>
              <span v-else>{{ invoice.customer_name }}</span>
              <router-link v-if="invoice.job_id" :to="`/jobs/${invoice.job_id}`" class="link" style="margin-left:1rem; font-size:0.85rem;">
                <i class="pi pi-briefcase" /> View Job
              </router-link>
            </p>
          </div>
          <div class="header-meta">
            <Tag
              :value="invoice.status"
              :severity="statusSeverity(invoice.status)"
              data-testid="invoice-status"
            />
            <p>Due: <strong>{{ formatDate(invoice.due_date) }}</strong></p>
            <p>Created: {{ formatDate(invoice.created_at) }}</p>
          </div>
        </header>

        <!-- Bill To panel — surfaces customer contact on the invoice so the
             office can email/call without bouncing through /customers/<id>.
             "Edit" opens the shared CustomerFormDialog. -->
        <section class="bill-to-card" data-testid="invoice-bill-to">
          <div class="bill-to-header">
            <h3>Bill To</h3>
            <Button
              v-if="invoice.customer_id"
              label="Edit Customer"
              icon="pi pi-pencil"
              size="small"
              text
              data-testid="invoice-edit-customer-btn"
              @click="openCustomerEdit"
            />
          </div>
          <div class="bill-to-grid">
            <div class="bill-to-row" data-testid="bill-to-name">
              <i class="pi pi-user" />
              <span v-if="invoice.customer_name">{{ invoice.customer_name }}</span>
              <span v-else class="muted">Unknown customer</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-email">
              <i class="pi pi-envelope" />
              <a v-if="invoice.customer_email" :href="`mailto:${invoice.customer_email}`">{{ invoice.customer_email }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-email" @click.prevent="openCustomerEdit">+ Add email</a>
              <span v-else class="muted">—</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-phone">
              <i class="pi pi-phone" />
              <a v-if="invoice.customer_phone" :href="`tel:${invoice.customer_phone}`">{{ formatPhone(invoice.customer_phone) }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-phone" @click.prevent="openCustomerEdit">+ Add phone</a>
              <span v-else class="muted">—</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-address">
              <i class="pi pi-map-marker" />
              <a v-if="invoice.customer_address" :href="`https://maps.google.com/?q=${encodeURIComponent(invoice.customer_address)}`" target="_blank" rel="noopener">{{ invoice.customer_address }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-address" @click.prevent="openCustomerEdit">+ Add address</a>
              <span v-else class="muted">—</span>
            </div>
          </div>
        </section>

        <Divider />

        <!-- Line Items -->
        <div class="lines-header">
          <h3>Line Items</h3>
          <div v-if="canEdit" class="lines-header-actions">
            <Button
              v-if="!editing"
              label="Edit"
              icon="pi pi-pencil"
              size="small"
              outlined
              data-testid="invoice-edit-btn"
              @click="enterEditMode"
            />
            <template v-else>
              <!-- Add Line lives inside LineItemEditor — the previous
                   duplicate "Add Line" here was removed when this view
                   was switched to the shared editor (2026-05-12). -->
              <Button
                label="Cancel"
                size="small"
                severity="secondary"
                outlined
                data-testid="invoice-edit-cancel"
                @click="cancelEdit"
              />
              <Button
                label="Save Changes"
                icon="pi pi-check"
                size="small"
                :loading="savingEdit"
                data-testid="invoice-edit-save"
                @click="saveEdit"
              />
            </template>
          </div>
        </div>
        <!-- Read-only line table (default) -->
        <DataTable
      responsiveLayout="scroll"
          v-if="!editing"
          :value="invoice.line_items"
          dataKey="id"
          data-testid="invoice-line-items"
          class="mb-1"
        >
          <template #empty>No line items.</template>
          <!-- D-S122b-detail-view-columns: render category/cost/margin fields
               so detail page round-trips what /billing/new captures.
               Column order matches LineItemEditor (Category / Description /
               Qty / Cost / Unit Price / Taxable / Margin / Total). -->
          <Column field="category" header="Category" style="width: 110px">
            <template #body="{ data }">
              <span v-if="data.category">{{ data.category }}</span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column field="description" header="Description" />
          <Column field="quantity" header="Qty" style="width: 80px" />
          <Column header="Cost" style="width: 100px">
            <template #body="{ data }">
              <span v-if="data.cost_snapshot != null">{{ currency(data.cost_snapshot) }}</span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column field="unit_price" header="Unit Price" style="width: 120px">
            <template #body="{ data }">{{ currency(data.unit_price) }}</template>
          </Column>
          <Column header="Taxable" style="width: 90px; text-align: center">
            <template #body="{ data }">
              <i v-if="data.taxable" class="pi pi-check" style="color: var(--p-success-500)" />
              <span v-else style="opacity: 0.5">—</span>
            </template>
          </Column>
          <Column header="Margin" style="width: 90px">
            <template #body="{ data }">
              <span v-if="data.margin_pct_override != null">{{ formatPercent(data.margin_pct_override) }}</span>
              <span v-else-if="data.margin_pct_snapshot != null">{{ formatPercent(data.margin_pct_snapshot) }}<small class="muted"> tier</small></span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column header="Total" style="width: 120px; text-align: right">
            <template #body="{ data }">{{ currency(lineTotal(data)) }}</template>
          </Column>
        </DataTable>
        <!-- Editable line items — shared LineItemEditor component (parity
             with /billing/new and /estimates/new). 2026-05-12: replaced the
             inline DataTable because it lacked the tier-aware recompute that
             EstimateView/InvoiceCreateView/LineItemEditor all share — typing
             a cost in edit mode never auto-filled unit_price. The shared
             editor also brings the Add-from-Catalog + parts-from-job panels
             into edit mode so a dispatcher can pull more parts onto a
             still-draft invoice without leaving the page. -->
        <LineItemEditor
          v-else
          v-model:lines="editLines"
          :categories="lineCategoryOptions"
          :job-id="invoice.job_id || null"
          show-taxable
          show-cost
          show-margin
          data-testid="invoice-edit-line-items"
        />
        <!-- Editable tax rate + dates + notes when in edit mode -->
        <div v-if="editing" class="edit-meta-grid">
          <div class="edit-field">
            <label>Tax Rate (%)</label>
            <InputNumber
              v-model="editTaxRatePct"
              :min="0"
              :max="100"
              :minFractionDigits="2"
              :maxFractionDigits="4"
              suffix=" %"
              data-testid="invoice-edit-tax-rate"
            />
            <small class="hint">
              Tenant default: {{ formatPercent(tenantDefaultRatePct, { digits: 2, whole: true }) }}.
              Leave 0 if every line is non-taxable.
            </small>
          </div>
          <div class="edit-field">
            <label>Invoice Date</label>
            <InputText v-model="editInvoiceDate" type="date" />
          </div>
          <div class="edit-field">
            <label>Due Date</label>
            <InputText v-model="editDueDate" type="date" />
          </div>
          <div class="edit-field" style="grid-column: 1 / -1">
            <label>Notes</label>
            <InputText v-model="editNotes" class="w-full" />
          </div>
          <div class="edit-field" style="grid-column: 1 / -1; display:flex; align-items:center; gap:0.6rem;">
            <ToggleSwitch v-model="editHideLinePrices" inputId="inv-hide-line-prices" data-testid="invoice-hide-line-prices" />
            <label for="inv-hide-line-prices" style="margin:0">Hide line-item prices on PDF</label>
          </div>
        </div>

        <!-- Totals. In edit mode the breakdown is computed live from the
             editable lines + rate so the dispatcher can see what the next
             save will produce. Read mode uses the server-stored numbers. -->
        <div class="totals-section" data-testid="invoice-totals">
          <template v-if="editing">
            <div class="total-row">
              <span>Subtotal</span>
              <strong>{{ currency(editSubtotal) }}</strong>
            </div>
            <div class="total-row">
              <span>Taxable Subtotal</span>
              <strong>{{ currency(editTaxableSubtotal) }}</strong>
            </div>
            <div class="total-row">
              <span>Tax ({{ formatPercent(editTaxRatePct, { digits: 2, whole: true }) }})</span>
              <strong>{{ currency(editTax) }}</strong>
            </div>
            <div class="total-row grand">
              <span>Preview Total</span>
              <strong>{{ currency(editTotal) }}</strong>
            </div>
          </template>
          <template v-else>
            <template v-if="invoice.line_items && invoice.line_items.length">
              <div class="total-row">
                <span>Subtotal</span>
                <strong>{{ currency(invoice.subtotal || subtotal) }}</strong>
              </div>
              <div v-if="invoice.taxable_subtotal !== undefined && invoice.taxable_subtotal !== invoice.subtotal" class="total-row">
                <span>Taxable Subtotal</span>
                <strong>{{ currency(invoice.taxable_subtotal) }}</strong>
              </div>
              <div class="total-row">
                <span>Tax<template v-if="invoice.tax_rate != null"> ({{ formatPercent(invoice.tax_rate, { digits: 2 }) }})</template></span>
                <strong>{{ currency(invoice.tax_amount) }}</strong>
              </div>
            </template>
            <div class="total-row grand">
              <span>Total</span>
              <strong>{{ currency(invoice.total) }}</strong>
            </div>
            <!-- Paid + Balance Due reflect the SAVED invoice — hide them
                 in edit mode so they don't argue with Preview Total above
                 (which is the in-progress projection). 2026-05-12 audit. -->
            <div class="total-row paid" v-if="totalPaid > 0">
              <span>Paid</span>
              <strong>{{ currency(totalPaid) }}</strong>
            </div>
            <div class="total-row balance">
              <span>{{ balanceDue < 0 ? 'Overpaid by' : 'Balance Due' }}</span>
              <strong :class="{ 'overpaid': balanceDue < 0 }">{{ currency(Math.abs(balanceDue)) }}</strong>
            </div>
          </template>
        </div>

        <Divider />

        <!-- Action Buttons — hidden in edit mode (2026-05-12 audit). The
             Send / Record Payment / Push QB / Delete actions all operate
             on the SAVED invoice, so surfacing them mid-edit lets a user
             "Send" an invoice whose draft edits haven't been committed yet
             — confusing at best, error-prone at worst. -->
        <div v-if="!editing" class="actions" data-testid="invoice-actions">
          <!-- Re-send is allowed on sent/overdue (2026-07-20): the composer
               already gates on an explicit click, and the concrete need is
               re-sending an invoice whose first email went out without the
               PDF. Only paid/void stay locked. -->
          <Button
            :label="['sent','overdue'].includes(String(invoice.status || '').toLowerCase()) ? 'Re-send Invoice' : 'Send Invoice'"
            icon="pi pi-send"
            data-testid="send-invoice-btn"
            :disabled="['paid','void'].includes(String(invoice.status || '').toLowerCase())"
            @click="sendInvoice"
          />
          <Button
            label="Record Payment"
            icon="pi pi-dollar"
            severity="success"
            data-testid="record-payment-btn"
            :disabled="String(invoice.status || '').toLowerCase() === 'paid'"
            @click="showPaymentDialog = true"
          />
          <Button
            label="Download PDF"
            icon="pi pi-file-pdf"
            severity="secondary"
            data-testid="download-pdf-btn"
            @click="downloadPdf"
          />
          <Button
            v-if="qbConnected"
            label="Push to QuickBooks"
            icon="pi pi-cloud-upload"
            severity="info"
            outlined
            :loading="pushingToQb"
            data-testid="push-qb-btn"
            @click="pushToQuickbooks"
          />
          <!-- PR6-billing-capture: per-invoice dunning mute for payment
               arrangements — manual reminder logs never pause the robot. -->
          <Button
            v-if="['sent','overdue'].includes(String(invoice.status || '').toLowerCase())"
            :label="invoice.dunning_paused ? 'Resume reminders' : 'Pause reminders'"
            :icon="invoice.dunning_paused ? 'pi pi-play' : 'pi pi-pause'"
            severity="warn"
            outlined
            data-testid="dunning-pause-btn"
            @click="toggleDunningPause"
          />
          <Button
            label="Delete"
            icon="pi pi-trash" aria-label="Delete"
            severity="danger"
            outlined
            data-testid="delete-invoice-btn"
            @click="confirmDelete"
          />
        </div>

        <Divider v-if="!editing" />

        <!-- Payment History -->
        <h3>Payment History</h3>
        <DataTable
      responsiveLayout="scroll" :value="invoice.payments" dataKey="id" data-testid="payment-history-table">
          <template #empty>No payments recorded.</template>
          <Column field="date" header="Date">
            <template #body="{ data }">{{ formatDate(data.date) }}</template>
          </Column>
          <Column field="method" header="Method" />
          <Column field="reference" header="Reference" />
          <Column field="amount" header="Amount" style="text-align: right">
            <template #body="{ data }">{{ currency(data.amount) }}</template>
          </Column>
        </DataTable>

        <!-- Notes -->
        <div v-if="invoice.notes" class="notes-section">
          <h3>Notes</h3>
          <p data-testid="invoice-notes">{{ invoice.notes }}</p>
        </div>
      </template>

      <!-- Email composer (Outlook-backed; mailto fallback when not connected) -->
      <Dialog v-model:visible="showComposer" header="Email invoice" modal
        :style="{ width: '720px' }" data-testid="invoice-composer">
        <div v-if="composerLoading" class="composer-loading">Building email…</div>
        <div v-else class="composer-form">
          <div class="form-field">
            <label>To</label>
            <InputText v-model="composer.to" placeholder="customer@example.com"
              class="w-full" data-testid="composer-to" />
          </div>
          <div class="form-field">
            <label>Subject</label>
            <InputText v-model="composer.subject" class="w-full" data-testid="composer-subject" />
          </div>
          <div class="form-field">
            <label>Message</label>
            <Textarea v-model="composer.body_text" rows="8" class="w-full" data-testid="composer-body" />
            <small class="muted">Plain text — line breaks are preserved.</small>
          </div>
          <div class="form-field">
            <label>Attachments</label>
            <div class="composer-attachments">
              <label class="composer-att-row">
                <input type="checkbox" :checked="true" disabled />
                <i class="pi pi-file-pdf" />
                <span>{{ composer.pdf?.name }}</span>
                <small class="muted">{{ formatBytes(composer.pdf?.size_bytes) }} · auto-attached</small>
              </label>
              <label v-for="att in composer.extras" :key="att.id" class="composer-att-row">
                <input type="checkbox" v-model="att._include" />
                <i :class="att.content_type?.startsWith('image/') ? 'pi pi-image' : 'pi pi-file'" />
                <span>{{ att.name }}</span>
                <small class="muted">{{ formatBytes(att.file_size) }}</small>
              </label>
            </div>
            <ComposerPdfPreview :pdf="composer.pdf" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showComposer = false" data-testid="composer-cancel" />
          <Button label="Send via Outlook" icon="pi pi-send" severity="primary"
            :loading="composerSending" :disabled="composerLoading || !composer.to"
            data-testid="composer-send" @click="sendComposer" />
        </template>
      </Dialog>

      <!-- Record Payment Dialog -->
      <Dialog
        v-model:visible="showPaymentDialog"
        header="Record Payment"
        modal
        :style="{ width: '480px' }"
        data-testid="record-payment-dialog"
      >
        <div class="form-grid-single">
          <div class="form-field">
            <label for="pay-amount">Amount *</label>
            <InputNumber
              id="pay-amount"
              v-model="newPayment.amount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              :max="balanceDue > 0 ? balanceDue : undefined"
              data-testid="payment-amount"
            />
            <small v-if="balanceDue > 0" class="form-hint">Balance due: {{ currency(balanceDue) }}</small>
          </div>
          <div class="form-field">
            <label for="pay-method">Payment Method *</label>
            <Select
              id="pay-method"
              v-model="newPayment.method"
              :options="paymentMethods"
              data-testid="payment-method"
            />
          </div>
          <div class="form-field">
            <label for="pay-ref">Reference #</label>
            <InputText
              id="pay-ref"
              v-model="newPayment.reference"
              placeholder="Check #, confirmation..."
              data-testid="payment-reference"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showPaymentDialog = false" />
          <Button
            label="Save Payment"
            data-testid="save-payment"
            :disabled="!newPayment.amount || !newPayment.method"
            :loading="savingPayment"
            @click="recordPayment"
          />
        </template>
      </Dialog>

      <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 already mounts
           one globally, and PrimeVue's useConfirm() broadcasts to every
           mounted instance, causing duplicate dialog renders. -->

      <CustomerFormDialog
        v-model:visible="showCustomerEditDialog"
        mode="edit"
        :customer="customerForEdit"
        @saved="onCustomerSaved"
      />

      <Toast data-testid="invoice-detail-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { formatDate, formatMoney, formatPercent, formatPhone } from "../composables/useFormatters";
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import { openAuthedFile, createAuthedBlobUrl } from "../composables/useAuthedFile";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Divider from "primevue/divider";
import Select from "primevue/select";
import ToggleSwitch from "primevue/toggleswitch";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toast from "primevue/toast";
import LineItemEditor from "../components/LineItemEditor.vue";
import CustomerFormDialog from "../components/CustomerFormDialog.vue";
import ComposerPdfPreview from "../components/ComposerPdfPreview.vue";

const api = useApi();
const route = useRoute();
const router = useRouter();
const { confirmDestructive } = useDestructiveConfirm();
const toast = useToast();

const loading = ref(true);
const savingPayment = ref(false);
const showPaymentDialog = ref(false);
const qbConnected = ref(false);
const pushingToQb = ref(false);

// Email composer (mirrors EstimateView). Server preps {to,subject,body_text,
// pdf{base64}} via /api/invoices/{id}/email-compose. User reviews + edits.
// Send routes through /api/outlook/send (PDF auto-attached). 409 falls back
// to mailto + downloading the PDF locally so the user can drag-attach.
const showComposer = ref(false);
const composerLoading = ref(false);
const composerSending = ref(false);
const composer = ref({ to: "", subject: "", body_text: "", pdf: null, extras: [] });
const paymentMethods = ["Cash", "Check", "Card", "Zelle", "Venmo", "ACH", "Other"];
const newPayment = ref({ amount: 0, method: "Cash", reference: "" });
// D-S122b-detail-view-columns — same category set as InvoiceCreateView.
const lineCategoryOptions = [
  { label: "Doors", value: "Doors" },
  { label: "Openers", value: "Openers" },
  { label: "Springs", value: "Springs" },
  { label: "Labor", value: "Labor" },
  { label: "Parts", value: "Parts" },
  { label: "Other", value: "Other" },
];
// Tenant-configured default rate (decimal fraction, e.g. 0.0738 == 7.38%).
// Loaded once from /api/tax/config in fetchInvoice; used as the seed value
// when entering edit mode on a legacy invoice that has no rate of its own.
const taxRate = ref(0.0);

// --- Edit-mode state. Only meaningful while editing=true. ---
const editing = ref(false);
const savingEdit = ref(false);
const editLines = ref([]);          // {_key, id?, description, quantity, unit_price, taxable}
const editTaxRatePct = ref(0);      // displayed as percent (e.g., 7.38), not decimal
const editInvoiceDate = ref("");    // ISO yyyy-mm-dd
const editDueDate = ref("");
const editNotes = ref("");
const editHideLinePrices = ref(false);
const tenantDefaultRatePct = computed(() => taxRate.value * 100);

const invoice = ref({
  id: null,
  invoice_number: "",
  customer_id: null,
  customer_name: "",
  customer_email: "",
  customer_phone: "",
  customer_address: "",
  status: "Draft",
  total: 0,
  due_date: "",
  created_at: "",
  notes: "",
  line_items: [],
  payments: [],
});

// Customer-edit dialog state. customerForEdit holds the full customer record
// (loaded just-in-time when the user clicks Edit) so the dialog can preserve
// fields the invoice payload doesn't carry (notes, access_notes, etc.).
const showCustomerEditDialog = ref(false);
const customerForEdit = ref(null);

// --- Computed ---
const subtotal = computed(() =>
  invoice.value.line_items.reduce((sum, item) => sum + lineTotal(item), 0)
);
const tax = computed(() => subtotal.value * taxRate.value);
const totalPaid = computed(() =>
  invoice.value.payments.reduce((sum, p) => sum + toNum(p.amount), 0)
);
const balanceDue = computed(() => toNum(invoice.value.total) - totalPaid.value);

// Edit mode is gated on draft status — once an invoice is sent or paid,
// the source-of-truth is whatever the customer received.
const canEdit = computed(() => {
  const s = String(invoice.value.status || "").toLowerCase();
  return s === "draft";
});

const editSubtotal = computed(() =>
  editLines.value.reduce((sum, ln) => sum + lineTotal(ln), 0),
);
const editTaxableSubtotal = computed(() =>
  editLines.value.reduce((sum, ln) => sum + (ln.taxable ? lineTotal(ln) : 0), 0),
);
const editTax = computed(() => editTaxableSubtotal.value * (toNum(editTaxRatePct.value) / 100));
const editTotal = computed(() => editSubtotal.value + editTax.value);

// --- Helpers ---
function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function lineTotal(item) {
  return toNum(item.quantity) * toNum(item.unit_price);
}

function currency(value) {
  return formatMoney(toNum(value));
}

function statusSeverity(status) {
  const map = { Draft: "secondary", Sent: "info", Paid: "success", Overdue: "danger", Partial: "warn" };
  return map[status] || "secondary";
}

function normalizeInvoice(payload) {
  // Backend `_serialize_invoice` puts the line array under `lines`. Pre-fix
  // this only checked `line_items`/`lineItems`/`items` so the InvoiceDetail
  // page rendered "No line items" for every QB-imported invoice. Same shape
  // bug as EstimateDetailView (Apr 30 2026 walk-through).
  const lineItems = (payload.lines || payload.line_items || payload.lineItems || payload.items || []).map((item, i) => ({
    id: item.id ?? `line-${i}`,
    description: item.description || "",
    quantity: toNum(item.quantity ?? 1),
    unit_price: toNum(item.unit_price ?? item.unitPrice ?? item.amount ?? 0),
    // Default true when the server didn't tell us — matches the column's
    // server_default. Only legacy QB-imported lines might come back without
    // an explicit value and historically those were treated as taxable.
    taxable: item.taxable === undefined ? true : Boolean(item.taxable),
    // S122-b detail-view parity — the DataTable columns at lines 91-127 read
    // these fields. Pre-fix the normalizer dropped them, so the columns fell
    // through to "—" even though the DB had real values. Forward as-is —
    // currency/percent formatting happens in the template.
    category: item.category ?? null,
    cost_snapshot: item.cost_snapshot ?? null,
    margin_pct_snapshot: item.margin_pct_snapshot ?? null,
    margin_pct_override: item.margin_pct_override ?? null,
    part_id: item.part_id ?? null,
  }));

  const payments = (payload.payments || payload.payment_history || []).map((p, i) => ({
    id: p.id ?? `pay-${i}`,
    amount: toNum(p.amount),
    method: p.method || "Cash",
    reference: p.reference || p.notes || "",
    date: p.date || p.paid_at || p.created_at || "",
  }));

  const computedTotal = lineItems.reduce((s, li) => s + toNum(li.quantity) * toNum(li.unit_price), 0);
  // Trust the server for the rate. Don't default to a hardcoded 8.25% —
  // that's been silently distorting QB-imported invoices' totals (Doug
  // 2026-05-06 / S110). Only overwrite the tenant-default rate (loaded
  // separately by loadTaxRate) when the invoice itself carries one.
  const serverRate = payload.tax_rate ?? payload.taxRate;
  if (serverRate != null) {
    const rate = toNum(serverRate);
    // Already a decimal fraction on the server; tolerate older payloads
    // that pass percentages by detecting >1.
    taxRate.value = rate > 1 ? rate / 100 : rate;
  }

  invoice.value = {
    id: payload.id,
    invoice_number: payload.invoice_number || payload.invoiceNumber || `INV-${String(payload.id).substring(0, 8)}`,
    customer_id: payload.customer_id || null,
    customer_name: payload.customer_name || payload.customer || (typeof payload.customer === "object" ? payload.customer?.name : "") || "Unknown",
    customer_email: payload.customer_email || "",
    customer_phone: payload.customer_phone || "",
    customer_address: payload.customer_address || "",
    status: payload.effective_status || payload.status || "Draft",
    subtotal: toNum(payload.subtotal),
    taxable_subtotal: payload.taxable_subtotal === undefined ? undefined : toNum(payload.taxable_subtotal),
    tax_rate: payload.tax_rate == null ? null : toNum(payload.tax_rate),
    tax_amount: toNum(payload.tax_amount),
    total: toNum(payload.total ?? payload.amount ?? computedTotal),
    invoice_date: payload.invoice_date || payload.invoiceDate || "",
    due_date: payload.due_date || payload.dueDate || "",
    created_at: payload.created_at || payload.createdAt || "",
    notes: payload.notes || "",
    // PR6 — drives the Pause/Resume reminders toggle.
    dunning_paused: Boolean(payload.dunning_paused),
    // Drives the edit-mode "hide line-item prices on PDF" toggle.
    hide_line_prices: Boolean(payload.hide_line_prices),
    line_items: lineItems,
    payments,
  };
}

// --- Actions ---
async function openCustomerEdit() {
  if (!invoice.value.customer_id) return;
  // Pull the full customer so the dialog edits a complete record (notes,
  // access_notes, customer_type, etc. aren't on the invoice payload).
  try {
    const result = await api.get(`/api/customers/${invoice.value.customer_id}`);
    customerForEdit.value = result?.data || result || {
      id: invoice.value.customer_id,
      name: invoice.value.customer_name,
      email: invoice.value.customer_email,
      phone: invoice.value.customer_phone,
      address: invoice.value.customer_address,
    };
  } catch {
    // Fall back to the slice we already have on the invoice payload so the
    // dialog still opens — the user can at least add the missing email.
    customerForEdit.value = {
      id: invoice.value.customer_id,
      name: invoice.value.customer_name,
      email: invoice.value.customer_email,
      phone: invoice.value.customer_phone,
      address: invoice.value.customer_address,
    };
  }
  showCustomerEditDialog.value = true;
}

async function onCustomerSaved() {
  // Re-fetch so the Bill-To card reflects the saved fields (server is the
  // source of truth — esp. for encrypted columns like address).
  await fetchInvoice();
}

async function fetchInvoice() {
  loading.value = true;
  try {
    const result = await api.get(`/api/invoices/${route.params.id}`);
    normalizeInvoice(result?.data || result || {});
  } catch {
    toast.add({ severity: "warn", summary: "Offline", detail: "Using placeholder data", life: 3000 });
    normalizeInvoice({
      id: route.params.id,
      invoice_number: `INV-${route.params.id}`,
      customer: "Sample Customer",
      status: "Draft",
      due_date: "2026-04-20",
      line_items: [
        { id: 1, description: "Service call", quantity: 1, unit_price: 150 },
        { id: 2, description: "Parts", quantity: 2, unit_price: 75 },
      ],
      payments: [],
    });
  } finally {
    loading.value = false;
  }
}

async function sendInvoice() {
  // 2026-05-15 — replaced the direct POST /send (which fired a server-side
  // HTML email with no PDF attached) with the same composer flow estimates
  // use: open dialog with PDF preview → user reviews → send via Outlook (or
  // mailto fallback). 2026-05-12 accidental-send guardrail is now built into
  // the dialog itself: nothing leaves the browser until the user clicks Send.
  composerLoading.value = true;
  showComposer.value = true;
  composer.value = { to: "", subject: "", body_text: "", pdf: null, extras: [] };
  try {
    const data = await api.get(`/api/invoices/${route.params.id}/email-compose`);
    const payload = data?.data || data;
    composer.value = {
      to: (payload.to && payload.to[0]) || "",
      subject: payload.subject || "",
      body_text: payload.body_text || "",
      pdf: payload.pdf,
      extras: (payload.extra_attachments || []).map((a) => ({ ...a, _include: true })),
    };
  } catch (err) {
    showComposer.value = false;
    toast.add({ severity: "error", summary: "Compose failed", detail: err?.message || "", life: 4000 });
  } finally {
    composerLoading.value = false;
  }
}

async function _blobToBase64(blob) {
  return await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error);
    r.onload = () => {
      const s = String(r.result || "");
      const i = s.indexOf(",");
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    r.readAsDataURL(blob);
  });
}

async function sendComposer() {
  if (!composer.value.to) return;
  composerSending.value = true;
  try {
    const atts = [
      {
        name: composer.value.pdf.name,
        content_type: composer.value.pdf.content_type,
        content_base64: composer.value.pdf.content_base64,
      },
    ];
    for (const ex of composer.value.extras) {
      if (!ex._include) continue;
      try {
        const blobUrl = await createAuthedBlobUrl(
          `/api/documents/${ex.id}/download`,
        );
        const blob = await (await fetch(blobUrl)).blob();
        URL.revokeObjectURL(blobUrl);
        atts.push({
          name: ex.name,
          content_type: ex.content_type,
          content_base64: await _blobToBase64(blob),
        });
      } catch (e) {
        toast.add({ severity: "warn", summary: "Skipping attachment", detail: ex.name, life: 3000 });
      }
    }
    // Escape first (quotes included — a raw " in a URL would otherwise
    // break out of the href attribute), then linkify bare URLs. Outlook
    // desktop doesn't auto-link plain text inside HTML bodies, so the
    // "Pay online:" link from the compose draft would arrive unclickable
    // without the anchor. The URL match stops at any escaped entity except
    // &amp;, so escaped quotes terminate the href cleanly.
    const escapedBody = composer.value.body_text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    const linkedBody = escapedBody.replace(
      /(https?:\/\/[^\s&]+(?:&amp;[^\s&]+)*)/g,
      '<a href="$1">$1</a>',
    );
    const bodyHtml = `<pre style="font-family:Arial,sans-serif;font-size:14px;white-space:pre-wrap">${
      linkedBody
    }</pre>`;
    try {
      // suppressErrorToast so a 409 (Outlook not connected) doesn't fire a
      // red error toast before the catch-block surfaces the mailto fallback.
      // useApiWithToast IS useApi (re-export since 2026-05-09), so the only
      // way to suppress is via this option — not by picking a different client.
      await api.post("/api/outlook/send", {
        to: [composer.value.to],
        subject: composer.value.subject,
        body_html: bodyHtml,
        attachments: atts,
      }, { suppressErrorToast: true });
      try {
        await api.post(`/api/invoices/${route.params.id}/mark-sent`, {}, { suppressErrorToast: true });
        await fetchInvoice();
      } catch (mse) {
        // Email left the building but we couldn't flip the row. Surface so
        // the operator knows to update status manually instead of resending.
        toast.add({
          severity: "warn",
          summary: "Emailed but status not flipped",
          detail: "Invoice was sent but the server didn't update its status. Refresh the page; if it still shows Draft, edit and re-mark.",
          life: 8000,
        });
      }
      toast.add({
        severity: "success",
        summary: "Sent",
        detail: `Invoice emailed to ${composer.value.to}. Check your Sent folder.`,
        life: 5000,
      });
      showComposer.value = false;
    } catch (err) {
      const status = err?.status || err?.response?.status;
      if (status === 409) {
        toast.add({
          severity: "info",
          summary: "Opening your mail client",
          detail: "Outlook isn't connected for this user — using your default mail client instead.",
          life: 5000,
        });
        await _emailViaMailtoFallback(composer.value, atts[0]);
        showComposer.value = false;
      } else {
        toast.add({ severity: "error", summary: "Send failed", detail: err?.message || "Outlook rejected the send", life: 5000 });
      }
    }
  } finally {
    composerSending.value = false;
  }
}

async function _emailViaMailtoFallback(c, pdfAtt) {
  // Save PDF locally so the user can drag-attach into their default mail
  // client. mailto: itself doesn't carry attachments — we surface both.
  const bytes = Uint8Array.from(atob(pdfAtt.content_base64), (ch) => ch.charCodeAt(0));
  const blob = new Blob([bytes], { type: pdfAtt.content_type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = pdfAtt.name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  const mailto = `mailto:${encodeURIComponent(c.to)}?subject=${encodeURIComponent(c.subject)}&body=${encodeURIComponent(c.body_text)}`;
  window.location.href = mailto;
  try {
    await api.post(`/api/invoices/${route.params.id}/mark-sent`, {}, { suppressErrorToast: true });
    await fetchInvoice();
  } catch (mse) {
    // Same as the Outlook path: surface a status-flip failure so the
    // operator knows the email left their hands but the row still says Draft.
    toast.add({
      severity: "warn",
      summary: "Email handed to your mail client",
      detail: "Status not auto-flipped — verify it sent, then mark this invoice as Sent manually.",
      life: 7000,
    });
  }
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(2)} MB`;
}

async function toggleDunningPause() {
  // PR6 — explicit per-invoice mute (payment arrangement made).
  try {
    const next = !invoice.value.dunning_paused;
    await api.post(`/api/invoices/${id}/dunning-pause`, { paused: next });
    invoice.value.dunning_paused = next;
    toast.add({
      severity: 'info',
      summary: next ? 'Reminders paused' : 'Reminders resumed',
      detail: next
        ? 'Automated payment reminders are muted for this invoice.'
        : 'This invoice is back on the automated reminder schedule.',
      life: 3000,
    });
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Error', detail: e.message || 'Failed to update reminders', life: 4000 });
  }
}

async function recordPayment() {
  savingPayment.value = true;
  try {
    // Backend PaymentCreateIn requires `date`. The dialog doesn't expose a
    // date picker (operators record payments same-day in the field) so
    // default to today. Pre-fix Save Payment 422'd on every click with
    // {type: "missing", loc: ["body", "date"]}.
    const result = await api.post(`/api/invoices/${route.params.id}/payments`, {
      amount: newPayment.value.amount,
      method: newPayment.value.method,
      reference: newPayment.value.reference,
      date: new Date().toISOString().slice(0, 10),
    });
    const saved = result?.data || result || {};
    invoice.value.payments.push({
      id: saved.id ?? `pay-${Date.now()}`,
      amount: toNum(saved.amount || newPayment.value.amount),
      method: saved.method || newPayment.value.method,
      reference: saved.reference || newPayment.value.reference,
      date: saved.date || new Date().toISOString().slice(0, 10),
    });
    // Update status if fully paid
    if (balanceDue.value <= 0) {
      invoice.value.status = "Paid";
    }
    showPaymentDialog.value = false;
    newPayment.value = { amount: 0, method: "Cash", reference: "" };
    toast.add({ severity: "success", summary: "Recorded", detail: "Payment recorded", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to record payment", life: 3000 });
  } finally {
    savingPayment.value = false;
  }
}

// --- Edit-mode actions ---
function enterEditMode() {
  // Snapshot the current invoice into editLines + edit fields. _key is
  // a Vue v-for key that survives re-orders; lines new since enterEdit
  // get a temporary key so we can identify them at save time.
  editLines.value = invoice.value.line_items.map((ln, i) => ({
    _key: `e-${ln.id ?? i}`,
    id: typeof ln.id === "string" && ln.id.length >= 32 ? ln.id : null,
    description: ln.description || "",
    quantity: toNum(ln.quantity) || 1,
    unit_price: toNum(ln.unit_price),
    taxable: ln.taxable !== false,
    // D-S122b-detail-view-columns — snapshot the new fields too.
    category: ln.category || null,
    cost: ln.cost_snapshot != null ? toNum(ln.cost_snapshot) : null,
    // Form shows percent (e.g. 35); backend stores decimal (0.35). Round-
    // trip via *100 on entry and /100 on save.
    margin_pct_override: ln.margin_pct_override != null
      ? Number((ln.margin_pct_override * 100).toFixed(2))
      : null,
  }));
  // Seed the rate input. Prefer the invoice's own rate; fall back to the
  // tenant default so legacy invoices get a sensible starting point on
  // first edit instead of showing 0%.
  const startRate = invoice.value.tax_rate != null
    ? toNum(invoice.value.tax_rate)
    : taxRate.value;
  editTaxRatePct.value = Number((startRate * 100).toFixed(4));
  editInvoiceDate.value = invoice.value.invoice_date || "";
  editDueDate.value = invoice.value.due_date || "";
  editNotes.value = invoice.value.notes || "";
  editHideLinePrices.value = Boolean(invoice.value.hide_line_prices);
  editing.value = true;
}

function cancelEdit() {
  editing.value = false;
  editLines.value = [];
}

async function saveEdit() {
  // Diff editLines against the current invoice, fire one request per
  // change. Server runs _recalculate_invoice on every line write so
  // totals/tax stay correct even if the patch sequence is interrupted.
  savingEdit.value = true;
  try {
    const id = route.params.id;
    const original = invoice.value.line_items;
    const originalById = new Map(original.map((ln) => [String(ln.id), ln]));
    const keptIds = new Set();

    // 1. Updates + inserts
    for (const ln of editLines.value) {
      const desc = (ln.description || "").trim();
      if (!desc) continue;  // skip rows with no description
      const qty = Math.max(1, Math.floor(toNum(ln.quantity) || 1));
      const price = Math.max(0, toNum(ln.unit_price));
      // D-S122b-detail-view-columns — forward category/cost/margin too.
      const category = ln.category || null;
      const cost = ln.cost != null && toNum(ln.cost) > 0 ? toNum(ln.cost) : null;
      const marginOverrideDec = ln.margin_pct_override != null && toNum(ln.margin_pct_override) > 0
        ? toNum(ln.margin_pct_override) / 100
        : null;
      if (ln.id && originalById.has(String(ln.id))) {
        keptIds.add(String(ln.id));
        const orig = originalById.get(String(ln.id));
        const origCost = orig.cost_snapshot != null ? toNum(orig.cost_snapshot) : null;
        const origMargin = orig.margin_pct_override != null ? toNum(orig.margin_pct_override) : null;
        const changed =
          orig.description !== desc ||
          toNum(orig.quantity) !== qty ||
          toNum(orig.unit_price) !== price ||
          (orig.taxable !== false) !== Boolean(ln.taxable) ||
          (orig.category || null) !== category ||
          origCost !== cost ||
          origMargin !== marginOverrideDec;
        if (changed) {
          const patch = {
            description: desc,
            quantity: qty,
            unit_price: price,
            taxable: Boolean(ln.taxable),
          };
          // Auditor catch (round 2): include the field in the PATCH even when
          // the new value is null — backend's exclude_unset=True semantics
          // mean omitted fields stay unchanged, so clearing a cost requires
          // an explicit `cost: null`.
          if (category !== (orig.category || null)) patch.category = category;
          if (cost !== origCost) patch.cost = cost;
          if (marginOverrideDec !== origMargin) patch.margin_pct_override = marginOverrideDec;
          await api.patch(`/api/invoices/${id}/lines/${ln.id}`, patch);
        }
      } else {
        const body = {
          description: desc,
          quantity: qty,
          unit_price: price,
          taxable: Boolean(ln.taxable),
        };
        if (category) body.category = category;
        if (cost != null) body.cost = cost;
        if (marginOverrideDec != null) body.margin_pct_override = marginOverrideDec;
        const lineResp = await api.post(`/api/invoices/${id}/lines`, body);
        // PR1-billing-capture: surface the F-75 zero-price warning the
        // server attaches in warn-mode — it was emitted but never rendered.
        if (lineResp && lineResp.warning) {
          toast.add({
            severity: 'warn',
            summary: 'Review pricing',
            detail: `${lineResp.warning}: ${desc}`,
            life: 8000,
          });
        }
      }
    }

    // 2. Deletions — anything in original that didn't appear in the
    // post-edit kept set.
    for (const orig of original) {
      if (!keptIds.has(String(orig.id))) {
        await api.del(`/api/invoices/${id}/lines/${orig.id}`);
      }
    }

    // 3. Tax rate / dates / notes via PATCH on the invoice. The rate is
    // sent exactly as displayed, INCLUDING an explicit 0 — the server then
    // recomputes tax_amount to $0. (Sending null here instead used to
    // PRESERVE the previously computed tax dollars, so zeroing the field
    // looked like it silently reverted.)
    const ratePct = toNum(editTaxRatePct.value);
    const ratePayload = Number.isFinite(ratePct) ? ratePct / 100 : 0;
    await api.patch(`/api/invoices/${id}`, {
      tax_rate: ratePayload,
      invoice_date: editInvoiceDate.value || null,
      due_date: editDueDate.value || null,
      notes: editNotes.value || null,
      hide_line_prices: editHideLinePrices.value,
    });

    toast.add({ severity: "success", summary: "Saved", detail: "Invoice updated", life: 3000 });
    editing.value = false;
    await fetchInvoice();
  } catch (err) {
    toast.add({
      severity: "error",
      summary: "Save failed",
      detail: err?.message || "Could not save invoice changes",
      life: 5000,
    });
  } finally {
    savingEdit.value = false;
  }
}

async function downloadPdf() {
  try {
    await openAuthedFile(`/api/invoices/${route.params.id}/pdf`);
  } catch (e) {
    console.error("invoice_pdf_failed", e);
    toast.add({
      severity: "error",
      summary: "PDF failed",
      detail: e?.message || "Could not open invoice PDF",
      life: 5000,
    });
  }
}

function confirmDelete() {
  confirmDestructive({
    message: `Delete ${invoice.value.invoice_number}? This cannot be undone.`,
    header: "Confirm Delete",
    accept: async () => {
      try {
        await api.del(`/api/invoices/${route.params.id}`);
        toast.add({ severity: "success", summary: "Deleted", detail: "Invoice deleted", life: 3000 });
        router.push("/billing");
      } catch (err) {
        toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to delete", life: 3000 });
      }
    },
  });
}

function pushToQuickbooks() {
  // Push IS destructive — it creates a QB invoice on the live realm and
  // can't be undone from the GDX side (operator has to void in QB if
  // wrong). 2026-05-12 audit-walk accident pushed a draft test invoice
  // to real QB. Confirm before firing.
  const totalLabel = invoice.value?.total != null
    ? currency(invoice.value.total)
    : "this invoice";
  confirmDestructive({
    message: `Push ${invoice.value?.invoice_number || "this invoice"} (${totalLabel}) to QuickBooks? A QB invoice will be created on the live realm.`,
    header: "Push to QuickBooks",
    icon: "pi pi-cloud-upload",
    acceptClass: "p-button-primary",
    acceptLabel: "Push to QB",
    rejectLabel: "Cancel",
    accept: () => doPushToQuickbooks(),
  });
}

async function doPushToQuickbooks() {
  pushingToQb.value = true;
  try {
    const id = route.params.id;
    const result = await api.post(`/api/qb/push/invoice/${id}`);
    const qbId = result?.qb_invoice_id || result?.qb_id;
    toast.add({
      severity: "success",
      summary: "Pushed to QuickBooks",
      detail: qbId ? `QuickBooks invoice ${qbId}` : "Invoice synced",
      life: 3000,
    });
  } catch (err) {
    toast.add({
      severity: "error",
      summary: "Push failed",
      detail: err?.message || "Could not push invoice to QuickBooks",
      life: 4000,
    });
  } finally {
    pushingToQb.value = false;
  }
}

async function loadQbStatus() {
  // Hide the Push to QB button when no QB connection is configured.
  // Falls back to the dashboard endpoint, then status. Either failing
  // means no QB integration on this tenant — leave button hidden.
  try {
    const dash = await api.get("/api/qb/dashboard").catch(() => api.get("/api/qb/status"));
    qbConnected.value = !!dash?.connected;
  } catch {
    qbConnected.value = false;
  }
}

async function loadTaxRate() {
  try {
    const cfg = await api.get("/api/tax/config");
    if (cfg && typeof cfg.default_rate === "number") {
      taxRate.value = cfg.default_rate;
    }
  } catch {
    // tax module may not be wired on this tenant yet — leave default
  }
}

onMounted(() => {
  // ?compose=1 — the Billing list's per-row Send lands here so every send
  // goes through the composer (preview + explicit click) instead of the
  // old fire-and-forget POST /send. Strip the flag so refresh/back doesn't
  // reopen the dialog, and gate on the loaded status so a hand-typed URL
  // can't open the composer on a paid/void invoice (mirrors the button).
  const autoCompose = !!route.query?.compose;
  if (autoCompose) {
    const { compose, ...rest } = route.query;
    router.replace({ query: rest })?.catch?.(() => {});
  }
  Promise.resolve(fetchInvoice()).then(() => {
    const st = String(invoice.value.status || "").toLowerCase();
    if (autoCompose && !["paid", "void"].includes(st)) sendInvoice();
  });
  loadTaxRate();
  loadQbStatus();
});
</script>

<style scoped>
.detail-header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.5rem;
}
.detail-header h2 {
  margin: 0.25rem 0;
}
.customer-name {
  color: var(--p-text-muted-color, #6b7280);
  margin: 0;
}
.header-meta {
  text-align: right;
}
.header-meta p {
  margin: 0.2rem 0;
  font-size: 0.875rem;
}

.mb-1 {
  margin-bottom: 1rem;
}

.totals-section {
  max-width: 360px;
  margin-left: auto;
  margin-top: 1rem;
}
.total-row {
  display: flex;
  justify-content: space-between;
  padding: 0.3rem 0;
  font-size: 0.925rem;
}
.total-row.grand {
  font-size: 1.1rem;
  border-top: 2px solid var(--p-content-border-color, #ddd);
  padding-top: 0.5rem;
  margin-top: 0.25rem;
}
.total-row.paid {
  color: var(--p-green-500, #22c55e);
}
.total-row.balance {
  color: var(--p-red-500, #ef4444);
  font-weight: 700;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 1rem 0;
}

.notes-section {
  margin-top: 1rem;
}
.notes-section p {
  white-space: pre-wrap;
}

.form-grid-single {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.form-field label {
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--p-text-muted-color, #6b7280);
}
.form-field :deep(.p-dropdown),
.form-field :deep(.p-inputtext),
.form-field :deep(.p-inputnumber) {
  width: 100%;
}

@media (max-width: 900px) {
  .detail-header {
    flex-direction: column;
  }
  .header-meta {
    text-align: left;
  }
  .totals-section {
    max-width: 100%;
  }
}
.link { color: var(--p-primary-color); text-decoration: none; }
.link:hover { text-decoration: underline; }
.form-hint { color: var(--text-muted, #94a3b8); font-size: 0.8rem; margin-top: 0.25rem; }
.overpaid { color: #f59e0b; }

.lines-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 1rem;
  margin-bottom: 0.5rem;
}
.lines-header h3 {
  margin: 0;
}
.lines-header-actions {
  display: flex;
  gap: 0.5rem;
}
.edit-meta-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(160px, 1fr));
  gap: 0.75rem 1rem;
  margin-top: 0.75rem;
  padding: 0.75rem;
  background: var(--p-surface-50, #f8f9fa);
  border-radius: 0.5rem;
}
.edit-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.edit-field label {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--p-text-color-secondary, #6c757d);
}
.edit-field .hint {
  color: var(--p-text-color-secondary, #6c757d);
  font-size: 0.75rem;
}
.composer-loading { padding: 2rem; text-align: center; color: #6b7280; }
.composer-form { display: flex; flex-direction: column; gap: 0.75rem; }
.composer-form .form-field { display: flex; flex-direction: column; gap: 0.25rem; }
.composer-attachments { display: flex; flex-direction: column; gap: 0.4rem; }
.composer-att-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 4px;
  cursor: pointer;
}
.composer-att-row span { flex: 1; word-break: break-word; }
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.85em; }

.bill-to-card {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin: 0.5rem 0 1rem;
  background: var(--p-content-background, #fff);
}
.bill-to-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}
.bill-to-header h3 {
  margin: 0;
  font-size: 0.95rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
}
.bill-to-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.4rem 1rem;
}
.bill-to-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.95rem;
}
.bill-to-row i { color: var(--p-text-muted-color, #6b7280); width: 1rem; }
.bill-to-row a { color: var(--p-primary-color, #3b82f6); text-decoration: none; }
.bill-to-row a:hover { text-decoration: underline; }
.bill-to-row .add-link { font-style: italic; }
</style>
