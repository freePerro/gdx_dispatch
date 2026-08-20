<!--
  LineItemEditor — shared line-items editor for invoice / estimate / future
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

    <!-- The install, billed twice: a part whose price already covers it
         sitting alongside a labor line. Nothing in the data marks a bundled
         part, so this fires off the office's own tick. Advisory only --
         billing both is legitimate when the job ran past what the bundle
         covers, and the office is the one who knows. -->
    <div
      v-if="doubleBilledInstall"
      class="install-double-bill"
      data-testid="install-double-bill-warning"
    >
      <strong>The install may be on this invoice twice.</strong>
      <span>
        {{ doubleBilledInstall.bundled.join(', ') }}
        {{ doubleBilledInstall.bundled.length === 1 ? 'is priced' : 'are priced' }}
        with the installation included, and this invoice also charges
        {{ doubleBilledInstall.labor.join(', ') }}.
      </span>
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
        <span
          v-if="showTaxable"
          class="col-taxable"
          title="Tick when this part's price already covers the installation. Billing it alongside a labor line charges the install twice."
        >Incl. install</span>
        <span v-if="showMargin" class="col-margin">Margin</span>
        <span class="col-total">Total</span>
        <span class="col-action"></span>
      </div>
      <div
        v-for="(item, idx) in localLines"
        :key="idx"
        class="line-item-row"
        :class="{ 'line-item-row-locked': isLocked(item) }"
        :style="gridStyle"
      >
        <template v-if="isLocked(item)">
          <span
            class="col-action locked-cell"
            v-tooltip="lockedTooltip"
            :data-testid="`line-locked-${idx}`"
          >
            <i class="pi pi-lock" />
          </span>
          <span v-if="categories.length" class="col-cat locked-cell">{{ item.category || '—' }}</span>
          <span class="col-desc locked-cell">{{ item.description }}</span>
          <span class="col-qty locked-cell">{{ toNum(item.quantity) }}</span>
          <span v-if="showCost" class="col-cost locked-cell"></span>
          <span class="col-price locked-cell">{{ currency(toNum(item.unit_price)) }}</span>
          <span v-if="showTaxable" class="col-taxable locked-cell">
            <input type="checkbox" :checked="item.taxable !== false" disabled />
          </span>
          <span v-if="showTaxable" class="col-taxable locked-cell">
            <input type="checkbox" :checked="!!item.includes_labor" disabled />
          </span>
          <span v-if="showMargin" class="col-margin locked-cell"></span>
          <span class="col-total line-total-display" :data-testid="`line-total-${idx}`">
            {{ currency(toNum(item.quantity) * toNum(item.unit_price)) }}
          </span>
          <span class="col-action"></span>
        </template>
        <template v-else>
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
          :options="optionsForLine(item)"
          optionLabel="label"
          optionValue="value"
          placeholder="Category"
          class="col-cat"
          :data-testid="`line-cat-${idx}`"
          @change="onCategoryChange(item)"
        />
        <!-- Description + its catalog-source pill share ONE grid cell. A
             sibling span here would become a 12th column and desync the
             gridTemplateColumns track list below. -->
        <span class="col-desc line-desc-cell">
          <InputText
            v-model="item.description"
            placeholder="Description"
            :data-testid="`line-desc-${idx}`"
            @update:modelValue="emitLines"
          />
          <span
            v-if="item._catalogName"
            class="status-pill catalog-source-pill"
            :title="`Added from the ${item._catalogName} catalog`"
            :data-testid="`line-source-${idx}`"
          >{{ item._catalogName }}</span>
        </span>
        <InputNumber
          v-model="item.quantity"
          :min="1"
          :useGrouping="false"
          class="col-qty"
          :data-testid="`line-qty-${idx}`"
          @update:modelValue="emitLines"
          @input="onQtyInput(item, $event)"
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
          @input="onCostInput(item, $event)"
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
          @input="onPriceInput(item, $event)"
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
        <span
          v-if="showTaxable"
          class="col-taxable"
        >
          <input
            type="checkbox"
            :checked="!!item.includes_labor"
            :data-testid="`line-includes-labor-${idx}`"
            aria-label="Price includes installation labor"
            @change="setIncludesLabor(idx, $event.target.checked)"
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
          @input="onMarginInput(item, $event)"
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
        </template>
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
        <Button
          v-if="showLabor"
          label="Add Labor"
          icon="pi pi-wrench"
          text
          size="small"
          severity="info"
          data-testid="line-add-labor-btn"
          @click="showLaborPicker = true"
        />
      </div>
      <div class="line-items-subtotal" data-testid="line-items-subtotal">
        Subtotal: <strong>{{ currency(subtotal) }}</strong>
      </div>
    </div>

    <!-- Shared catalog picker (one tab per real catalog) -->
    <CatalogPickerDialog v-model:visible="showCatalogPicker" @add="addFromCatalog" />

    <!-- Two-lane labor picker: matrix flat price OR the tech's attested hours -->
    <LaborPickerDialog
      v-if="showLabor"
      v-model:visible="showLaborPicker"
      :closeout="closeout"
      @add="addLaborLines"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Select from 'primevue/select';
