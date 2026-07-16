<!--
  LineItemEditor — shared line-items editor for invoice / estimate / proposal / future
  surfaces. Extracted from EstimateView/BillingView lineage S122.

  Contract:
    v-model:lines       → array of line objects (parent owns the array).
                          Line shape: { description, quantity, unit_price,
                          taxable?, category?, cost?, line_total? } — extra
                          fields are passed through untouched.
    v-model:fromPartIds → cumulative list of JobPartNeeded.id strings the user
                          has pulled into lines from the parts-from-job
                          checklist. Parent passes this to the API on submit.
    :categories         → optional [{label, value}] for the per-line Category
                          select. Hidden when not provided.
    :job-id             → when set, fetches /api/jobs/:job-id/parts-needed and
                          renders a parts-from-job checklist above the line
                          table.
    :show-taxable       → render the per-line taxable checkbox (invoice mode).
    :show-cost          → render the cost column (estimate mode).
    :show-margin        → render the margin column (estimate mode).
    :catalog-endpoint   → URL for the catalog picker. Defaults to /api/catalogs
                          aggregated. Pass a different URL for tenant-scoped
                          catalogs.

  Why a single component instead of two: BillingView's dialog and EstimateView
  reimplemented the same line table twice; their parts-from-job story was
  missing entirely. Sharing the logic also means the taxable + tax-rate
  semantics added in S122 land in both surfaces at the same time.
