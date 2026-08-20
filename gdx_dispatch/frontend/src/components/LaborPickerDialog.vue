<!--
  LaborPickerDialog — the two ways a labor line gets onto a bill.

  Doug 2026-08-19, asked whether Add Labor should bill the matrix flat price or
  the tech's attested hours: "it could be either." So both lanes are offered,
  side by side, and the operator picks. Nothing here chooses for them.

  THE INVARIANT THIS COMPONENT PROTECTS
  -------------------------------------
  Billed labor comes from attested hours only; code may not invent hours.
  A matrix row is a QUOTED FLAT PRICE — a contract price for a job of that
  shape — and is NOT a claim about how long the work took. So:

    * the matrix lane emits a flat-price line and NEVER writes an hours count
      into the description. `assumed_man_hours` is used for the cost snapshot
      (margin math) and for the disagreement warning below, nothing else.
    * the attested lane is the only lane allowed to express hours, and only
      the ones the tech signed off.

  When both are available and they DISAGREE, both numbers are shown together.
  Hiding the attested hours behind a flat price is how the evidence gets lost,
  and the office is the one who should decide which to bill.

  Contract:
    v-model:visible → open/close (parent owns visibility)
    :closeout       → the /api/jobs/:id/closeout-billing-suggestion payload the
                      parent already fetched. Absent/!has_closeout hides lane 2.
    @add(lines)     → array of ready-to-use line objects, each carrying
                      labor_source ('matrix' | 'attested') plus, for matrix
                      rows, labor_price_item_id + estimated_man_hours.
-->
<template>
  <Dialog
    :visible="visible"
    @update:visible="$emit('update:visible', $event)"
    header="Add Labor"
    modal
    :style="{ width: '780px' }"
    data-testid="labor-picker"
  >
    <!-- Both lanes have something to say and they disagree: show it. -->
    <div
      v-if="hoursDisagreement"
      class="labor-disagree"
      data-testid="labor-hours-disagreement"
    >
      <strong>The matrix and the tech disagree about the hours.</strong>
      <span>
        The matrix assumes {{ hoursDisagreement.matrix }} h for
        “{{ hoursDisagreement.label }}”; the tech attested
        {{ hoursDisagreement.attested }} h on this job. Bill whichever reflects
        what was actually agreed — a flat price is a contract, attested hours
        are evidence.
      </span>
    </div>

    <!-- Lane 2 first: when a tech has attested hours on THIS job, that is the
         evidence, and it should not be below the fold. -->
    <section v-if="attested" class="labor-lane" data-testid="labor-lane-attested">
      <h4 class="labor-lane-head">From the tech's attested hours</h4>
      <p class="labor-lane-sub">
        {{ attested.hours }} h × {{ attested.techs }} tech{{ attested.techs === 1 ? '' : 's' }}
        <template v-if="attested.closedAt"> · closed {{ attested.closedAt }}</template>
      </p>
      <div class="labor-attested-row">
        <span class="labor-attested-desc">{{ attested.description }}</span>
        <span class="labor-attested-price" data-testid="labor-attested-price">
          {{ currency(attested.unitPrice * attested.quantity) }}
        </span>
        <Button
          label="Bill these hours"
          icon="pi pi-check"
          size="small"
          :disabled="taxLaborLoading"
          data-testid="labor-add-attested"
          @click="addAttested"
        />
      </div>
    </section>

    <!-- Install jobs: the closeout already picked a matrix row. Offered under
         the MATRIX lane's terms — a quoted contract price — never relabelled
         as attested hours. -->
    <section
      v-if="suggestedMatrixLine"
      class="labor-lane"
      data-testid="labor-lane-suggested-matrix"
    >
      <h4 class="labor-lane-head">Quoted on this job's closeout</h4>
      <p class="labor-lane-sub">
        A flat install price the tech already picked from the matrix. This is a
        contract price, not a record of hours worked.
      </p>
      <div class="labor-attested-row">
        <span class="labor-attested-desc">{{ suggestedMatrixLine.description }}</span>
        <span class="labor-attested-price">
          {{ currency(suggestedMatrixLine.unitPrice * suggestedMatrixLine.quantity) }}
        </span>
        <Button
          label="Bill this price"
          icon="pi pi-check"
          size="small"
          :disabled="taxLaborLoading"
          data-testid="labor-add-suggested-matrix"
          @click="addSuggestedMatrix"
        />
      </div>
    </section>

    <section class="labor-lane" data-testid="labor-lane-matrix">
      <h4 class="labor-lane-head">From the labor matrix</h4>
      <InputText
        v-model="search"
        placeholder="Search by description, service, size, or SKU…"
        class="w-full"
        data-testid="labor-search"
      />
      <div v-if="loading" class="muted">Loading labor matrix…</div>
      <div v-else-if="forbidden" class="muted" data-testid="labor-forbidden">
        <i class="pi pi-lock" /> Your role can't see the labor matrix —
        ask an admin for <code>pricing.labor_matrix.read</code>.
      </div>
      <div v-else-if="!items.length" class="muted" data-testid="labor-empty">
        No labor rows configured. Add rows in
        <a href="/labor-matrix" target="_blank">Labor Matrix</a>.
      </div>
      <DataTable
        v-else
        responsiveLayout="scroll"
        :value="filtered"
        :paginator="filtered.length > 10"
        :rows="10"
        selectionMode="multiple"
        v-model:selection="selected"
        dataKey="id"
        stripedRows
        data-testid="labor-table"
      >
        <Column selectionMode="multiple" style="width: 3rem" />
        <Column field="description" header="Description" sortable />
        <Column field="service_type" header="Service" sortable style="width: 110px" />
        <Column header="Size / SKU" style="width: 110px">
          <template #body="{ data }">{{ sizeLabel(data) }}</template>
        </Column>
        <Column header="Flat price" style="width: 110px">
          <template #body="{ data }">{{ currency(data.flat_price) }}</template>
        </Column>
        <Column header="Man-hrs" style="width: 90px">
          <template #body="{ data }">{{ Number(data.assumed_man_hours).toFixed(1) }}h</template>
        </Column>
      </DataTable>
    </section>

    <template #footer>
      <Button label="Cancel" severity="secondary" @click="$emit('update:visible', false)" />
      <Button
        :label="`Add ${selected.length} labor item${selected.length !== 1 ? 's' : ''}`"
        icon="pi pi-plus"
        :disabled="!selected.length || taxLaborLoading"
        data-testid="labor-add-matrix"
        @click="addMatrix"
      />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import Dialog from 'primevue/dialog';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import { useApi } from '../composables/useApi';
