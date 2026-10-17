<script setup>
// A job with no date dropped onto a tech: how long it takes and who the
// whole crew is, before anything is written. State comes from
// useScheduleDropPrompt (composables/useScheduleDrop.js).
import { computed, ref, watch } from 'vue';
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';
import InputText from 'primevue/inputtext';
import MultiSelect from 'primevue/multiselect';
import { estimateHoursOver } from '../composables/useScheduleDrop';

const props = defineProps({
  prompt: { type: Object, required: true },
  // [{ id, name }] — the techs a job can be given.
  technicians: { type: Array, default: () => [] },
  // [{ id, name }] — every tech, inactive too, to name one already on the crew.
  allTechnicians: { type: Array, default: () => [] },
});
const emit = defineEmits(['answer']);

const hours = ref('');
const techIds = ref([]);
// Set once the scheduler types in the hours box: from then on the box is
// theirs, and no estimate prefill or crew change rewrites it.
const typed = ref(false);
// { manHours, accepted } behind a prefill from the job's estimate, kept only
// when it arrived before the scheduler typed (GDXA-1262).
const estimate = ref(null);

// Keyed on seq too: a question asked while another is open replaces it in
// one tick, and the visible flag alone would not see the change.
watch(() => (props.prompt.visible ? `open:${props.prompt.seq ?? ''}` : ''), (open) => {
  if (!open) return;
  hours.value = props.prompt.hours;
  techIds.value = [...props.prompt.techIds];
  typed.value = false;
  estimate.value = null;
}, { immediate: true });

watch(() => props.prompt.estimate, (e) => {
  if (e && !typed.value && props.prompt.hours === '') estimate.value = e;
});

function onHoursInput(value) {
  typed.value = true;
  hours.value = value;
}

// The estimate's man-hours over the crew picked here, re-divided as the crew
// changes until the scheduler types. No crew picked, no figure.
const estimateHours = computed(() => (estimate.value && techIds.value.length
  ? estimateHoursOver(estimate.value.manHours, techIds.value.length)
  : null));
watch(estimateHours, (h) => {
  if (h != null && !typed.value) hours.value = String(h);
});
// The note explains the prefilled figure, so it shows only while the box
// holds that figure for the crew picked now.
const estimateNote = computed(() => (estimateHours.value != null && Number(hours.value) === estimateHours.value
  ? { ...estimate.value, crew: techIds.value.length }
  : null));

// Any tech already on the crew stays pickable even if inactive, so opening
// the dialog never silently drops one.
const options = computed(() => {
  const list = props.technicians.map((t) => ({ id: String(t.id), name: t.name }));
  for (const id of props.prompt.techIds) {
    if (list.some((t) => t.id === id)) continue;
    const known = props.allTechnicians.find((t) => String(t.id) === id);
    list.push({ id, name: known?.name || 'Unknown tech' });
  }
  return list;
});

const hoursValid = computed(() => {
  const raw = String(hours.value ?? '').trim();
  return raw === '' || (Number.isFinite(Number(raw)) && Number(raw) > 0);
});

// The prefilled lead while they stay picked; taken off, the tech the job was
// dropped on (a swap), else the first tech picked.
const lead = computed(() => [props.prompt.leadId, props.prompt.droppedId]
  .find((id) => id && techIds.value.includes(id)) || techIds.value[0]);
const leadName = computed(() => options.value.find((t) => t.id === lead.value)?.name || '');

function schedule() {
  if (!techIds.value.length || !hoursValid.value) return;
  const raw = String(hours.value ?? '').trim();
  const leadId = lead.value;
  const keptSaved = props.prompt.hours !== '' && raw === String(props.prompt.hours);
  emit('answer', {
    tech_ids: [leadId, ...techIds.value.filter((t) => t !== leadId)],
    lead: leadId,
    // The job's own saved hours left as shown are not re-sent. Anything else
    // in the box is, an estimate prefill included: that is a suggestion the
    // scheduler accepts by pressing Schedule, and nothing else would save it.
    hours: raw !== '' && !keptSaved ? Number(raw) : null,
  });
}
</script>

<template>
  <Dialog
    :visible="prompt.visible"
    header="Schedule this job"
    :modal="true"
    :closable="true"
    :style="{ width: '95vw', maxWidth: '460px' }"
    data-testid="schedule-drop-prompt"
    @update:visible="(v) => { if (!v) emit('answer', null) }"
  >
    <p class="schedule-drop-intro">
      <strong>{{ prompt.jobLabel }}</strong>
      <span v-if="prompt.whenLabel"> — {{ prompt.whenLabel }}</span>
    </p>
    <div class="schedule-drop-field">
      <label for="schedule-drop-hours">Hours</label>
      <InputText
        id="schedule-drop-hours"
        :model-value="hours"
        type="number"
        step="0.25"
        min="0"
        placeholder="e.g. 1.5"
        :invalid="!hoursValid"
        data-testid="schedule-drop-hours"
        @update:model-value="onHoursInput"
      />
      <small v-if="estimateNote" class="muted" data-testid="schedule-drop-from-estimate">
        {{ prompt.jobLabel }} has no estimated time yet.
        The {{ estimateNote.accepted ? 'accepted' : 'not yet accepted' }}
        estimate has {{ estimateNote.manHours }} man-hours; the hours above
        are that over a crew of {{ estimateNote.crew }}.
        Change them if you know better.
      </small>
    </div>
    <div class="schedule-drop-field">
      <label for="schedule-drop-techs">
        Techs <span class="muted" data-testid="schedule-drop-count">({{ techIds.length }})</span>
      </label>
      <MultiSelect
        id="schedule-drop-techs"
        v-model="techIds"
        :options="options"
        optionLabel="name"
        optionValue="id"
        placeholder="Pick the crew"
        display="chip"
        filter
        class="w-full"
        data-testid="schedule-drop-techs"
      />
      <small v-if="!techIds.length" class="schedule-drop-warn" data-testid="schedule-drop-no-tech">
        Pick at least one tech.
      </small>
      <small v-else-if="techIds.length > 1" class="muted" data-testid="schedule-drop-lead">
        Lead: {{ leadName }}
      </small>
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text data-testid="schedule-drop-cancel" @click="emit('answer', null)" />
      <Button
        label="Schedule"
        icon="pi pi-check"
        :disabled="!techIds.length || !hoursValid"
        data-testid="schedule-drop-confirm"
        @click="schedule"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.schedule-drop-intro {
  margin-top: 0;
}
.schedule-drop-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.9rem;
}
.schedule-drop-field :deep(.p-inputtext) {
  max-width: 10rem;
}
.schedule-drop-warn {
  color: var(--p-red-500);
}
</style>