-->
<template>
  <div class="line-item-editor-root">
    <!-- Parts-from-job error banner (S122 D-3) -->
    <div
      v-if="jobId && partsPanelError === 'forbidden'"
      class="parts-from-job parts-from-job-locked"
      data-testid="parts-from-job-forbidden"
    >
      <i class="pi pi-lock" /> Your role can't see parts on this job —
      <small class="muted">ask an admin for `inventory.read` to pull
      parts into invoices.</small>
    </div>

    <!-- Parts-from-job checklist (S122) -->
    <div
      v-if="jobId && partsFromJob.length"
      class="parts-from-job"
      data-testid="parts-from-job-panel"
    >
      <div class="parts-from-job-header">
        <span class="parts-from-job-title">
          Parts on this job
          <small class="muted">({{ partsFromJob.length }} unbilled)</small>
        </span>
        <Button
          label="Add Selected"
          icon="pi pi-plus"
          size="small"
          text
          severity="info"
          :disabled="!anySelected"
          data-testid="parts-from-job-add"
          @click="addSelectedParts"
        />
      </div>
      <div
        v-for="part in partsFromJob"
        :key="part.id"
        class="parts-from-job-row"
        :data-testid="`parts-from-job-row-${part.id}`"
      >
        <input
          type="checkbox"
          :id="`part-check-${part.id}`"
          :checked="selectedPartIds.includes(part.id)"
          :data-testid="`parts-from-job-check-${part.id}`"
          @change="togglePart(part.id, $event.target.checked)"
        />
        <label :for="`part-check-${part.id}`" class="parts-from-job-name">
          {{ part.part_name }}
          <small v-if="part.sku" class="muted">· {{ part.sku }}</small>
        </label>
        <span class="parts-from-job-qty">×{{ part.quantity || 1 }}</span>
        <span
          v-if="part.source === 'vendor_invoice'"
          class="status-pill status-vendor-bill"
          :data-testid="`parts-from-job-badge-${part.id}`"
        >vendor bill<template v-if="part.supplier"> · {{ part.supplier }}</template></span>
        <span
          v-else-if="part.status === 'ordered'"
          class="status-pill status-ordered"
          :data-testid="`parts-from-job-badge-${part.id}`"
        >ordered, not received</span>
        <span
          v-else-if="part.status === 'received'"
          class="status-pill status-received"
          :data-testid="`parts-from-job-badge-${part.id}`"
        >received</span>
        <!-- PR4-billing-capture: provenance badge for tech-attested usage —
             closeout / mobile / van captures that used to leak. -->
        <span
          v-else-if="part.status === 'used'"
          class="status-pill status-used"
          :data-testid="`parts-from-job-badge-${part.id}`"
        >used · {{ part.source || 'closeout' }}</span>
      </div>
    </div>

    <!-- Line items table -->
    <div class="line-items-editor" data-testid="line-items-editor">
      <div class="line-item-header" :style="gridStyle">
        <span class="col-action"></span>
        <span v-if="categories.length" class="col-cat">Category</span>
        <span class="col-desc">Description</span>
        <span class="col-qty">Qty</span>
        <span v-if="showCost" class="col-cost">Cost</span>
        <span class="col-price">Unit Price</span>
        <span v-if="showTaxable" class="col-taxable">Taxable</span>
        <span v-if="showMargin" class="col-margin">Margin</span>
        <span class="col-total">Total</span>
        <span class="col-action"></span>
      </div>
      <div
        v-for="(item, idx) in localLines"
        :key="idx"
        class="line-item-row"
        :style="gridStyle"
      >
        <Button
          icon="pi pi-trash"
          aria-label="Delete line"
          v-tooltip="'Delete line'"
          severity="danger"
          text
          size="small"
          class="col-action"
          :data-testid="`line-delete-${idx}`"
          @click="removeLineAt(idx)"
        />
        <Select
          v-if="categories.length"
          v-model="item.category"
          :options="categories"
          optionLabel="label"
          optionValue="value"
          placeholder="Category"
          class="col-cat"
          :data-testid="`line-cat-${idx}`"
          @change="onCategoryChange(item)"
        />
        <InputText
          v-model="item.description"
          placeholder="Description"
          class="col-desc"
          :data-testid="`line-desc-${idx}`"
          @update:modelValue="emitLines"
        />
        <InputNumber
          v-model="item.quantity"
          :min="1"
          :useGrouping="false"
          class="col-qty"
          :data-testid="`line-qty-${idx}`"
          @update:modelValue="emitLines"
        />
        <InputNumber
          v-if="showCost"
          v-model="item.cost"
          mode="currency"
          currency="USD"
          locale="en-US"
          :min="0"
          class="col-cost"
          :data-testid="`line-cost-${idx}`"
          @update:modelValue="onCostChange(item)"
        />
        <InputNumber
          v-model="item.unit_price"
          mode="currency"
          currency="USD"
          locale="en-US"
          :min="0"
          class="col-price"
          :data-testid="`line-price-${idx}`"
          @update:modelValue="markPriceOverride(item)"
        />
        <span
          v-if="showTaxable"
          class="col-taxable"
        >
          <input
            type="checkbox"
            :checked="item.taxable !== false"
            :data-testid="`line-taxable-${idx}`"
            @change="setTaxable(idx, $event.target.checked)"
          />
        </span>
        <InputNumber
          v-if="showMargin"
          v-model="item.margin_pct_override"
          suffix="%"
          :min="0"
          :max="99"
          :maxFractionDigits="1"
          placeholder="tier"
          class="col-margin"
          :data-testid="`line-margin-${idx}`"
          @update:modelValue="onMarginOverrideChange(item)"
        />
        <span class="col-total line-total-display" :data-testid="`line-total-${idx}`">
          {{ currency(toNum(item.quantity) * toNum(item.unit_price)) }}
        </span>
        <Button
          icon="pi pi-clone"
          aria-label="Duplicate line"
          v-tooltip="'Duplicate line'"
          text
          size="small"
          class="col-action"
          :data-testid="`line-copy-${idx}`"
          @click="duplicateLineAt(idx)"
        />
      </div>
      <div class="line-item-buttons">
        <Button
          label="Add Line"
          icon="pi pi-plus"
          text
          size="small"
          data-testid="line-add-btn"
          @click="addLine"
        />
        <Button
          label="Add from Catalog"
          icon="pi pi-book"
          text
          size="small"
          severity="info"
          data-testid="line-add-catalog-btn"
          @click="showCatalogPicker = true"
        />
      </div>
      <div class="line-items-subtotal" data-testid="line-items-subtotal">
        Subtotal: <strong>{{ currency(subtotal) }}</strong>
      </div>
    </div>

    <!-- Shared catalog picker (one tab per real catalog) -->
    <CatalogPickerDialog v-model:visible="showCatalogPicker" @add="addFromCatalog" />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Select from 'primevue/select';
import CatalogPickerDialog from './CatalogPickerDialog.vue';
import { useApi } from '../composables/useApi';

const props = defineProps({
  lines: { type: Array, default: () => [] },
  fromPartIds: { type: Array, default: () => [] },
  categories: { type: Array, default: () => [] },
  jobId: { type: String, default: null },
  showTaxable: { type: Boolean, default: false },
  showCost: { type: Boolean, default: false },
  showMargin: { type: Boolean, default: false },
  catalogEndpoint: { type: String, default: '/api/catalogs' },
});

const emit = defineEmits(['update:lines', 'update:fromPartIds']);

