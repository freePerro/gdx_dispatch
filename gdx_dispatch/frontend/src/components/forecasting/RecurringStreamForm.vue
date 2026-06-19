<template>
  <Dialog
    :visible="visible"
    @update:visible="$emit('update:visible', $event)"
    :header="isEdit ? 'Edit recurring payment' : 'Mark as recurring'"
    modal
    :style="{ width: '32rem' }"
    :closable="!submitting"
  >
    <div class="form">
      <div class="row">
        <label>Label</label>
        <InputText v-model="form.label" placeholder="e.g. Midwest Bank loan 6705454" />
      </div>
      <div class="row">
        <label>Payee match (uppercase)</label>
        <InputText v-model="form.payee_pattern" placeholder="MIDWEST BANK" />
        <div class="hint">The detector groups bank txns whose payee normalizes to this string.</div>
      </div>
      <div class="row two">
        <div>
          <label>Amount min</label>
          <InputNumber v-model="form.amount_min" mode="currency" currency="USD" :minFractionDigits="2" />
        </div>
        <div>
          <label>Amount max</label>
          <InputNumber v-model="form.amount_max" mode="currency" currency="USD" :minFractionDigits="2" />
        </div>
      </div>
      <div class="row">
        <label>Cadence</label>
        <Select v-model="form.cadence" :options="cadenceOptions" optionLabel="label" optionValue="value" />
      </div>
      <div class="row">
        <label>Term type</label>
        <SelectButton v-model="termMode" :options="termModes" optionLabel="label" optionValue="value" />
      </div>
      <div v-if="termMode === 'occurrences'" class="row">
        <label>Total payments</label>
        <InputNumber v-model="form.term_total_occurrences" :min="1" :max="600" placeholder="e.g. 36 for a 36-payment loan" />
      </div>
      <div v-if="termMode === 'end_date'" class="row">
        <label>End date</label>
        <DatePicker v-model="form.term_end_date" dateFormat="yy-mm-dd" :showIcon="true" />
      </div>
      <div class="row">
        <label>Notes</label>
        <Textarea v-model="form.notes" rows="2" placeholder="optional" />
      </div>
    </div>

    <template #footer>
      <Button label="Cancel" severity="secondary" outlined :disabled="submitting" @click="$emit('update:visible', false)" />
      <Button :label="isEdit ? 'Save changes' : 'Create'" icon="pi pi-check" :loading="submitting" @click="onSubmit" />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Select from 'primevue/select';
import SelectButton from 'primevue/selectbutton';
import DatePicker from 'primevue/datepicker';
import Textarea from 'primevue/textarea';

const props = defineProps({
  visible: { type: Boolean, default: false },
  stream: { type: Object, default: null },
  submitting: { type: Boolean, default: false },
});
const emit = defineEmits(['update:visible', 'submit']);

const isEdit = computed(() => !!props.stream?.id);

const cadenceOptions = [
  { label: 'Weekly', value: 'weekly' },
  { label: 'Bi-weekly', value: 'biweekly' },
  { label: 'Monthly', value: 'monthly' },
  { label: 'Quarterly', value: 'quarterly' },
  { label: 'Semi-annual', value: 'semiannual' },
  { label: 'Annual', value: 'annual' },
];

const termModes = [
  { label: 'Open-ended (subscription)', value: 'open' },
  { label: 'Number of payments', value: 'occurrences' },
  { label: 'End date', value: 'end_date' },
];

const form = ref({
  label: '',
  payee_pattern: '',
  amount_min: null,
  amount_max: null,
  cadence: 'monthly',
  cadence_anchor_day: null,
  term_total_occurrences: null,
  term_end_date: null,
  notes: '',
});
const termMode = ref('open');

watch(() => props.visible, (v) => {
  if (!v) return;
  if (props.stream) {
    form.value = {
      label: props.stream.label || '',
      payee_pattern: props.stream.payee_pattern || '',
      amount_min: props.stream.amount_min ?? null,
      amount_max: props.stream.amount_max ?? null,
      cadence: props.stream.cadence || 'monthly',
      cadence_anchor_day: props.stream.cadence_anchor_day ?? null,
      term_total_occurrences: props.stream.term_total_occurrences ?? null,
      term_end_date: props.stream.term_end_date ? new Date(props.stream.term_end_date) : null,
      notes: props.stream.notes || '',
    };
    if (form.value.term_total_occurrences) termMode.value = 'occurrences';
    else if (form.value.term_end_date) termMode.value = 'end_date';
    else termMode.value = 'open';
  } else {
    form.value = {
      label: '', payee_pattern: '',
      amount_min: null, amount_max: null,
      cadence: 'monthly', cadence_anchor_day: null,
      term_total_occurrences: null, term_end_date: null, notes: '',
    };
    termMode.value = 'open';
  }
});

// When user flips term mode, null out the other field so server XOR check passes.
watch(termMode, (m) => {
  if (m === 'open') {
    form.value.term_total_occurrences = null;
    form.value.term_end_date = null;
  } else if (m === 'occurrences') {
    form.value.term_end_date = null;
  } else if (m === 'end_date') {
    form.value.term_total_occurrences = null;
  }
});

function onSubmit() {
  const payload = {
    label: form.value.label?.trim(),
    payee_pattern: (form.value.payee_pattern || '').toUpperCase().trim(),
    amount_min: Number(form.value.amount_min ?? 0),
    amount_max: Number(form.value.amount_max ?? 0),
    cadence: form.value.cadence,
  };
  if (form.value.cadence_anchor_day != null) payload.cadence_anchor_day = Number(form.value.cadence_anchor_day);
  if (form.value.notes) payload.notes = form.value.notes;
  if (termMode.value === 'occurrences' && form.value.term_total_occurrences) {
    payload.term_total_occurrences = Number(form.value.term_total_occurrences);
  } else if (termMode.value === 'end_date' && form.value.term_end_date) {
    const d = form.value.term_end_date;
    payload.term_end_date = d instanceof Date ? d.toISOString().slice(0, 10) : d;
  }
  emit('submit', payload);
}
</script>

<style scoped>
.form { display: flex; flex-direction: column; gap: 1rem; padding: 0.25rem 0; }
.row { display: flex; flex-direction: column; gap: 0.375rem; }
.row.two { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.row label { font-weight: 500; font-size: 0.875rem; }
.hint { font-size: 0.75rem; color: var(--p-text-muted-color); }
</style>