import CatalogPickerDialog from './CatalogPickerDialog.vue';
import LaborPickerDialog from './LaborPickerDialog.vue';
import { useApi } from '../composables/useApi';
import {
  VALID_BUCKETS,
  categoryToPricingCategory,
  displayCategoryFor,
  isRenderableOption,
} from '../composables/useLineCategories';

const props = defineProps({
  lines: { type: Array, default: () => [] },
  fromPartIds: { type: Array, default: () => [] },
  categories: { type: Array, default: () => [] },
  jobId: { type: String, default: null },
  showTaxable: { type: Boolean, default: false },
  // Render the Add Labor button + two-lane picker. Invoice surfaces only:
  // EstimateView has its own long-standing matrix picker.
  showLabor: { type: Boolean, default: false },
  // Closeout-billing-suggestion payload, passed through to the picker so the
  // attested-hours lane can offer the tech's signed-off numbers.
  closeout: { type: Object, default: null },
  showCost: { type: Boolean, default: false },
  showMargin: { type: Boolean, default: false },
  catalogEndpoint: { type: String, default: '/api/catalogs' },
  // Rows matching this predicate render read-only with a lock icon — no
  // inputs, no delete, no duplicate. Used for the deposit-netting line on
  // final invoices: it mirrors money actually collected, and the server
  // 409s any edit/delete of it anyway.
  lockedPredicate: { type: Function, default: null },
  lockedTooltip: { type: String, default: "This line is locked and can't be edited" },
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
  // Seed `_lastPrice` HERE — one ingest point, not per call site.
  //
  // It is the baseline `markPriceOverride` compares against to tell a real
  // edit from PrimeVue's commit-on-every-blur echo. Setting it only inside
  // markPriceOverride meant no line ever had one before its FIRST price
  // commit, so the guard was skipped exactly when it was needed and the echo
  // still cleared `_priceOverridden` on a flat-priced labor line — after
  // which a cost edit rewrote the quoted price (650 -> 769.23, reproduced).
  //
  // Every line reaches the editor through here: v-model ingest, catalog and
  // parts adds, the labor picker, the closeout prefill, enterEditMode
  // snapshots and duplicates. Seeding at the source covers all of them and
  // cannot be forgotten by a new call site.
  return Array.isArray(arr)
    ? arr.map((l) => ({ ...l, _lastPrice: l._lastPrice ?? toNum(l.unit_price) }))
    : [];
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
    base._marginPersisted = false;
    base._autoMargin = null;
  }
  return base;
}

function addLine() {
  localLines.value.push({ ...defaultLine(), _lastPrice: 0 });
  emitLines();
}

function isLocked(item) {
  return typeof props.lockedPredicate === 'function' && Boolean(props.lockedPredicate(item));
}

/**
 * Options for ONE row's Category select.
 *
 * A PrimeVue Select whose model value matches no option renders its
 * placeholder, so a line already carrying a free-form category showed an empty
 * cell — the operator could not tell "no category" from "a category you can't
 * see", and picking anything silently discarded the stored value. Prod
 * `invoice_lines` really does hold `Accessories`, `Inventory`, `Operators` and
 * `Seals`, and the 2026-08-19 decision was to normalize at add-time and leave
 * existing rows alone — so unmatched values are permanent and have to render.
 */