const api = useApi();

// Local mirror of the lines array so editing per-row inputs doesn't fight the
// parent's reactive ref. We emit back on every change.
const localLines = ref(cloneLines(props.lines));
watch(() => props.lines, (next) => {
  if (next !== localLines.value) localLines.value = cloneLines(next);
}, { deep: false });

function cloneLines(arr) {
  return Array.isArray(arr) ? arr.map((l) => ({ ...l })) : [];
}

function emitLines() {
  emit('update:lines', localLines.value.map((l) => ({ ...l })));
}

function defaultLine() {
  const base = { description: '', quantity: 1, unit_price: 0 };
  if (props.showTaxable) base.taxable = true;
  if (props.categories.length) base.category = null;
  if (props.showCost) base.cost = null;
  if (props.showMargin) {
    base.margin_pct_override = null;
    base._priceOverridden = false;
    base._marginUserEdited = false;
    base._suppressMarginUserEdit = false;
  }
  return base;
}

function addLine() {
  localLines.value.push(defaultLine());
  emitLines();
}

function removeLineAt(idx) {
  localLines.value.splice(idx, 1);
  emitLines();
}

// D-S122-line-editor-proposals auditor catch: preserve the per-line Copy
// affordance that ProposalsView had before migration. Multi-door installs
// (common GDX workflow) duplicate a line then edit qty/spec — keeping this
// is a real productivity feature, not polish.
function duplicateLineAt(idx) {
  const src = localLines.value[idx];
  if (!src) return;
  // Shallow clone — strip any id-like fields so the copy is treated as a
  // new line by callers that key on id (estimate save path uses `id` to
  // distinguish updates from inserts).
  const { id, _key, tempId, part_id, ...rest } = src;
  localLines.value.splice(idx + 1, 0, { ...rest });
  emitLines();
}

function setTaxable(idx, value) {
  localLines.value[idx] = { ...localLines.value[idx], taxable: !!value };
  emitLines();
}

// Pricing tiers — retail-only by design, matching EstimateView.vue:737. When
// pricing_class differentiation lands across the app, drop the retail filter
// on both surfaces in the same slice.
const tierSetsByCategory = ref({});

// `estimates_allow_line_margin_override` is the per-tenant policy switch from
// SettingsView — when false, line-level margin override is ignored and tier
// always wins, and the margin column is not auto-filled. EstimateView gates
// every override-vs-tier branch on this; carrying the same gate keeps invoice
// + estimate surfaces consistent so a tenant who disables override on
// estimates also disables it on invoices.
const editorFeatures = ref({ estimates_allow_line_margin_override: true });

async function loadPricingTiers() {
  if (!props.showCost || !props.showMargin) return;
  try {
    const sets = await api.get('/api/pricing-engine/tier-sets', { suppressErrorToast: true });
    const byCat = {};
    for (const s of sets || []) {
      if (s.pricing_class !== 'retail') continue;
      byCat[s.pricing_category] = (s.tiers || []).slice().sort(
        (a, b) => (Number(a.cost_min) || 0) - (Number(b.cost_min) || 0),
      );
    }
    tierSetsByCategory.value = byCat;
  } catch {
    tierSetsByCategory.value = {};
  }
}

async function loadEditorFeatures() {
  if (!props.showCost || !props.showMargin) return;
  try {
    const f = await api.get('/api/estimates-features', { suppressErrorToast: true });
    if (f) editorFeatures.value = f;
  } catch { /* default permissive */ }
}

function categoryToPricingCategory(cat) {
  const c = (cat || '').toLowerCase();
  if (c === 'springs') return 'parts';
  if (['doors', 'openers', 'parts', 'labor', 'other'].includes(c)) return c;
  return 'other';
}

function findTierMargin(pricingCategory, cost) {
  // Reject cost <= 0 (not just < 0). Zero-cost matches the open-ended bottom
  // tier and writes a fake-looking margin into the column while sell = 0/x =
  // 0 leaves the button disabled. Auditor catch 2026-05-12 — keep zero out
  // of the tier lookup so the operator gets the "type a cost first" cue
  // rather than a misleading 35.0 in the margin column.
  const tiers = tierSetsByCategory.value[pricingCategory];
  if (!tiers || tiers.length === 0 || cost == null || cost <= 0) return null;
  const match = tiers.find((t) =>
    cost >= Number(t.cost_min ?? 0)
    && (t.cost_max == null || cost < Number(t.cost_max)),
  );
  return match ? Number(match.margin_pct) : null;
}