import { formatMoney as currency } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  // The closeout-billing-suggestion payload the parent already holds.
  closeout: { type: Object, default: null },
});
const emit = defineEmits(['update:visible', 'add']);

const api = useApi();

const items = ref([]);
const selected = ref([]);
const search = ref('');
const loading = ref(false);
const forbidden = ref(false);
// Tenant loaded labor cost per hour, for the matrix lane's cost snapshot so the
// profit panel matches what the backend stores. 0 => labor shows 100% margin.
const loadedRate = ref(0);
// Tenant "Tax labor lines" setting (Settings -> Tax). Default false, matching
// TaxConfig's own default and `_load_tax_labor_flag`.
//
// Hardcoding `false` here would have been simpler and wrong: the closeout
// AUTODRAFT honours this flag, so with it ON the same job would bill labor
// taxable through the autodraft and non-taxable through Add Labor. Four paths
// already agree on this setting; a fifth that ignores it is the drift that
// produced the tax bug this codebase has already paid for once (money audit
// M24, invoices.py).
const taxLabor = ref(false);
// The add buttons stay disabled until the flag is known. Without this a fast
// click can emit a line with the default while the request is still in flight,
// silently misbilling tax on a tenant that taxes labor.
const taxLaborLoading = ref(true);

// `immediate` matters: a parent that renders this already-open (v-if plus a
// pre-set flag) would otherwise never trigger the load and show an empty matrix
// that looks like "no labor rows configured".
watch(() => props.visible, (open) => {
  if (!open) return;
  search.value = '';
  selected.value = [];
  loadRate();
  loadTaxLabor();
  if (!items.value.length) loadMatrix();
}, { immediate: true });