function optionsForLine(item) {
  const stored = item?.category;
  if (!stored || isRenderableOption(stored, props.categories)) return props.categories;
  return [...props.categories, { label: `${stored} (as stored)`, value: stored }];
}

function removeLineAt(idx) {
  if (isLocked(localLines.value[idx])) return;
  localLines.value.splice(idx, 1);
  emitLines();
}

// D-S122-line-editor-proposals auditor catch: preserve the per-line Copy
// affordance the old ProposalsView had (that page was retired in migration
// 061, but this stayed). Multi-door installs (common GDX workflow) duplicate
// a line then edit qty/spec — keeping this is a real productivity feature,
// not polish.
function duplicateLineAt(idx) {
  const src = localLines.value[idx];
  if (!src || isLocked(src)) return;
  // Shallow clone — strip any id-like fields so the copy is treated as a
  // new line by callers that key on id (estimate save path uses `id` to
  // distinguish updates from inserts).
  // Strip transient recompute bookkeeping as well as identity. `_priceOverridden`
  // is the killer: it permanently suppresses recomputeSell, so a duplicate of a
  // price-overridden line would silently never re-price when its cost changed.
  // `_autoMargin` is blur-echo state that means nothing on a fresh row. The
  // margin VALUE and its persist flags are kept on purpose — a copy of an
  // override line is still an override line.
  const {
    id, _key, tempId, part_id, _priceOverridden, _autoMargin, ...rest
  } = src;
  localLines.value.splice(idx + 1, 0, { ...rest });
  emitLines();
}

function setTaxable(idx, value) {
  localLines.value[idx] = { ...localLines.value[idx], taxable: !!value };
  emitLines();
}

// Doug 2026-08-19: "sometimes the install price is in the part price", and
// the answer is a checkbox at billing time. Nothing in the catalog marks a
// bundled item -- only the words in its name -- so a human decides and the
// invoice records it.
function setIncludesLabor(idx, value) {
  localLines.value[idx] = { ...localLines.value[idx], includes_labor: !!value };
  emitLines();
}

// A line whose price already covers the install, sitting on the same invoice
// as a labor line, charges the customer for the install twice. Warn only --
// the office decides (there are real reasons to bill both, e.g. extra hours
// beyond what the bundle covers).
const doubleBilledInstall = computed(() => {
  const bundled = localLines.value.filter((l) => l && l.includes_labor);
  if (!bundled.length) return null;
  const labor = localLines.value.filter(
    (l) => l && !l.includes_labor && String(l.category || '').toLowerCase() === 'labor',
  );
  if (!labor.length) return null;
  return {
    bundled: bundled.map((l) => l.description || 'part').filter(Boolean),
    labor: labor.map((l) => l.description || 'labor').filter(Boolean),
  };
});

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

// categoryToPricingCategory now lives in composables/useLineCategories.js and
// mirrors the backend's `_derive_pricing_category` for the buckets that exist
// in code. The local copy that used to sit here only knew `springs→parts`, so
// 142 live `parts` catalog rows (hinges, rollers, drums, struts, fixtures…)
// fell through to the `other` tier and were over-priced by 10 points below
// $500. See the module header.

/**
 * The pricing bucket to look tiers up in, for ONE line.
 *
 * The item's own `pricing_category` wins when it has one. That field is what
 * the backend derived, what `pricing_tier_sets` is keyed on, and it is correct
 * on all 300 live catalog rows — so using it means the tier lookup no longer
 * depends on round-tripping through a human-facing display string.
 *
 * Mapping the display category is the FALLBACK, for hand-typed lines and
 * legacy rows that never had a bucket. `onCategoryChange` clears
 * `pricing_category` precisely so an operator who re-categorises a line gets
 * the new category's tier instead of the catalog's stale bucket.
 */