// Estimate-parity recompute. When the operator types a cost and leaves margin
// blank, fall through to the tier table for the line's category and auto-fill
// both unit_price AND the margin column so the operator sees the tier-implied
// %. Mirrors EstimateView.vue:651-679 exactly, gated on the editor having
// both cost + margin columns visible.
function recomputeSell(item) {
  if (!props.showCost || !props.showMargin) return;
  if (item._priceOverridden) return;
  const cost = Number(item.cost) || 0;
  const override = Number(item.margin_pct_override);
  let margin;
  if (
    editorFeatures.value.estimates_allow_line_margin_override
    && item._marginUserEdited
    && Number.isFinite(override)
    && override > 0
    && override < 100
  ) {
    margin = override / 100;
  } else {
    const pc = categoryToPricingCategory(item.category);
    margin = findTierMargin(pc, Number(item.cost));
    if (margin != null && margin < 1
        && editorFeatures.value.estimates_allow_line_margin_override) {
      const pct = Math.round(margin * 1000) / 10;
      if (Number(item.margin_pct_override) !== pct) {
        item._suppressMarginUserEdit = true;
        item.margin_pct_override = pct;
      }
    }
  }
  if (margin == null || margin >= 1) return;
  const sell = cost / (1 - margin);
  item.unit_price = Math.round(sell * 100) / 100;
}

function onCostChange(item) {
  recomputeSell(item);
  emitLines();
}

function onCategoryChange(item) {
  // Category drives tier-set selection. Clear the user-edited margin flag so
  // a re-categorized line picks up the new category's tier instead of
  // sticking to a margin the operator last typed under the old category.
  item._marginUserEdited = false;
  recomputeSell(item);
  emitLines();
}

function onMarginOverrideChange(item) {
  // Suppression flag set by recomputeSell after a programmatic margin write —
  // don't treat that round-trip as a user edit.
  if (item._suppressMarginUserEdit) {
    item._suppressMarginUserEdit = false;
    emitLines();
    return;
  }
  if (item.margin_pct_override == null || item.margin_pct_override === '') {
    item._marginUserEdited = false;
    item._priceOverridden = false;
    recomputeSell(item);
    emitLines();
    return;
  }
  item._marginUserEdited = true;
  item._priceOverridden = false;
  recomputeSell(item);
  emitLines();
}

