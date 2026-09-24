<!--
  TimeOffRequestPanel — "Time off" on the tech's own clock pages: the button
  that opens TimeOffRequestDialog and the list of what they have asked for,
  with its status and the office's note. One component under both the mobile
  section and the desktop card so the two cannot drift.

  Reads GET /api/timeclock/time-off/requests (the caller's own). Cancel is
  the only write here and only while pending; an approved day is undone by
  the office (revoke), never by the tech.
-->
<template>
  <section class="time-off-panel" data-testid="time-off-panel">
    <div class="panel-head">
      <h2 class="section-title">Time off</h2>
      <Button
        label="Request time off"
        icon="pi pi-calendar-plus"
        size="small"
        severity="secondary"
        outlined
        data-testid="time-off-request-btn"
        @click="showDialog = true"
      />
    </div>

    <div v-if="loading" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
    <ul v-else-if="requests.length" class="request-list" data-testid="time-off-request-list">
      <li
        v-for="r in requests"
        :key="r.id"
        class="request-row"
        :data-testid="`time-off-request-${r.id}`"
      >
        <div class="request-main">
          <strong class="request-dates">{{ dateRange(r) }}</strong>
          <Tag :value="r.status" :severity="timeOffStatusSeverity(r.status)" class="request-status" />
        </div>
        <div class="request-meta muted">
          {{ r.entry_type_label || 'Vacation' }} ·
          {{ (r.minutes_per_day / 60).toFixed(r.minutes_per_day % 60 ? 2 : 0) }}h/day
          <template v-if="r.workday_count != null">
            · {{ r.workday_count }} {{ r.workday_count === 1 ? 'workday' : 'workdays' }}
          </template>
        </div>
        <div v-if="r.notes" class="request-note muted">“{{ r.notes }}”</div>
        <div v-if="r.review_note" class="request-note" data-testid="time-off-review-note">
          Office: {{ r.review_note }}
        </div>
        <Button
          v-if="r.status === 'pending'"
          label="Cancel request"
          size="small"
          text
          severity="danger"
          :loading="cancelling === r.id"
          :data-testid="`time-off-cancel-${r.id}`"
          @click="cancel(r)"
        />
      </li>
    </ul>
    <p v-else class="muted state-msg" data-testid="time-off-empty">No time off requested yet.</p>

    <TimeOffRequestDialog v-model:visible="showDialog" :mobile="mobile" @saved="onSaved" />
  </section>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import Button from 'primevue/button';
import Tag from 'primevue/tag';
import TimeOffRequestDialog from './TimeOffRequestDialog.vue';
import { useApi } from '../composables/useApi';
import { formatDate, parseLocalDateString } from '../composables/useFormatters';
import { timeOffStatusSeverity } from '../utils/statusSeverity';

defineProps({
  /** Bottom-sheet dialog for the phone. */
  mobile: { type: Boolean, default: false },
});
const emit = defineEmits(['changed']);

const api = useApi();
const requests = ref([]);
const loading = ref(false);
const showDialog = ref(false);
const cancelling = ref('');

function fmt(day, withYear) {
  const d = parseLocalDateString(day);
  if (!d) return String(day || '');
  return formatDate(d, {
    options: { weekday: 'short', month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}) },
  });
}

function dateRange(r) {
  if (r.start_date === r.end_date) return fmt(r.start_date, true);
  return `${fmt(r.start_date, false)} – ${fmt(r.end_date, true)}`;
}

async function load() {
  loading.value = true;
  try {
    const data = await api.get('/api/timeclock/time-off/requests', { suppressErrorToast: true });
    requests.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    // The clock pages must not blank over this; the list just reads empty.
    requests.value = [];
  } finally {
    loading.value = false;
  }
}

async function cancel(r) {
  cancelling.value = r.id;
  try {
    await api.post(`/api/timeclock/time-off/requests/${r.id}/cancel`, {}, { successMessage: 'Request cancelled' });
    await load();
    emit('changed');
  } finally {
    cancelling.value = '';
  }
}

async function onSaved() {
  await load();
  emit('changed');
}

onMounted(load);

defineExpose({ load });
</script>

<style scoped>
.time-off-panel {
  margin-top: 0.5rem;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
  flex-wrap: wrap;
}
.section-title {
  margin: 0;
  font-size: 1rem;
  font-weight: 700;
}
.request-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.request-row {
  background: var(--p-content-background);
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.request-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.request-meta,
.request-note {
  font-size: 0.85rem;
}
.state-msg {
  text-align: center;
  padding: 0.5rem 0;
  margin: 0;
}
.muted {
  color: var(--p-text-muted-color);
}
</style>
