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

    <Message v-if="adjustsInvoiceId" severity="warn" :closable="false" data-testid="supplemental-banner">
      Supplemental invoice{{ adjustsInvoiceNumber ? ' adjusting ' + adjustsInvoiceNumber : '' }}
      — the closeout changed after billing. Add only the difference; the original invoice stays as-is.
    </Message>

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

          <!-- The tech's attested closeout — what the office bills from
               (Doug 2026-08-07: "click invoice — it does not show hours or
               notes from the job"). Display-only context; the labor line
               below is prefilled from the same numbers when nothing else
               (an estimate) already claimed the editor. -->
          <div v-if="closeoutSuggestion?.has_closeout" class="form-field full-width closeout-context" data-testid="closeout-context">
            <div class="closeout-context-head">
              <strong>From the job closeout</strong>
              <span v-if="closeoutSuggestion.closeout.closed_at" class="muted">
                closed {{ closeoutSuggestion.closeout.closed_at.slice(0, 10) }}
              </span>
            </div>
            <div class="closeout-context-row">
              <span data-testid="closeout-context-hours">
                {{ closeoutSuggestion.closeout.hours_worked }} h on site ×
                {{ closeoutSuggestion.closeout.techs_on_site }} tech{{ closeoutSuggestion.closeout.techs_on_site === 1 ? '' : 's' }}
              </span>
              <span v-if="closeoutSuggestion.closeout.no_parts_used" class="muted">· no parts used</span>
            </div>
            <p v-if="closeoutSuggestion.closeout.notes" class="closeout-context-notes" data-testid="closeout-context-notes">
              “{{ closeoutSuggestion.closeout.notes }}”
            </p>
            <Button
              v-if="closeoutSuggestion.closeout.notes && !form.notes"
              size="small" text label="Use as invoice notes"
              data-testid="use-closeout-notes"
              @click="form.notes = closeoutSuggestion.closeout.notes"
            />
            <!-- The tech's JOB notes (mobile Add-note) — usually the real
                 work summary (round 2: "missing the notes the tech put on
                 it"). Internal notes are badged; nothing copies onto the
                 customer-facing invoice without an explicit tap. -->
            <div v-if="closeoutSuggestion.job_notes?.length" class="closeout-tech-notes" data-testid="closeout-tech-notes">
              <div class="closeout-context-head" style="margin-top: 0.5rem">
                <strong>Tech notes on the job</strong>
              </div>
              <div v-for="(n, idx) in closeoutSuggestion.job_notes" :key="idx" class="tech-note-row">
                <p class="closeout-context-notes">
                  “{{ n.body }}”
                  <span class="muted">— {{ n.author_name || 'tech' }}<template v-if="n.visibility === 'internal'"> · internal</template></span>
                </p>
                <span class="tech-note-actions">
                  <Button size="small" text label="Add to invoice notes"
                    :data-testid="`note-to-invoice-notes-${idx}`"
                    @click="appendNoteToInvoiceNotes(n)" />
                  <Button size="small" text label="Use as labor description"
                    :data-testid="`note-to-labor-desc-${idx}`"
                    @click="useNoteAsLaborDescription(n)" />
                </span>
              </div>
            </div>
          </div>

          <!-- The same part recorded more than once on this job, still
               unbilled (2026-08-19). Capture rows are never machine-merged —
               any automatic dedup undercounts or double-counts — so the
               office is told before it prices anything. Deliberately a
               SIBLING of the closeout card, not a child: mobile and van
               captures happen on jobs with no closeout at all. -->
          <div
            v-if="closeoutSuggestion?.duplicate_part_warnings?.length"
            class="form-field full-width closeout-dupe-warning"
            data-testid="duplicate-part-warnings"
          >
            <div class="closeout-context-head">
              <strong>Recorded more than once — check before billing</strong>
            </div>
            <p
              v-for="(d, idx) in closeoutSuggestion.duplicate_part_warnings"
              :key="idx"
              class="closeout-context-notes"
              :data-testid="`duplicate-part-warning-${idx}`"
            >
              {{ d.part }}<template v-if="d.sku"> ({{ d.sku }})</template>
              — logged {{ d.times_captured }}× at qty {{ d.quantity }}
              <span class="muted">via {{ d.sources.join(' + ') }}</span>
            </p>
            <p class="muted closeout-context-notes">
              These are separate records, not one part counted twice by the
              system. Bill whichever reflects the work actually done.
            </p>
          </div>
          <div class="form-field full-width">
            <label>Line Items</label>
            <LineItemEditor
              v-model:lines="form.line_items"
              v-model:fromPartIds="form.from_part_ids"
              :job-id="form.job_id || null"
              :categories="lineCategories"
              :closeout="closeoutSuggestion"
              show-taxable
              show-cost
              show-margin
              show-labor
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

          <!-- Job photos on the invoice PDF (2026-08-12). The picker used to
               exist only on the invoice DETAIL page, after the draft was
               already created — so the office building an invoice here, from
               the job the tech just photographed, had no way to attach them.
               Production says the feature had never been used once. -->
          <div class="form-field full-width" v-if="form.job_id" data-testid="invoice-create-photos">
            <label>Job photos on this invoice</label>
            <div v-if="jobPhotosLoading" class="muted">Loading photos…</div>
            <div v-else-if="jobPhotosError" class="muted" data-testid="invoice-create-photos-error">
              {{ jobPhotosError }}
            </div>
            <div v-else-if="!jobPhotos.length" class="muted" data-testid="invoice-create-photos-empty">
              No photos on this job yet.
            </div>
            <div v-else class="photo-pick-grid">
              <label
                v-for="p in jobPhotos"
                :key="p.id"
                class="photo-pick"
                :class="{ selected: form.attached_photo_ids.includes(p.id) }"
                :data-testid="`invoice-create-photo-${p.id}`"
              >
                <input type="checkbox" :value="p.id" v-model="form.attached_photo_ids" />
                <AuthedImage :src="p.url" :alt="p.caption || p.kind || 'Job photo'" class="photo-pick-thumb">
                  <template #fallback>
                    <span class="photo-pick-failed">Image unavailable</span>
                  </template>
                </AuthedImage>
                <span class="photo-pick-meta">{{ p.kind || 'photo' }}</span>
              </label>
            </div>
            <small class="muted" v-if="form.attached_photo_ids.length">
              {{ form.attached_photo_ids.length }} photo(s) will print on the invoice PDF.
            </small>
          </div>

          <!-- D2 (Doug 2026-08-19: "whole invoice") — mirrors the estimate's
               Discount field. Materialized server-side as the SAME negative
               `category="discount"` line the estimate copy mints, so both
               surfaces produce identical rows. Before this the office could
               not enter a discount at all: the contract forbids a negative
               unit_price, and the only discount row the system minted came
               from a path /billing/new never triggers. -->
          <div class="form-field">
            <label for="inv-discount">Discount</label>
            <InputNumber
              id="inv-discount"
              v-model="form.discount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0"
              class="w-full"
              data-testid="invoice-discount"
            />
            <small class="muted">Comes off the taxable total, like the estimate's.</small>
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
          <div class="totals-row" v-if="toNum(form.discount) > 0">
            <span>Discount</span>
            <span class="discount-amount" data-testid="invoice-discount-amount">
              -{{ currency(toNum(form.discount)) }}
            </span>
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
import Message from 'primevue/message';
import Select from 'primevue/select';
import DatePicker from 'primevue/datepicker';
import InputNumber from 'primevue/inputnumber';
import Textarea from 'primevue/textarea';
import Divider from 'primevue/divider';
import { useToast } from 'primevue/usetoast';
import { useApi } from '../composables/useApi';
import { formatMoney as currency } from '../composables/useFormatters';
import LineItemEditor from '../components/LineItemEditor.vue';
import { LINE_CATEGORY_OPTIONS } from '../composables/useLineCategories';
import AuthedImage from '../components/AuthedImage.vue';

const route = useRoute();
const router = useRouter();
const api = useApi();
const toast = useToast();

const loading = ref(true);
const creating = ref(false);
const adjustsInvoiceId = ref('');
const adjustsInvoiceNumber = ref('');

// One shared option list (composables/useLineCategories.js). Two of the three
// hardcoded copies are gone — this one and InvoiceDetailView's. EstimateView
// still has its own (a plain string array, different shape) and p5 replaces
// all of them with GET /api/catalogs/pricing-categories, so an admin-seeded
// bucket surfaces everywhere without a code change.
const lineCategories = LINE_CATEGORY_OPTIONS;

const form = ref({
  customer_id: null,
  job_id: null,
  invoice_date: null,
  due_date: null,
  // Stored as a percent integer in the form (8.25), converted to decimal
  // (0.0825) at POST time. Form-friendly; backend-canonical.
  tax_rate_pct: 0,
  discount: null,
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
  // job_photos.id values to print on the PDF (validated server-side against
  // this invoice's job, exactly like the PATCH path).
  attached_photo_ids: [],
});

// The job's photos, for the picker above. Read from job_photos — the same
// endpoint the job page and the invoice detail picker use.
const jobPhotos = ref([]);
const jobPhotosLoading = ref(false);
const jobPhotosError = ref('');

async function loadJobPhotos(jobId) {
  jobPhotos.value = [];
  jobPhotosError.value = '';
  // Picks belong to the job they were made on: switching jobs must not carry
  // a photo id across, or create 422s on an id the new job doesn't own.
  form.value.attached_photo_ids = [];
  if (!jobId) return;
  jobPhotosLoading.value = true;
  try {
    const rows = await api.get(`/api/jobs/${jobId}/photos`, { suppressErrorToast: true });
    jobPhotos.value = Array.isArray(rows) ? rows : [];
  } catch (err) {
    // Never silently empty: "no photos" and "you can't see the photos" are
    // different answers and the office needs to know which it got.
    jobPhotosError.value = (err?.status === 403 || err?.status === 404)
      ? "You don't have access to this job's photos."
      : 'Could not load this job\'s photos.';
  } finally {
    jobPhotosLoading.value = false;
  }
}

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

// The discount comes off the TAXABLE base, floored at 0 — mirroring
// `_recalculate_invoice`, which floors it server-side when the discount
// exceeds the goods. Getting this wrong here would show the operator a total
// the invoice does not have.
const discountedTaxableBase = computed(() =>
  Math.max(taxableSubtotal.value - toNum(form.value.discount), 0),
);

const taxAmount = computed(() =>
  Math.round(discountedTaxableBase.value * (toNum(form.value.tax_rate_pct) / 100) * 100) / 100,
);

const total = computed(() =>
  Math.max(subtotal.value - toNum(form.value.discount), 0) + taxAmount.value,
);

const canCreate = computed(() => {
  // Customer is the AR target — always required. Job is optional so
  // counter-sale (parts/over-the-counter) invoices can exist. At least one
  // PRICED line still required to enable Create — an all-$0 invoice is a
  // deliberate act the operator confirms by pricing at least something —
  // but described $0 companions are no longer silently dropped at submit.
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
      // Clearing job_id HERE is programmatic, so the Select's @change never
      // fires and onJobChange's cleanup never runs. Without this line the
      // estimate provenance survived a customer switch, and customer B's
      // invoice shipped carrying customer A's estimate link — audited as
      // "prefilled", which is a lie about whose numbers these are.
      sourceEstimateId.value = null;
    }
  }
  resolveTaxRate();
}

function onJobChange() {
  // Different job, different estimate. Stale provenance would link the invoice
  // to an estimate for work it does not cover.
  sourceEstimateId.value = null;
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
  // Same reasoning for photos: they are job-scoped, and the loader clears the
  // previous job's picks.
  loadJobPhotos(form.value.job_id);
  // Sequence matters: estimate first (it wins the editor), closeout second
  // (fills only if the editor is still the empty starter row).
  prefillFromJobEstimate(form.value.job_id).then(() =>
    prefillFromJobCloseout(form.value.job_id),
  );
}

// Closeout prefill (2026-08-07): the attested hours become a priced labor
// line (same billing_lanes math the autodraft and truck paths use) and the
// closeout context renders above the editor. Runs AFTER the estimate
// prefill and only fills a still-empty editor — an estimate outranks the
// lanes (§15.1), and hand-typed lines are never clobbered.
const closeoutSuggestion = ref(null);
// The accepted estimate whose lines prefilled this editor, if any. Recorded on
// the invoice as provenance — the operator may edit the lines afterwards, so
// this says "the numbers started here", not "these are the estimate's lines".
const sourceEstimateId = ref(null);

function appendNoteToInvoiceNotes(note) {
  const body = (note?.body || '').trim();
  if (!body) return;
  form.value.notes = form.value.notes ? `${form.value.notes}\n${body}` : body;
}

function useNoteAsLaborDescription(note) {
  // The tech's work summary usually beats the man-hours boilerplate on a
  // customer bill. Targets the first Labor line (else the first line);
  // the field stays a plain editable input either way.
  const body = (note?.body || '').trim();
  if (!body || !form.value.line_items.length) return;
  const target =
    form.value.line_items.find((l) => (l.category || '').toLowerCase() === 'labor') ||
    form.value.line_items[0];
  target.description = body.slice(0, 500);
}

async function prefillFromJobCloseout(jobId) {
  closeoutSuggestion.value = null;
  if (!jobId) return;
  try {
    const s = await api.get(
      `/api/jobs/${jobId}/closeout-billing-suggestion`,
      { suppressErrorToast: true },
    );
    // Store BEFORE the has_closeout gate: duplicate_part_warnings covers
    // mobile and van captures, which happen on jobs that never get a
    // closeout. Gating the whole payload on has_closeout made the warning
    // dead for the exact rows this release started pricing.
    closeoutSuggestion.value = s;
    if (!s?.has_closeout) return;
    // Round 2 (Doug 2026-08-07): the closeout's own note now moves onto the
    // invoice automatically — it was attested at billing time and the
    // operator can edit or clear it before saving. Job notes stay opt-in
    // (they're often internal).
    if (s.closeout?.notes && !form.value.notes) {
      form.value.notes = s.closeout.notes;
    }
    const starterOnly =
      form.value.line_items.length === 1 &&
      !form.value.line_items[0].description &&
      !toNum(form.value.line_items[0].unit_price);
    if (s.labor_line && starterOnly) {
      // Provenance rides the prefill too. This is the DOMINANT path — most
      // invoices get their labor line here, not from the picker — so leaving it
      // NULL would mean the column answers "how was this priced?" only for the
      // minority of lines somebody added by hand.
      //
      // The server says which lane it computed; trust that rather than
      // re-deriving it, and never claim 'matrix' without the row id (the API
      // rejects that shape, and it would be an unverifiable claim).
      const src = s.labor_line.source;
      const provenance = src === 'matrix' && s.labor_line.labor_price_item_id
        ? {
            labor_source: 'matrix',
            labor_price_item_id: s.labor_line.labor_price_item_id,
            // The price this provenance refers to, so a later reprice
            // downgrades matrix -> manual. Without it the guard in
            // markPriceOverride never fires and a repriced line keeps claiming
            // the matrix quoted it — and THIS is the dominant path: most
            // invoices get their labor line here, not from the picker.
            _provenancePrice: Number(s.labor_line.unit_price || 0),
          }
        : src === 'attested'
          ? {
              labor_source: 'attested',
              _provenancePrice: Number(s.labor_line.unit_price || 0),
              ...(s.labor_line.man_hours != null
                ? { estimated_man_hours: Number(s.labor_line.man_hours) } : {}),
            }
          : {};
      form.value.line_items = [{
        description: s.labor_line.description,
        quantity: Number(s.labor_line.quantity || 1) || 1,
        unit_price: Number(s.labor_line.unit_price || 0),
        // Labor is non-taxable — same rule the estimate prefill applies.
        taxable: false,
        category: 'Labor',
        cost: null,
        margin_pct_override: null,
        ...provenance,
      }];
    }
  } catch (e) {
    // closeout prefill is best-effort — a blank editor is the old behavior
  }
}

async function prefillFromJobEstimate(jobId) {
  if (!jobId) return;
  try {
    const list = await api.get(
      `/api/estimates?job_id=${encodeURIComponent(jobId)}`,
      { suppressErrorToast: true },
    );
    const all = Array.isArray(list) ? list : Array.isArray(list?.data) ? list.data : [];
    // §15.1 (2026-08-08 audit): only an ACCEPTED estimate outranks the
    // closeout lanes — this used to take the LATEST estimate regardless of
    // status, prefilling prices from a draft or even a DECLINED estimate
    // the customer never agreed to (and blocking the closeout labor
    // prefill, which only fills an empty editor).
    const estimates = all.filter((e) => String(e.status || '').toLowerCase() === 'accepted');
    if (!estimates.length) {
      // A previously-picked job may have set this; clearing matters because
      // job changes re-run the prefill and a stale id would credit the wrong
      // estimate.
      sourceEstimateId.value = null;
      return;
    }
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
      // A margin persisted on an ESTIMATE line is already a human decision —
      // EstimateView only writes that column when the operator set one. Stamp
      // it so the POST gate below forwards it instead of dropping a real
      // override on the estimate → invoice carry.
      //
      // `_marginPersisted`, not `_marginUserEdited`: the latter doubles as the
      // recompute flag and is cleared by `markPriceOverride`, so gating on it
      // meant a price edit erased the carried margin.
      // Both flags — see InvoiceDetailView.enterEditMode for why. Stamping
      // only _marginPersisted lets recomputeSell refill from the tier and then
      // persist that fill over the estimate's real override.
      _marginPersisted: ln.margin_pct_override != null,
      _marginUserEdited: ln.margin_pct_override != null,
    }));
    // Only claim provenance once lines actually landed — an estimate that
    // yielded no lines did not price this invoice.
    sourceEstimateId.value = latest.id;
    if (!form.value.notes) form.value.notes = est.description || est.notes || '';
  } catch (e) {
    // estimate prefill is best-effort
  }
}