function markPriceOverride(item) {
  // Operator typed a price directly. Decide whether to treat it as an
  // override vs the tier-implied price, and reflect the *actual* margin in
  // the Margin column so the operator sees what they'll really run at.
  const pc = categoryToPricingCategory(item.category);
  const tierMargin = findTierMargin(pc, Number(item.cost));
  const cost = Number(item.cost);
  const sell = Number(item.unit_price);
  if (tierMargin == null || item.cost == null) {
    item._priceOverridden = false;
  } else {
    const expected = (cost || 0) / (1 - tierMargin);
    item._priceOverridden = Math.abs(expected - (sell || 0)) > 0.01;
  }
  if (editorFeatures.value.estimates_allow_line_margin_override
      && cost > 0 && sell > 0) {
    const actualPct = Math.round(((sell - cost) / sell) * 1000) / 10;
    if (Number(item.margin_pct_override) !== actualPct) {
      item._suppressMarginUserEdit = true;
      item.margin_pct_override = actualPct;
    }
    item._marginUserEdited = false;
  }
  emitLines();
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function currency(v) {
  return `$${toNum(v).toFixed(2)}`;
}

const subtotal = computed(() =>
  localLines.value.reduce((sum, l) => sum + toNum(l.quantity) * toNum(l.unit_price), 0),
);

// Browser-walk fix (2026-05-11): grid-template-columns must match the actual
// cells rendered, which varies by flags. Build the column track list to match
// the conditional <span>/<input> order in the template (left action + cat? +
// desc + qty + cost? + price + taxable? + margin? + total + right action).
const gridStyle = computed(() => {
  const cols = ['36px'];                         // left action (delete)
  if (props.categories.length) cols.push('130px');  // category select
  cols.push('minmax(180px, 1fr)');               // description
  cols.push('80px');                              // qty
  if (props.showCost) cols.push('110px');         // cost
  cols.push('110px');                              // unit price
  if (props.showTaxable) cols.push('70px');       // taxable
  if (props.showMargin) cols.push('90px');        // margin override
  cols.push('100px');                              // total
  cols.push('36px');                               // right action (copy)
  return { gridTemplateColumns: cols.join(' ') };
});

// ---------------------------------------------------------------------------
// Parts-from-job (S122)
// ---------------------------------------------------------------------------

const partsFromJob = ref([]);
const selectedPartIds = ref([]);
// D-S122-parts-panel-silent-hide: distinguish "no permission" from "no
// parts". When the API returns 403, show a small banner instead of silently
// hiding the panel — otherwise office staff w/o inventory.read have no
// visible cue that the picker exists.
const partsPanelError = ref(null);  // null | 'forbidden' | 'failed'

async function loadPartsFromJob() {
  partsPanelError.value = null;
  if (!props.jobId) {
    partsFromJob.value = [];
    selectedPartIds.value = [];
    return;
  }
  try {
    // PR4-billing-capture: 'used' rows are the closeout/mobile/van captures
    // that previously never reached this checklist — the structural leak.
    const url = `/api/jobs/${encodeURIComponent(props.jobId)}/parts-needed?status=ordered,received,used&unbilled=true`;
    const r = await api.get(url, { suppressErrorToast: true });
    const list = Array.isArray(r) ? r : Array.isArray(r?.data) ? r.data : [];
    partsFromJob.value = list;
    // Pre-check received parts AND tech-attested used parts;
    // ordered-but-not-received parts default off (office sees them but
    // decides per-part whether to bill in advance).
    // [vendor-invoice-intake AUDIT-R2] vendor-bill-sourced rows arrive
    // UNCHECKED — a special-order door is usually already on the estimate, so
    // the office must add it deliberately (else it lands as a duplicate/$0
    // line). They're still shown + badged so they can't be missed.
    selectedPartIds.value = list
      .filter((p) => (p.status === 'received' || p.status === 'used') && p.source !== 'vendor_invoice')
      .map((p) => p.id);
  } catch (e) {
    partsFromJob.value = [];
    selectedPartIds.value = [];
    const status = e?.status ?? e?.response?.status;
    if (status === 401 || status === 403) {
      partsPanelError.value = 'forbidden';
    } else {
      partsPanelError.value = 'failed';
    }
  }
}

watch(() => props.jobId, () => { loadPartsFromJob(); }, { immediate: true });

onMounted(() => {
  loadPricingTiers();
  loadEditorFeatures();
});

const anySelected = computed(() => selectedPartIds.value.length > 0);

function togglePart(partId, checked) {
  if (checked) {
    if (!selectedPartIds.value.includes(partId)) selectedPartIds.value.push(partId);
  } else {
    selectedPartIds.value = selectedPartIds.value.filter((id) => id !== partId);
  }
}

async function addSelectedParts() {
  const picked = partsFromJob.value.filter((p) => selectedPartIds.value.includes(p.id));
  if (!picked.length) return;

  // Price preference (PR4): the capture-time catalog sell price on the row
  // wins; else enrich via sku-suggest; else 0 — operator types the price.
  const enriched = await Promise.all(picked.map(async (p) => {
    let unitPrice = Number(p.unit_price) > 0 ? Number(p.unit_price) : 0;
    if (!unitPrice && p.sku) {
      try {
        const url = `/api/parts-needed/sku-suggest?q=${encodeURIComponent(p.sku)}&limit=4`;
        const sug = await api.get(url, { suppressErrorToast: true });
        const matches = Array.isArray(sug) ? sug : Array.isArray(sug?.data) ? sug.data : [];
        const hit = matches.find((m) => (m.sku || '').toLowerCase() === p.sku.toLowerCase());
        if (hit && Number(hit.price) > 0) unitPrice = Number(hit.price);
      } catch (e) {
        // suppressErrorToast: tech sans inventory.read just leaves price at 0.
      }
    }
    return {
      description: p.part_name,
      quantity: Number(p.quantity) || 1,
      unit_price: unitPrice,
      // D-S122-line-removal-unbill: stamp the part's ID on the line so the
      // backend can release the part when this line is later deleted.
      part_id: p.id,
      ...(props.showTaxable ? { taxable: true } : {}),
      ...(props.categories.length ? { category: 'Parts' } : {}),
      ...(props.showCost ? { cost: null } : {}),
      ...(props.showMargin ? { margin_pct_override: null } : {}),
    };
  }));

  // If the editor currently shows a single empty placeholder line, replace it.
  const lines = localLines.value;
  const onlyEmpty = lines.length === 1 && !lines[0].description && !toNum(lines[0].unit_price);
  if (onlyEmpty) lines.splice(0, 1);
  enriched.forEach((l) => lines.push(l));

  // Update from-part-ids (cumulative).
  const existing = new Set(props.fromPartIds || []);
  picked.forEach((p) => existing.add(p.id));
  emit('update:fromPartIds', Array.from(existing));

  // Remove the just-added parts from the panel so they can't be added twice
  // within the same session. Backend's unbilled filter will exclude them on
  // re-fetch too.
  const pickedIds = new Set(picked.map((p) => p.id));
  partsFromJob.value = partsFromJob.value.filter((p) => !pickedIds.has(p.id));
  selectedPartIds.value = selectedPartIds.value.filter((id) => !pickedIds.has(id));

  emitLines();
}

// ---------------------------------------------------------------------------
// Catalog picker
// ---------------------------------------------------------------------------

// Catalog picking is handled by the shared <CatalogPickerDialog>, which shows
// one tab per real catalog. We only own the open/close flag and turn the items
// it emits into invoice line rows.
const showCatalogPicker = ref(false);

function addFromCatalog(items) {
  const lines = localLines.value;
  const onlyEmpty = lines.length === 1 && !lines[0].description && !toNum(lines[0].unit_price);
  if (onlyEmpty) lines.splice(0, 1);
  for (const item of items) {
    lines.push({
      description: item.description || item.name,
      quantity: 1,
      unit_price: Number(item.price) || 0,
      ...(props.showTaxable ? { taxable: true } : {}),
      ...(props.categories.length ? { category: item.category || null } : {}),
      // Carry the catalog item's cost + pricing bucket so the backend tier
      // engine computes the marked-up sell price. Without these the line was
      // posted at the catalog price (= cost for imports → zero markup).
      ...(props.showCost ? { cost: Number(item.cost) || null } : {}),
      ...(item.pricing_category ? { pricing_category: item.pricing_category } : {}),
      ...(props.showMargin ? { margin_pct_override: null } : {}),
    });
  }
  emitLines();
}
</script>

<style scoped>
.line-item-editor-root {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.parts-from-job {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  background: var(--p-surface-50, #fafafa);
}
.parts-from-job-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.25rem;
}
.parts-from-job-title {
  font-weight: 600;
}
.parts-from-job-row {
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  gap: 0.5rem;
  align-items: center;
  padding: 0.25rem 0;
}
.parts-from-job-name {
  cursor: pointer;
  user-select: none;
}
.parts-from-job-qty {
  color: var(--p-text-muted-color, #6b7280);
}
.status-pill {
  font-size: 0.75rem;
  padding: 0.125rem 0.5rem;
  border-radius: 999px;
}
.status-ordered {
  background: #fef3c7;
  color: #92400e;
}
.status-received {
  background: #d1fae5;
  color: #065f46;
}
.status-vendor-bill {
  background: #ede9fe;
  color: #5b21b6;
}
.status-used {
  background: #dbeafe;
  color: #1e40af;
}
.parts-from-job-locked {
  background: #fef3c7;
  color: #92400e;
}

.line-items-editor {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 6px;
  padding: 0.5rem;
  overflow-x: auto;
}
.line-item-header,
.line-item-row {
  display: grid;
  /* grid-template-columns set inline via :style="gridStyle" (computed). */
  gap: 0.5rem;
  align-items: center;
  padding: 0.25rem 0;
}
.line-item-header {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--p-text-muted-color, #6b7280);
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  padding-bottom: 0.5rem;
  margin-bottom: 0.25rem;
}
.line-total-display {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.col-action {
  display: flex;
  align-items: center;
  justify-content: center;
}
.col-taxable {
  text-align: center;
}
/* Make PrimeVue inputs fill their grid cell so the layout stays predictable
   regardless of input's intrinsic width. */
.line-item-row :deep(.p-inputtext),
.line-item-row :deep(.p-inputnumber),
.line-item-row :deep(.p-inputnumber input),
.line-item-row :deep(.p-select) {
  width: 100%;
  min-width: 0;
}
.line-item-buttons {
  display: flex;
  gap: 0.5rem;
  padding-top: 0.5rem;
}
.line-items-subtotal {
  text-align: right;
  padding-top: 0.5rem;
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  margin-top: 0.5rem;
}
.muted {
  color: var(--p-text-muted-color, #6b7280);
}
.w-full {
  width: 100%;
}
</style>
