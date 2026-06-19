<!--
  TierEditor — edit a single PricingTierSet's rows. Wholesale-replace on save.
-->
<template>
  <div class="tier-editor" :data-testid="`tier-editor-${tierSet.pricing_category}-${tierSet.pricing_class}`">
    <div class="whatif">
      <label class="whatif-label">Try a cost:</label>
      <InputNumber v-model="tryCost" :min="0" mode="currency" currency="USD" locale="en-US"
        placeholder="$ e.g. 450"
        data-testid="tier-tryCost" />
      <span class="muted small">Type a cost to see what each tier would sell it for.</span>
    </div>

    <DataTable :value="rows" data-testid="tier-rows" striped-rows>
      <Column header="Min Cost ($)" style="width: 130px">
        <template #body="{ data, index }">
          <InputNumber v-model="rows[index].cost_min" :min="0" mode="currency" currency="USD" locale="en-US"
            :data-testid="`tier-min-${index}`" />
        </template>
      </Column>
      <Column header="Max Cost ($)" style="width: 160px">
        <template #body="{ data, index }">
          <InputNumber v-model="rows[index].cost_max" :min="0" mode="currency" currency="USD" locale="en-US"
            placeholder="(blank = and above)"
            :data-testid="`tier-max-${index}`" />
        </template>
      </Column>
      <Column header="Margin %" style="width: 130px">
        <template #body="{ data, index }">
          <InputNumber v-model="rows[index].margin_pct"
            :min="0" :max="0.99"
            :min-fraction-digits="2" :max-fraction-digits="4"
            :data-testid="`tier-margin-${index}`" />
        </template>
      </Column>
      <Column header="Markup %" style="width: 110px">
        <template #body="{ data }">
          <span class="muted" :data-testid="`tier-markup-display`">{{ marginToMarkup(data.margin_pct) }}</span>
        </template>
      </Column>
      <Column header="Sell @ Min" style="width: 130px">
        <template #body="{ data }">
          <span class="muted">{{ previewSell(data.cost_min, data.margin_pct) }}</span>
        </template>
      </Column>
      <Column :header="tryCostHeader" style="width: 140px">
        <template #body="{ data }">
          <span class="try-sell" :data-testid="`tier-trysell-display`">
            {{ tryCost ? previewSell(tryCost, data.margin_pct) : '—' }}
          </span>
        </template>
      </Column>
      <Column header="" style="width: 80px">
        <template #body="{ index }">
          <Button icon="pi pi-trash" text size="small" severity="danger"
            :data-testid="`tier-remove-${index}`"
            @click="removeRow(index)" />
        </template>
      </Column>
    </DataTable>

    <div class="actions">
      <Button label="+ Add Tier" icon="pi pi-plus" size="small" outlined
        data-testid="tier-add-row" @click="addRow" />
      <span v-if="hasError" class="error">⚠ {{ error }}</span>
      <Button label="Save Tiers" icon="pi pi-save" size="small"
        :loading="saving" :disabled="hasError || rows.length === 0"
        :data-testid="`tier-save-${tierSet.pricing_category}-${tierSet.pricing_class}`"
        @click="save" />
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import InputNumber from 'primevue/inputnumber';

const props = defineProps({
  tierSet: { type: Object, required: true },
  saving: { type: Boolean, default: false },
});
const emit = defineEmits(['save']);

const rows = ref([]);
const tryCost = ref(null);

const tryCostHeader = computed(() => {
  if (!tryCost.value) return 'Sell @ Try';
  const fmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
  return `Sell @ ${fmt.format(tryCost.value)}`;
});

function marginToMarkup(margin) {
  if (margin == null || margin >= 1 || margin < 0) return '—';
  if (margin === 0) return '0%';
  const markup = margin / (1 - margin);
  return `${(markup * 100).toFixed(1)}%`;
}

watch(() => props.tierSet, (ts) => {
  rows.value = (ts.tiers || []).map(t => ({
    cost_min: t.cost_min,
    cost_max: t.cost_max,
    margin_pct: t.margin_pct,
  }));
}, { immediate: true });

function addRow() {
  // Default new row: just above the highest existing min
  const maxMin = Math.max(0, ...rows.value.map(r => r.cost_max ?? r.cost_min ?? 0));
  rows.value.push({ cost_min: maxMin, cost_max: null, margin_pct: 0.30 });
}

function removeRow(idx) {
  rows.value.splice(idx, 1);
}

const error = computed(() => {
  if (rows.value.length === 0) return 'Need at least one tier';
  const sorted = [...rows.value].sort((a, b) => (a.cost_min ?? 0) - (b.cost_min ?? 0));
  for (let i = 0; i < sorted.length; i++) {
    const t = sorted[i];
    if (t.cost_min == null || t.margin_pct == null) return `Row ${i + 1}: missing required value`;
    if (t.margin_pct < 0 || t.margin_pct >= 1) return `Row ${i + 1}: margin must be 0–0.99`;
    if (t.cost_max != null && t.cost_max <= t.cost_min) {
      return `Row ${i + 1}: max cost must be greater than min cost (or blank for top tier)`;
    }
    if (i + 1 < sorted.length) {
      const nxt = sorted[i + 1];
      if (t.cost_max == null) return `Open-ended tier (blank max) must be the highest`;
      if (nxt.cost_min < t.cost_max) return `Tiers ${i + 1} and ${i + 2} overlap`;
    }
  }
  return '';
});

const hasError = computed(() => Boolean(error.value));

function previewSell(cost, margin) {
  if (cost == null || margin == null || margin >= 1) return '—';
  const sell = cost / (1 - margin);
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(sell);
}

function save() {
  if (hasError.value) return;
  emit('save', rows.value.map(r => ({
    cost_min: r.cost_min,
    cost_max: r.cost_max,
    margin_pct: r.margin_pct,
  })));
}
</script>

<style scoped>
.tier-editor { padding: 8px 0; }
.whatif { display: flex; gap: 12px; align-items: center; padding: 8px 12px; margin-bottom: 8px;
  background: var(--surface-elevated, var(--p-content-hover-background));
  color: var(--text-primary, inherit);
  border: 1px solid var(--border-subtle, transparent);
  border-radius: 6px; }
.whatif-label { font-weight: 600; font-size: 0.9em; color: var(--text-primary, inherit); }
.whatif .small { font-size: 0.85em; color: var(--text-muted, var(--p-text-muted-color)); }
.try-sell { font-weight: 600; color: var(--interactive-primary, var(--p-primary-color)); }
.actions { display: flex; gap: 12px; align-items: center; padding: 12px 0; }
.actions .error { color: var(--p-red-600); flex: 1; font-size: 0.9em; }
.muted { color: var(--p-text-muted-color); }
</style>
