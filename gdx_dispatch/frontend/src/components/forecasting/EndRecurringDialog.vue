<template>
  <Dialog
    :visible="visible"
    @update:visible="$emit('update:visible', $event)"
    header="End recurring payment"
    modal
    :style="{ width: '26rem' }"
    :closable="!submitting"
  >
    <p v-if="stream" class="lead">
      <strong>{{ stream.label }}</strong> · {{ formatAmount(stream) }} {{ stream.cadence }}
    </p>
    <p class="hint">
      Ending preserves all history — past payments stay queryable on the Ended tab.
      Future forecast projections will drop this stream.
    </p>

    <div class="row">
      <label>Reason</label>
      <SelectButton v-model="reason" :options="reasonOptions" optionLabel="label" optionValue="value" />
    </div>
    <div class="row">
      <label>Effective date</label>
      <DatePicker v-model="endedAt" dateFormat="yy-mm-dd" :showIcon="true" />
    </div>

    <template #footer>
      <Button label="Cancel" severity="secondary" outlined :disabled="submitting" @click="$emit('update:visible', false)" />
      <Button label="End stream" icon="pi pi-stop-circle" severity="warning" :loading="submitting" @click="onSubmit" />
    </template>
  </Dialog>
</template>

<script setup>
import { ref, watch } from 'vue';
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';
import SelectButton from 'primevue/selectbutton';
import DatePicker from 'primevue/datepicker';

const props = defineProps({
  visible: { type: Boolean, default: false },
  stream: { type: Object, default: null },
  submitting: { type: Boolean, default: false },
});
const emit = defineEmits(['update:visible', 'submit']);

const reasonOptions = [
  { label: 'Paid off', value: 'paid_off' },
  { label: 'Cancelled', value: 'cancelled' },
  { label: 'Expired', value: 'expired' },
];

const reason = ref('paid_off');
const endedAt = ref(new Date());

watch(() => props.visible, (v) => {
  if (v) {
    reason.value = 'paid_off';
    endedAt.value = new Date();
  }
});

function formatAmount(s) {
  const fmt = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(Number(n));
  const lo = Number(s.amount_min);
  const hi = Number(s.amount_max);
  if (Math.abs(hi - lo) < 0.01) return fmt(lo);
  return `${fmt(lo)} – ${fmt(hi)}`;
}

function onSubmit() {
  const payload = { reason: reason.value };
  if (endedAt.value) {
    const d = endedAt.value;
    payload.ended_at = d instanceof Date ? d.toISOString().slice(0, 10) : d;
  }
  emit('submit', payload);
}
</script>

<style scoped>
.lead { margin: 0 0 0.5rem; }
.hint { font-size: 0.875rem; color: var(--p-text-muted-color); margin: 0 0 1rem; }
.row { display: flex; flex-direction: column; gap: 0.375rem; margin-bottom: 1rem; }
.row label { font-weight: 500; font-size: 0.875rem; }
</style>