async function createInvoice() {
  creating.value = true;
  try {
    // 2026-08-08 audit: this filter silently DROPPED operator-typed $0
    // lines (warranty/no-charge items) while machine-generated $0 lines
    // sent fine elsewhere. Keep every described line; the tenant's
    // zero-price catalog policy (block/warn) is the arbiter server-side,
    // and a block surfaces as a visible 422 instead of a silent vanish.
    const lineItems = form.value.line_items
      .filter((l) => l.description && toNum(l.unit_price) >= 0)
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
        // An OVERRIDE is a margin a human chose. The editor also AUTO-FILLS
        // this column with the tier-implied margin so the operator can see
        // what they're running at — that fill is not a choice, and posting it
        // records "the operator set this margin" for a number nobody set.
        // `routers/invoices.py` runs no pricing engine: it stores this value
        // as a snapshot on the line, so the false override is not re-priced
        // away later — it becomes the invoice's permanent record of a decision
        // that never happened, and it is what the margin report reads back.
        //
        // `_marginUserEdited` is the editor's own flag for "a human committed
        // a value here", and EstimateView has always gated on it
        // (EstimateView.vue `_linePostPayload`). This surface did not — it
        // leaked on every cost edit, and would now leak on every costed
        // catalog add too, since those run the tier fill.
        if (
          (l._marginUserEdited || l._marginPersisted)
          && l.margin_pct_override != null
          && toNum(l.margin_pct_override) > 0
        ) {
          // Form is in percent (e.g. 35), backend expects decimal (0.35).
          out.margin_pct_override = toNum(l.margin_pct_override) / 100;
        }
        // D-S122-line-removal-unbill — forward the part_id so the backend
        // can store the linkage and release the part on line-delete.
        if (l.part_id) out.part_id = l.part_id;
        // Only forward when ticked: the contract defaults it False, and
        // sending it unconditionally would put a flag on every historical-
        // shaped payload for no reason.
        if (l.includes_labor) out.includes_labor = true;
        // Labor provenance (migration 071) — WHICH lane priced this line.
        // The contract rejects labor_source='matrix' without an id, and an id
        // without a source, so send them together or not at all.
        if (l.labor_source) {
          out.labor_source = l.labor_source;
          if (l.labor_price_item_id) out.labor_price_item_id = l.labor_price_item_id;
          if (l.estimated_man_hours != null && toNum(l.estimated_man_hours) > 0) {
            out.estimated_man_hours = toNum(l.estimated_man_hours);
          }
        }
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
      // Only on job-linked invoices — the contract rejects photo ids without a
      // job, and a counter sale has no job whose photos could print.
      attached_photo_ids: form.value.job_id ? (form.value.attached_photo_ids || []) : [],
    };
    if (toNum(form.value.discount) > 0) payload.discount = toNum(form.value.discount);
    if (adjustsInvoiceId.value) payload.adjusts_invoice_id = adjustsInvoiceId.value;
    // Provenance only. NOT `estimate_id` — that tells the server to copy the
    // estimate's lines and ignore ours, which would discard whatever the
    // operator just edited.
    if (sourceEstimateId.value) payload.source_estimate_id = sourceEstimateId.value;

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
    await prefillFromJobCloseout(qJobId);
    // The deep-linked path (BillingView's "Create invoice" on a job row)
    // never runs onJobChange, so the picker has to be filled here too — this
    // is THE path the office takes from Ready-for-Billing.
    await loadJobPhotos(qJobId);
  }
  // §12 supplemental: BillingView's "Create supplemental" deep-link passes the
  // original invoice id (and number, for the banner). We record it as
  // provenance on the new invoice — the office still confirms the lines/amount.
  if (q.adjusts_invoice_id) {
    adjustsInvoiceId.value = String(q.adjusts_invoice_id);
    adjustsInvoiceNumber.value = q.adjusts_invoice_number ? String(q.adjusts_invoice_number) : '';
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
/* Job-photo picker — same shape as the invoice detail page's picker, so the
   two surfaces read as one feature rather than two. Theme variables only:
   this has to hold in dark mode. */
.photo-pick-grid { display: flex; flex-wrap: wrap; gap: 0.75rem; }
.photo-pick {
  display: flex; flex-direction: column; gap: 0.35rem; width: 140px;
  padding: 0.4rem; cursor: pointer;
  border: 1px solid var(--p-content-border-color); border-radius: 6px;
}
.photo-pick.selected { border-color: var(--p-primary-color); }
.photo-pick :deep(img), .photo-pick-thumb {
  width: 100%; height: 96px; object-fit: cover; border-radius: 4px; display: block;
}
.photo-pick-failed {
  display: flex; align-items: center; justify-content: center;
  width: 100%; height: 96px; border-radius: 4px;
  background: var(--p-content-background); color: var(--p-text-muted-color);
  font-size: 0.7rem; text-align: center;
}
.photo-pick-meta { font-size: 0.75rem; color: var(--p-text-muted-color); word-break: break-word; }
/* The tech's closeout context above the line editor. */
.closeout-context {
  border: 1px solid var(--p-content-border-color, var(--border));
  border-left: 3px solid var(--p-primary-color);
  border-radius: 6px;
  padding: 0.6rem 0.9rem;
}
.closeout-context-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}
/* Theme tokens only — this has to stay legible in dark mode, where a
   hardcoded warning colour on a dark card goes unreadable. */
.closeout-dupe-warning {
  margin-top: 0.6rem;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--p-orange-500, #f97316);
  background: var(--p-content-hover-background, rgba(127, 127, 127, 0.08));
  border-radius: 4px;
}
.closeout-context-row {
  margin-top: 0.25rem;
}
.closeout-context-notes {
  margin: 0.35rem 0 0;
  color: var(--p-text-muted-color);
  white-space: pre-line;
}
.tech-note-row {
  padding: 0.25rem 0;
}
.tech-note-row + .tech-note-row {
  border-top: 1px dashed var(--p-content-border-color, var(--border));
}
.tech-note-actions {
  display: inline-flex;
  gap: 0.25rem;
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