async function loadRate() {
  try {
    const s = await api.get('/api/pricing-engine/settings', { suppressErrorToast: true });
    loadedRate.value = Number(s?.loaded_labor_cost_per_hour) || 0;
  } catch { /* 0 is a safe default — cost snapshot just stays 0 */ }
}

async function loadTaxLabor() {
  taxLaborLoading.value = true;
  try {
    const cfg = await api.get('/api/tax/config', { suppressErrorToast: true });
    taxLabor.value = Boolean(cfg?.tax_labor);
  } catch {
    // Default false = do not tax labor, matching the server-side helper's own
    // fallback. Defaulting true would re-introduce an overbill.
    taxLabor.value = false;
  } finally {
    taxLaborLoading.value = false;
  }
}

async function loadMatrix() {
  loading.value = true;
  forbidden.value = false;
  try {
    const r = await api.get('/api/labor-pricing/items?active=true', { suppressErrorToast: true });
    const list = Array.isArray(r) ? r : r?.data || [];
    items.value = list.filter((i) => i.active !== false);
  } catch (e) {
    // "no permission" and "no rows" are different answers and the operator
    // needs to know which one they got — same reasoning as the parts panel.
    const status = e?.status ?? e?.response?.status;
    forbidden.value = status === 401 || status === 403;
    items.value = [];
  } finally {
    loading.value = false;
  }
}

function sizeLabel(item) {
  // width_ft / height_ft are FEET. EstimateView divides them by 12, which
  // renders every 16x7 row as "1x1" — a bug copied from there and NOT
  // reproduced here. (Fixing EstimateView's copy is a separate change; this
  // component is new and starts correct.)
  if (item.width_ft && item.height_ft) {
    return `${Math.round(item.width_ft)}x${Math.round(item.height_ft)}`;
  }
  return item.sku || '—';
}

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  if (!q) return items.value;
  return items.value.filter((i) =>
    [i.description, i.service_type, i.sku, sizeLabel(i)]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(q)),
  );
});

// Lane 2, from the closeout the parent already fetched — reused rather than
// recomputed, so the dialog cannot disagree with the prefill about what the
// hours are worth.
//
// CRITICAL: `labor_line` is NOT always attested hours. The server computes it
// per job lane — a service job yields attested hours x rate, an INSTALL job
// yields a quoted flat price from a labor-matrix row. Treating both as
// "attested" recorded a contract price as hours evidence, with hours that did
// not price it, which inverts the one invariant this dialog exists to hold.
// The API now says which (`labor_line.source`); this lane accepts ONLY the
// attested one. A matrix-sourced suggestion is offered under the matrix lane's
// terms instead — see `suggestedMatrixLine`.
const attested = computed(() => {
  const c = props.closeout;
  const l = c?.labor_line;
  if (!c?.has_closeout || !l) return null;
  if (l.source !== 'attested') return null;
  return {
    description: l.description,
    quantity: Number(l.quantity || 1) || 1,
    unitPrice: Number(l.unit_price || 0),
    hours: l.man_hours ?? c.closeout?.hours_worked,
    techs: c.closeout?.techs_on_site ?? 1,
    closedAt: (c.closeout?.closed_at || '').slice(0, 10),
  };
});

// The install-lane suggestion: a flat price the tech's closeout already picked
// a matrix row for. Offered as what it is — quoted, not attested — and it
// carries the matrix row id so the invoice line can name what priced it.
const suggestedMatrixLine = computed(() => {
  const l = props.closeout?.labor_line;
  if (!props.closeout?.has_closeout || !l || l.source !== 'matrix') return null;
  return {
    description: l.description,
    quantity: Number(l.quantity || 1) || 1,
    unitPrice: Number(l.unit_price || 0),
    laborPriceItemId: l.labor_price_item_id || null,
  };
});

// Only meaningful once the operator has picked a matrix row to compare against.
const hoursDisagreement = computed(() => {
  if (!attested.value || selected.value.length !== 1) return null;
  const row = selected.value[0];
  const matrixHours = Number(row.assumed_man_hours) || 0;
  const attestedHours = Number(attested.value.hours) || 0;
  if (!matrixHours || !attestedHours) return null;
  if (Math.abs(matrixHours - attestedHours) < 0.5) return null;
  return {
    matrix: matrixHours.toFixed(1),
    attested: attestedHours.toFixed(1),
    label: row.description,
  };
});