function bucketForLine(item) {
  const pc = String(item?.pricing_category || '').trim().toLowerCase();
  if (pc && VALID_BUCKETS.has(pc)) return pc;
  return categoryToPricingCategory(item?.category);
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
    const pc = bucketForLine(item);
    margin = findTierMargin(pc, Number(item.cost));
    if (margin != null && margin < 1
        && editorFeatures.value.estimates_allow_line_margin_override) {
      const pct = Math.round(margin * 1000) / 10;
      // Programmatic fill. InputNumber never emits update:modelValue for a
      // model write, so record the value instead — onMarginOverrideChange
      // uses _autoMargin to tell a blur echo / tab-through apart from a
      // genuine user override.
      item._autoMargin = pct;
      if (Number(item.margin_pct_override) !== pct) {
        item.margin_pct_override = pct;
      }
    }
  }
  if (margin == null || margin >= 1) return;
  const sell = cost / (1 - margin);
  item.unit_price = Math.round(sell * 100) / 100;
  // Programmatic write. Without moving the baseline, the next blur of the
  // (untouched) price field compares against a stale number and reads as a
  // real edit — which would falsely downgrade a matrix line to manual.
  item._lastPrice = item.unit_price;
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
  // A re-categorised line prices off the NEW tier, so any prior override no
  // longer describes it. (markPriceOverride deliberately does NOT clear this —
  // editing a price on an override line keeps it an override line.)
  item._marginPersisted = false;
  // The operator just chose a bucket by hand, so the catalog's own
  // pricing_category is no longer what this line is. Without this, re-filing a
  // hinge as "Doors" would keep pricing it off the `parts` tier and the change
  // would look like it did nothing.
  item.pricing_category = null;
  recomputeSell(item);
  emitLines();
}

function onMarginOverrideChange(item) {
  const v = item.margin_pct_override;
  if (v == null || v === '') {
    item._marginUserEdited = false;
    // Operator emptied the field — that IS a decision to drop the override.
    item._marginPersisted = false;
    item._priceOverridden = false;
    item._autoMargin = null;
    recomputeSell(item);
    emitLines();
    return;
  }
  // InputNumber commits on EVERY blur — changed or not — and programmatic
  // tier fills never emit at all. So a commit equal to the last auto-filled
  // value is a tab-through/blur echo, not a user override. (The old
  // _suppressMarginUserEdit flag waited for an emit that never came, then
  // swallowed the user's NEXT real edit — and tab-through froze the tier
  // margin as a fake override so cost edits stopped refreshing it.)
  if (item._autoMargin != null && Number(v) === Number(item._autoMargin)) {
    emitLines();
    return;
  }
  item._marginUserEdited = true;
  // Separate flag, deliberately. `_marginUserEdited` also drives the
  // tier-vs-override RECOMPUTE, and `markPriceOverride` clears it whenever the
  // operator retypes a price — correct for recompute, catastrophic for
  // persistence, because it made "edit the price" silently erase a stored
  // margin. `_marginPersisted` means only: this line's margin is a real
  // override and must be saved.
  item._marginPersisted = true;
  item._priceOverridden = false;
  item._autoMargin = null;
  recomputeSell(item);
  emitLines();
}

