<script setup>
// A booking that runs past the dropped-on tech's shift end (Doug,
// 2026-10-18): add a tech, book the rest on that tech's next workdays, or
// keep it all on the one day. State comes from useDayOverflowPrompt
// (composables/useScheduleDrop.js); nothing is written until a choice.
import { computed, ref, watch } from 'vue';
import Dialog from 'primevue/dialog';
import Button from 'primevue/button';
import MultiSelect from 'primevue/multiselect';
import { hoursForCrew, splitOverDays } from '../composables/useScheduleDrop';

const props = defineProps({
  prompt: { type: Object, required: true },
  // [{ id, name }] — the techs a job can be given.
  technicians: { type: Array, default: () => [] },
  // [{ id, name }] — every tech, inactive too, to name one already on the crew.
  allTechnicians: { type: Array, default: () => [] },
});
const emit = defineEmits(['answer']);

const techIds = ref([]);

watch(() => (props.prompt.visible ? `open:${props.prompt.seq ?? ''}` : ''), (open) => {
  if (!open) return;
  techIds.value = [...props.prompt.techIds];
}, { immediate: true });

const options = computed(() => {
  const list = props.technicians.map((t) => ({ id: String(t.id), name: t.name }));
  for (const id of props.prompt.techIds) {
    if (list.some((t) => t.id === id)) continue;
    const known = props.allTechnicians.find((t) => String(t.id) === id);
    list.push({ id, name: known?.name || 'Unknown tech' });
  }
  return list;
});

// The job's hours for the crew picked here: the same man-hours, shared.
const hours = computed(() => (techIds.value.length === props.prompt.techIds.length
  ? Number(props.prompt.hours)
  : hoursForCrew(props.prompt.hours, props.prompt.techIds.length, techIds.value.length)));
const left = computed(() => Number(props.prompt.hoursLeft) || 0);
const fits = computed(() => hours.value <= left.value);
const days = computed(() => (fits.value ? null : splitOverDays({
  day: props.prompt.day,
  hours: hours.value,
  firstHours: left.value,
  shiftLength: props.prompt.shift ? props.prompt.shift.end - props.prompt.shift.start : 0,
  mask: props.prompt.mask,
})));

const lead = computed(() => [props.prompt.leadId, props.prompt.droppedId]
  .find((id) => id && techIds.value.includes(id)) || techIds.value[0]);

function fmtHours(h) {
  return `${Math.round(Number(h) * 100) / 100} h`;
}
function fmtDay(dayStr) {
  const [y, m, d] = String(dayStr).split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}
function fmtClock(h) {
  const mins = Math.round(Number(h) * 60);
  const hh = Math.floor(mins / 60);
  const mm = mins % 60;
  return `${hh % 12 || 12}:${String(mm).padStart(2, '0')} ${hh < 12 ? 'AM' : 'PM'}`;
}

function choose(split) {
  if (!techIds.value.length) return;
  const leadId = lead.value;
  emit('answer', {
    tech_ids: [leadId, ...techIds.value.filter((t) => t !== leadId)],
    lead: leadId,
    hours: hours.value,
    days: split ? days.value : null,
  });
}
</script>

<template>
  <Dialog
    :visible="prompt.visible"
    header="This job runs past the end of the day"
    :modal="true"
    :closable="true"
    :style="{ width: '95vw', maxWidth: '480px' }"
    data-testid="day-overflow-prompt"
    @update:visible="(v) => { if (!v) emit('answer', null) }"
  >
    <p class="overflow-intro" data-testid="day-overflow-summary">
      <strong>{{ prompt.jobLabel }}</strong> needs {{ fmtHours(prompt.hours) }}.
      {{ prompt.techName }} has {{ fmtHours(left) }} left on {{ fmtDay(prompt.day) }}
      <span v-if="prompt.shift">(shift ends {{ fmtClock(prompt.shift.end) }})</span>.
    </p>

    <div class="overflow-field">
      <label for="day-overflow-techs">
        Add a tech? <span class="muted" data-testid="day-overflow-count">({{ techIds.length }} on the job)</span>
      </label>
      <MultiSelect
        id="day-overflow-techs"
        v-model="techIds"
        :options="options"
        optionLabel="name"
        optionValue="id"
        placeholder="Pick the crew"
        display="chip"
        filter
        class="w-full"
        data-testid="day-overflow-techs"
      />
      <small v-if="!techIds.length" class="overflow-warn">Pick at least one tech.</small>
      <small v-else-if="techIds.length !== prompt.techIds.length" class="muted" data-testid="day-overflow-crew-hours">
        With {{ techIds.length }} {{ techIds.length === 1 ? 'tech' : 'techs' }} it takes about {{ fmtHours(hours) }}.
      </small>
    </div>

    <p v-if="techIds.length && fits" class="overflow-fits" data-testid="day-overflow-fits">
      That fits in {{ fmtDay(prompt.day) }}.
    </p>
    <div v-else-if="techIds.length && days" class="overflow-field" data-testid="day-overflow-days">
      <span>Or book it over {{ days.length }} workdays:</span>
      <ul class="overflow-days">
        <li v-for="d in days" :key="d.day">{{ fmtDay(d.day) }} — {{ fmtHours(d.hours) }}</li>
      </ul>
    </div>
    <p v-else-if="techIds.length && left <= 0" class="overflow-warn" data-testid="day-overflow-no-split">
      This drop is at or after the end of {{ prompt.techName }}'s shift. To book it over several days,
      cancel and drop it earlier on the day the work starts.
    </p>
    <p v-else-if="techIds.length" class="overflow-warn" data-testid="day-overflow-no-split">
      The rest cannot be placed on {{ prompt.techName }}'s next workdays from here. Keep it on one day,
      or add the other days from the job's Visits card.
    </p>

    <template #footer>
      <Button label="Cancel" severity="secondary" text data-testid="day-overflow-cancel" @click="emit('answer', null)" />
      <template v-if="fits">
        <Button label="Schedule" icon="pi pi-check" :disabled="!techIds.length" data-testid="day-overflow-confirm" @click="choose(false)" />
      </template>
      <template v-else>
        <Button
          :label="`Keep it all on ${fmtDay(prompt.day)}`"
          severity="secondary"
          :disabled="!techIds.length"
          data-testid="day-overflow-one-day"
          @click="choose(false)"
        />
        <Button
          v-if="days"
          :label="`Book ${days.length} days`"
          icon="pi pi-calendar-plus"
          :disabled="!techIds.length"
          data-testid="day-overflow-split"
          @click="choose(true)"
        />
      </template>
    </template>
  </Dialog>
</template>

<style scoped>
.overflow-intro {
  margin-top: 0;
}
.overflow-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.9rem;
}
.overflow-days {
  margin: 0;
  padding-left: 1.2rem;
}
.overflow-fits {
  color: var(--p-green-600);
}
.overflow-warn {
  color: var(--p-red-500);
}
</style>
