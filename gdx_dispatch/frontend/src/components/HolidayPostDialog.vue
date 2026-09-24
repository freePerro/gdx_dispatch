<!--
  HolidayPostDialog — give the people you pick the holiday's paid hours.

  Opened from Settings (the calendar) and from Timesheets (the "not posted
  yet" notice), so posting is reachable from wherever the office already is.
  The list is the timeclock roster (active people); those who have ever
  clocked in are pre-ticked, because they are the hourly crew — the office
  unticks or ticks the rest. Nothing is posted by a schedule: this click is
  the audit record of who decided.

  POST /api/timeclock/time-off/holidays/post skips anyone who already holds a
  time-off entry that day and names them, so pressing it twice pays nobody
  twice. The result stays on screen until Close.
-->
<template>
  <Dialog
    :visible="visible"
    :header="holiday ? `Post holiday pay — ${holiday.name}` : 'Post holiday pay'"
    modal
    :style="{ width: 'min(32rem, 95vw)' }"
    data-testid="holiday-post-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <template v-if="holiday">
      <p class="lead">
        <strong>{{ dateLabel }}</strong> — {{ (holiday.minutes / 60).toFixed(holiday.minutes % 60 ? 2 : 0) }}
        paid hours each, as a <em>holiday</em> entry on the timesheet.
      </p>

      <div v-if="result" class="result" data-testid="holiday-post-result">
        <Message severity="success" :closable="false">
          Posted for {{ result.created }} {{ result.created === 1 ? 'person' : 'people' }}.
          <span v-if="result.skipped.length">
            Skipped {{ result.skipped.length }} who already had the day: {{ skippedNames }}.
          </span>
        </Message>
      </div>

      <template v-else>
        <div v-if="loading" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
        <ul v-else class="people" data-testid="holiday-people">
          <li v-for="p in people" :key="p.technician_id" class="person">
            <Checkbox
              v-model="checked[p.technician_id]"
              :binary="true"
              :inputId="`hp-${p.technician_id}`"
              :data-testid="`holiday-person-${p.technician_id}`"
            />
            <label :for="`hp-${p.technician_id}`">{{ p.name || `Unknown (${String(p.technician_id).slice(0, 8)})` }}</label>
            <span v-if="p.has_entries" class="muted hint">clocks in</span>
          </li>
        </ul>
        <p v-if="!loading && !people.length" class="muted">Nobody active on the roster.</p>
        <Message v-if="error" severity="error" :closable="false" data-testid="holiday-post-error">
          {{ error }}
        </Message>
      </template>
    </template>
    <template #footer>
      <template v-if="result">
        <Button label="Close" data-testid="holiday-post-close" @click="$emit('update:visible', false)" />
      </template>
      <template v-else>
        <Button label="Cancel" severity="secondary" text @click="$emit('update:visible', false)" />
        <Button
          :label="`Post for ${selectedIds.length}`"
          icon="pi pi-check"
          :loading="posting"
          :disabled="!selectedIds.length || posting"
          data-testid="holiday-post-confirm"
          @click="post"
        />
      </template>
    </template>
  </Dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Dialog from 'primevue/dialog';
import Message from 'primevue/message';
import { useApi } from '../composables/useApi';
import { formatDate, parseLocalDateString } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  /** {date: 'YYYY-MM-DD', name, minutes} from the calendar. */
  holiday: { type: Object, default: null },
});
const emit = defineEmits(['update:visible', 'posted']);

const api = useApi();
const people = ref([]);
const checked = reactive({});
const loading = ref(false);
const posting = ref(false);
const error = ref('');
const result = ref(null);

const dateLabel = computed(() => {
  const d = props.holiday ? parseLocalDateString(props.holiday.date) : null;
  return d
    ? formatDate(d, { options: { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' } })
    : props.holiday?.date || '';
});

const selectedIds = computed(() => people.value.map((p) => String(p.technician_id)).filter((id) => checked[id]));

const skippedNames = computed(() => {
  if (!result.value) return '';
  const byId = new Map(people.value.map((p) => [String(p.technician_id), p.name]));
  return result.value.skipped.map((id) => byId.get(String(id)) || String(id).slice(0, 8)).join(', ');
});

watch(
  () => props.visible,
  async (open) => {
    if (!open) return;
    error.value = '';
    result.value = null;
    loading.value = true;
    try {
      const rows = await api.get('/api/timeclock/roster', { suppressErrorToast: true });
      const list = (Array.isArray(rows) ? rows : rows?.items || []).filter((p) => p.active !== false);
      list.sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
      people.value = list;
      for (const key of Object.keys(checked)) delete checked[key];
      for (const p of list) checked[String(p.technician_id)] = !!p.has_entries;
    } catch {
      people.value = [];
    } finally {
      loading.value = false;
    }
  },
  { immediate: true },
);

async function post() {
  if (!props.holiday || !selectedIds.value.length) return;
  posting.value = true;
  error.value = '';
  try {
    const out = await api.post(
      '/api/timeclock/time-off/holidays/post',
      { date: props.holiday.date, technician_ids: selectedIds.value },
      { successMessage: 'Holiday pay posted', suppressErrorToast: true },
    );
    result.value = { created: Number(out?.created || 0), skipped: out?.skipped || [] };
    emit('posted', out);
  } catch (e) {
    const detail = e?.body?.detail;
    error.value = (typeof detail === 'string' && detail) || e?.message || 'The holiday was not posted';
  } finally {
    posting.value = false;
  }
}
</script>

<style scoped>
.lead {
  margin: 0 0 0.75rem;
}
.people {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 0.4rem;
  max-height: 50vh;
  overflow: auto;
}
.person {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  min-height: 2.25rem;
}
.hint {
  font-size: 0.75rem;
}
.state-msg {
  text-align: center;
  padding: 0.5rem 0;
}
.muted {
  color: var(--p-text-muted-color);
}
.result {
  margin-top: 0.5rem;
}
</style>