function markPriceOverride(item) {
  // A repriced matrix line is no longer matrix-quoted. The matrix said $650;
  // if a human typed $900, calling that "matrix quoted row X" credits the
  // matrix for a number nobody quoted — the exact falsehood
  // `_labor_provenance_for` guards against on the estimate-copy path. That
  // guard existed on the old path and not on the one this feature ships, so
  // it is applied here too. The row id is KEPT: "started from row X, then
  // repriced" is the true statement, and the API accepts manual-with-an-id
  // precisely so it can be said.
  //
  // Gated on the price ACTUALLY MOVING. PrimeVue's InputNumber commits
  // update:modelValue on EVERY blur, changed or not — the same blur-echo trap
  // `onMarginOverrideChange` documents below. Without this gate, tabbing
  // across the price field of an untouched matrix line rewrote its provenance
  // to "a human repriced this" and cleared `_priceOverridden`, which then let
  // a later cost edit rewrite a quoted flat price.
  // NOTHING below this line should run for a commit that did not change the
  // price. PrimeVue's InputNumber fires update:modelValue on EVERY blur,
  // changed or not — the blur-echo trap `onMarginOverrideChange` documents.
  // Letting the rest of this function run on an echo cleared
  // `_priceOverridden` on an untouched matrix labor line, and a later cost
  // edit then rewrote the quoted flat price (650 -> 769.23, observed).
  // Guarding only the downgrade fixed the label and left that live.
  if (
    item._lastPrice != null
    && Math.abs(toNum(item.unit_price) - toNum(item._lastPrice)) <= 0.005
  ) {
    return;
  }
  item._lastPrice = toNum(item.unit_price);

  // Same rule for BOTH priced lanes. A repriced attested line is no longer
  // "the tech's hours x the rate" either — that number came from a human, and
  // leaving it 'attested' dresses an office price up as tech-signed evidence,
  // which is the more dangerous direction of the two.
  if (
    (item.labor_source === 'matrix' || item.labor_source === 'attested')
    && item._provenancePrice != null
    && Math.abs(toNum(item.unit_price) - toNum(item._provenancePrice)) > 0.005
  ) {
    item.labor_source = 'manual';
  }
  // Operator typed a price directly. Decide whether to treat it as an
  // override vs the tier-implied price, and reflect the *actual* margin in
  // the Margin column so the operator sees what they'll really run at.
  const pc = bucketForLine(item);
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
    // Programmatic reflection of the actual margin — same _autoMargin
    // bookkeeping as the tier fill in recomputeSell.
    item._autoMargin = actualPct;
    if (Number(item.margin_pct_override) !== actualPct) {
      item.margin_pct_override = actualPct;
    }
    item._marginUserEdited = false;
  }
  emitLines();
}

// PrimeVue InputNumber only commits v-model on blur/Enter; its `input` event
// fires per keystroke with the parsed value. Assign it live so line totals,
// the margin column, and the parent's tax/profit math track WHILE the user
// types instead of appearing frozen until focus leaves the field.
function onQtyInput(item, e) {
  item.quantity = e.value;
  emitLines();
}

function onCostInput(item, e) {
  item.cost = e.value;
  onCostChange(item);
}

function onPriceInput(item, e) {
  item.unit_price = e.value;
  markPriceOverride(item);
}

