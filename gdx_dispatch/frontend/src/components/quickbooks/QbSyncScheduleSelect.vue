<template>
  <div class="qb-schedule">
    <label class="schedule-row">
      <span class="schedule-label">Auto-sync</span>
      <Select
        v-model="frequency"
        :options="options"
        optionLabel="label"
        optionValue="value"
        class="schedule-select"
        data-testid="qb-schedule-select"
        :loading="loading"
        @change="save"
      />
    </label>
    <div v-if="lastRunAt || lastRunStatus" class="schedule-meta">
      Last run: {{ formatDateTime(lastRunAt) }}<span v-if="lastRunStatus"> · {{ lastRunStatus }}</span>
      <span v-if="lastRunError" class="schedule-err" :title="lastRunError"> · error</span>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue';
import Select from 'primevue/select';
import { useApiWithToast } from '../../composables/useApiWithToast';

const api = useApiWithToast();
const frequency = ref('manual');
const lastRunAt = ref(null);
const lastRunStatus = ref(null);
const lastRunError = ref(null);
const loading = ref(false);

const options = [
  { label: 'Manual only', value: 'manual' },
  { label: 'Hourly', value: 'hourly' },
  { label: 'Every 4 hours', value: 'every_4h' },
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
];

const load = async () => {
  loading.value = true;
  try {
    const s = await api.get('/api/qb/schedule');
    frequency.value = s?.frequency || 'manual';
    lastRunAt.value = s?.last_run_at || null;
    lastRunStatus.value = s?.last_run_status || null;
    lastRunError.value = s?.last_run_error || null;
  } catch (err) {
    api.toast?.add?.({ severity: 'error', summary: 'Could not load sync schedule', detail: err?.message || 'Unknown error', life: 4000 });
  } finally {
    loading.value = false;
  }
};

const save = async () => {
  try {
    const s = await api.put('/api/qb/schedule', { frequency: frequency.value }, {
      successMessage: `Auto-sync set to: ${options.find(o => o.value === frequency.value)?.label || frequency.value}`,
    });
    lastRunAt.value = s?.last_run_at || lastRunAt.value;
    lastRunStatus.value = s?.last_run_status || lastRunStatus.value;
  } catch (err) {
    api.toast?.add?.({ severity: 'error', summary: 'Could not save schedule', detail: err?.message || 'Unknown error', life: 4000 });
  }
};

const formatDateTime = (s) => {
  if (!s) return '—';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
};

onMounted(load);
</script>

<style scoped>
.qb-schedule {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.schedule-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.schedule-label { font-size: 0.8125rem; color: var(--p-text-muted-color); }
.schedule-select { min-width: 12rem; }
.schedule-meta { font-size: 0.75rem; color: var(--p-text-muted-color); }
.schedule-err { color: var(--p-red-600, #dc2626); }
</style>