function addMatrix() {
  const rate = Number(loadedRate.value) || 0;
  const lines = selected.value.map((item) => {
    const hours = Number(item.assumed_man_hours) || 0;
    return {
      // NO hours in the description. A matrix row is a quoted contract price,
      // and writing "6.5 hrs labor" here would be the code inventing hours —
      // the exact thing the labor invariant forbids.
      description: item.description,
      quantity: 1,
      unit_price: Number(item.flat_price) || 0,
      category: 'Labor',
      // Follows the tenant's "Tax labor lines" setting, the same flag the
      // estimate copy, mobile tier and closeout autodraft all resolve. On this
      // tenant it is OFF (MN garage-door work is a construction contract).
      taxable: taxLabor.value,
      // Cost from the tenant's loaded rate x assumed hours so the margin panel
      // matches what the backend stamps. Not a claim about hours worked.
      cost: Math.round(rate * hours * 100) / 100,
      _priceOverridden: true, // flat price IS the price — suppress tier recompute
      labor_price_item_id: item.id,
      estimated_man_hours: hours,
      labor_source: 'matrix',
      // The price this provenance refers to. The editor downgrades
      // matrix -> manual only when unit_price actually moves away from it;
      // PrimeVue commits on every blur, so "did it change" cannot be inferred
      // from the event alone.
      _provenancePrice: Number(item.flat_price) || 0,
    };
  });
  if (lines.length) emit('add', lines);
  selected.value = [];
  emit('update:visible', false);
}

function addAttested() {
  const a = attested.value;
  if (!a) return;
  emit('add', [{
    description: a.description,
    quantity: a.quantity,
    unit_price: a.unitPrice,
    category: 'Labor',
    cost: null,
    // Follows the tenant's "Tax labor lines" setting — see addMatrix.
    taxable: taxLabor.value,
    _priceOverridden: true,
    estimated_man_hours: Number(a.hours) || null,
    labor_source: 'attested',
    // Baseline for the downgrade: repricing attested hours means an office
    // number, not tech-signed evidence.
    _provenancePrice: a.unitPrice,
  }]);
  emit('update:visible', false);
}

// The closeout already picked a matrix row for this install. Same terms as any
// other matrix line: quoted price, named row, no hours claim.
function addSuggestedMatrix() {
  const m = suggestedMatrixLine.value;
  if (!m) return;
  emit('add', [{
    description: m.description,
    quantity: m.quantity,
    unit_price: m.unitPrice,
    category: 'Labor',
    cost: null,
    taxable: taxLabor.value,
    _priceOverridden: true,
    ...(m.laborPriceItemId
      ? {
          labor_price_item_id: m.laborPriceItemId,
          labor_source: 'matrix',
          _provenancePrice: m.unitPrice,
        }
      : { labor_source: 'manual' }),
  }]);
  emit('update:visible', false);
}
</script>

<style scoped>
.labor-lane { margin-bottom: 1.25rem; }
.labor-lane-head { margin: 0 0 0.25rem; font-size: 0.95rem; font-weight: 600; }
.labor-lane-sub { margin: 0 0 0.5rem; color: var(--p-text-muted-color); font-size: 0.85rem; }
.labor-attested-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  background: var(--p-content-hover-background);
}
.labor-attested-desc { flex: 1 1 auto; min-width: 0; }
.labor-attested-price { font-weight: 600; }
/* Theme variables only — must stay readable in dark mode. */
.labor-disagree {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  padding: 0.6rem 0.8rem;
  margin-bottom: 1rem;
  border: 1px solid var(--p-content-border-color);
  border-left: 3px solid var(--p-orange-500, #f97316);
  border-radius: 6px;
  background: var(--p-content-hover-background);
  font-size: 0.9rem;
}
.muted { color: var(--p-text-muted-color, #6b7280); }
.w-full { width: 100%; margin-bottom: 0.75rem; }
</style>
