<!--
  VolumeTierEditor — edit customer rolling-12mo paid-volume discount tiers.
  Wholesale-replace on save. Tier lookup keys on the customer's trailing
  365-day paid invoice volume (Sprint 1.0.6), not the per-estimate subtotal.
-->
<template>
  <div class="vol-tier-editor" data-testid="volume-tier-editor">
    <DataTable :value="rows" data-testid="volume-tier-rows" striped-rows>
      <Column header="12-Mo Volume Min ($)" style="width: 180px">
        <template #body="{ index }">
          <InputNumber v-model="rows[index].volume_min_12mo" :min="0" mode="currency" currency="USD" locale="en-US"
            :data-testid="`vol-min-${index}`" />
        </template>
      </Column>
      <Column header="12-Mo Volume Max ($)" style="width: 200px">
        <template #body="{ index }">
          <InputNumber v-model="rows[index].volume_max_12mo" :min="0" mode="currency" currency="USD" locale="en-US"
            placeholder="(blank = and above)"
            :data-testid="`vol-max-${index}`" />
        </template>
      </Column>
      <Column header="Discount %" style="width: 130px">
        <template #body="{ index }">
          <InputNumber v-model="rows[index].discount_pct"
            :min="0" :max="0.5" :max-fraction-digits="4"
            :data-testid="`vol-pct-${index}`" />
        </template>
      </Column>
      <Column header="" style="width: 80px">
        <template #body="{ index }">
          <Button icon="pi pi-trash" text size="small" severity="danger"
            :data-testid="`vol-remove-${index}`"
            @click="removeRow(index)" />
        </template>
      </Column>
    </DataTable>

    <div class="actions">
      <Button label="+ Add Tier" icon="pi pi-plus" size="small" outlined
        data-testid="vol-add-row" @click="addRow" />
      <span v-if="hasError" class="error">⚠ {{ error }}</span>
      <Button label="Save Volume Tiers" icon="pi pi-save" size="small"
        :loading="saving" :disabled="hasError"
        data-testid="vol-tier-save" @click="save" />
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
  tiers: { type: Array, required: true },
  saving: { type: Boolean, default: false },
});
const emit = defineEmits(['save']);

const rows = ref([]);

watch(() => props.tiers, (t) => {
  rows.value = (t || []).map(r => ({
    volume_min_12mo: r.volume_min_12mo,
    volume_max_12mo: r.volume_max_12mo,
    discount_pct: r.discount_pct,
  }));
}, { immediate: true });

function addRow() {
  const maxMin = Math.max(0, ...rows.value.map(r => r.volume_max_12mo ?? r.volume_min_12mo ?? 0));
  rows.value.push({ volume_min_12mo: maxMin, volume_max_12mo: null, discount_pct: 0.05 });
}

function removeRow(idx) {
  rows.value.splice(idx, 1);
}

const error = computed(() => {
  if (rows.value.length === 0) return '';  // empty list = no discount = OK
  const sorted = [...rows.value].sort((a, b) => (a.volume_min_12mo ?? 0) - (b.volume_min_12mo ?? 0));
  for (let i = 0; i < sorted.length; i++) {
    const t = sorted[i];
    if (t.volume_min_12mo == null || t.discount_pct == null) return `Row ${i + 1}: missing value`;
    if (t.discount_pct < 0 || t.discount_pct >= 1) return `Row ${i + 1}: discount must be 0–0.99`;
    if (t.volume_max_12mo != null && t.volume_max_12mo <= t.volume_min_12mo) {
      return `Row ${i + 1}: max must be greater than min (or blank)`;
    }
    if (i + 1 < sorted.length) {
      const nxt = sorted[i + 1];
      if (t.volume_max_12mo == null) return `Open-ended tier must be highest`;
      if (nxt.volume_min_12mo < t.volume_max_12mo) return `Tiers ${i + 1} and ${i + 2} overlap`;
    }
  }
  return '';
});

const hasError = computed(() => Boolean(error.value));

function save() {
  if (hasError.value) return;
  emit('save', rows.value);
}
</script>

<style scoped>
.vol-tier-editor { padding: 8px 0; }
.actions { display: flex; gap: 12px; align-items: center; padding: 12px 0; }
.actions .error { color: var(--p-red-600); flex: 1; font-size: 0.9em; }
</style>