function onMarginInput(item, e) {
  item.margin_pct_override = e.value;
  if (e.value == null) {
    // Mid-edit clear: reset the override flags but DEFER the tier refill to
    // the blur commit — refilling now would rewrite the field under the
    // user's cursor while they type a replacement number.
    item._marginUserEdited = false;
    item._priceOverridden = false;
    item._autoMargin = null;
    emitLines();
    return;
  }
  onMarginOverrideChange(item);
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
  // "Incl. install" rides with the taxable flag: both are invoice-mode-only
  // per-line money flags, and the grid track list must match the cells the
  // template actually renders (2026-05-11 browser-walk fix).
  if (props.showTaxable) cols.push('76px');       // includes_labor
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
        // job_id resolves the customer's pricing class server-side, so a
        // contractor or wholesale customer isn't quoted a retail margin.
        const jobQ = props.jobId ? `&job_id=${encodeURIComponent(props.jobId)}` : '';
        const url = `/api/parts-needed/sku-suggest?q=${encodeURIComponent(p.sku)}&limit=4${jobQ}`;
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
      _lastPrice: unitPrice,
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
const showLaborPicker = ref(false);

/**
 * Labor lines from the two-lane picker.
 *
 * Deliberately NOT run through `recomputeSell`: both lanes produce a price that
 * is already the answer — a quoted flat price, or attested hours x the rate —
 * and the tier engine has no labor tier anyway (the `labor` tier sets are
 * inactive on this tenant; labor prices from the matrix, not the engine).
 *
 * Taxability is resolved in the picker from the tenant's "Tax labor lines"
 * setting — the same flag the estimate copy, mobile tier and closeout autodraft
 * all honour — and arrives on the line already decided. The operator still has
 * the per-line Taxable checkbox.
 */
function addLaborLines(newLines) {
  if (!Array.isArray(newLines) || !newLines.length) return;
  const lines = localLines.value;
  const onlyEmpty = lines.length === 1 && !lines[0].description && !toNum(lines[0].unit_price);
  if (onlyEmpty) lines.splice(0, 1);
  for (const l of newLines) {
    lines.push({
      ...l,
      _lastPrice: toNum(l.unit_price),
      ...(props.showTaxable ? { taxable: l.taxable !== false } : {}),
      ...(props.showMargin ? { margin_pct_override: null } : {}),
    });
  }
  emitLines();
}

function addFromCatalog(items) {
  const lines = localLines.value;
  const onlyEmpty = lines.length === 1 && !lines[0].description && !toNum(lines[0].unit_price);
  if (onlyEmpty) lines.splice(0, 1);
  for (const item of items) {
    const cost = Number(item.cost) > 0 ? Number(item.cost) : null;
    const line = {
      description: item.description || item.name,
      quantity: 1,
      unit_price: Number(item.price) || 0,
      // Which catalog this came from — provenance the picker always knew and
      // never put on the line (Doug 2026-08-19: "what catalog did it come
      // from"). Display-only; rendered as a pill beside the description so it
      // costs no grid column. Never sent to the API.
      ...(item._catalogName ? { _catalogName: item._catalogName } : {}),
      ...(props.showTaxable ? { taxable: true } : {}),
      // Display category resolved from the item's own pricing_category first,
      // NOT its free-form string — 223 of 300 live catalog rows carry words
      // like `3" Struts` that match no option and rendered the cell blank.
      ...(props.categories.length
        ? { category: displayCategoryFor(item, props.categories) }
        : {}),
      // Carry the catalog item's cost + its canonical pricing bucket. The
      // bucket is read by `bucketForLine` for the CLIENT-side tier lookup —
      // it is deliberately NOT sent to the invoice API, whose line contract
      // has no such field and forbids extras. For invoices the client is the
      // pricing authority: routers/invoices.py stores unit_price verbatim and
      // runs no engine, so getting this bucket right IS the billed number.
      ...(props.showCost ? { cost } : {}),
      ...(item.pricing_category ? { pricing_category: item.pricing_category } : {}),
      ...(props.showMargin ? { margin_pct_override: null } : {}),
    };
    line._lastPrice = toNum(line.unit_price);
    lines.push(line);
    // Show the marked-up sell immediately, the way the estimate page always
    // has. Verified a no-op against all 299 live costed catalog rows on
    // 2026-08-19 — they already sit exactly on their tier price — so this is
    // closing the hole for the next import or cost edit, not re-pricing the
    // catalog today.
    if (cost) recomputeSell(line);
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
  background: var(--p-content-hover-background);
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
/* Description cell holds the input AND the catalog-source pill in one grid
   track. min-width:0 lets the input shrink instead of forcing the row wider —
   the row is already ~1018px of fixed tracks at full flag count. */
.line-desc-cell {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  min-width: 0;
}
.line-desc-cell :deep(.p-inputtext) {
  flex: 1 1 auto;
  min-width: 0;
}
/* Theme variables only — this has to hold in dark mode, where a hardcoded
   light background would paint invisible text. */
.catalog-source-pill {
  flex: 0 0 auto;
  max-width: 9rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  background: var(--p-content-hover-background);
  color: var(--p-text-muted-color);
  border: 1px solid var(--p-content-border-color);
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
.line-item-row-locked {
  opacity: 0.75;
}
.line-item-row-locked .locked-cell {
  display: flex;
  align-items: center;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.line-item-row-locked .locked-cell.col-qty,
.line-item-row-locked .locked-cell.col-price {
  justify-content: flex-end;
  font-variant-numeric: tabular-nums;
}
.line-item-row-locked .pi-lock {
  color: var(--p-text-muted-color, #6b7280);
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
/* Theme tokens only -- a hardcoded warning colour goes unreadable on the
   dark card, and this repo's memory records that jsdom proves nothing about
   contrast. Verified in a real browser in both themes. */
.install-double-bill {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  margin-bottom: 0.6rem;
  padding: 0.5rem 0.75rem;
  border-left: 3px solid var(--p-orange-500, #f97316);
  background: var(--p-content-hover-background, rgba(127, 127, 127, 0.08));
  border-radius: 4px;
  font-size: 0.9rem;
}
</style>
