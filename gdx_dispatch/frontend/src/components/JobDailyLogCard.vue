<!--
  JobDailyLogCard — a multi-day job's daily log (plan §5.4a "Daily log",
  R-P3): every day a crew closed with "No, not finished", newest first, with
  the person, hours, note and who closed it.

  Reads GET /api/jobs/{jobId}/day-log. Hidden while there are no rows, and
  when the read fails: the log is a view of hours already billed elsewhere,
  so a failed read must not put an error card on every job page.

  `compact` drops the card chrome for the phone's job detail. Bump
  `refreshKey` to re-read after a day closes.
-->
<template>
  <div v-if="rows.length" :class="compact ? 'daily-log compact' : 'card daily-log'" data-testid="job-daily-log">
    <div class="card-header">
      <h3>Daily log</h3>
      <span class="muted" data-testid="job-daily-log-total">{{ formatHours(total) }} h logged</span>
    </div>
    <section v-for="g in groups" :key="g.date" class="day-group" :data-testid="`job-daily-log-day-${g.date}`">
      <h4 class="day-label">{{ formatDayLong(g.date) }}</h4>
      <ul class="day-rows">
        <li v-for="r in g.rows" :key="r.id" class="day-row" :data-testid="`job-daily-log-row-${r.id}`">
          <div class="day-main">
            <strong>{{ r.person_name || 'Someone' }}</strong>
            <span>{{ formatHours(r.hours) }} h</span>
            <span v-if="r.closed_by" class="muted">closed by {{ r.closed_by }}</span>
          </div>
          <p v-if="r.note" class="day-note">{{ r.note }}</p>
        </li>
      </ul>
    </section>
  </div>
</template>

<script setup>
import { ref, computed, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatHours, formatDayLong } from "../utils/dayClose";

const props = defineProps({
  jobId: { type: String, default: "" },
  refreshKey: { type: Number, default: 0 },
  compact: { type: Boolean, default: false },
});

const api = useApiWithToast();
const rows = ref([]);

async function load() {
  const jobId = props.jobId;
  if (!jobId) { rows.value = []; return; }
  try {
    const d = await api.get(`/api/jobs/${jobId}/day-log`, { suppressErrorToast: true });
    if (jobId !== props.jobId) return;
    rows.value = Array.isArray(d?.rows) ? d.rows : [];
  } catch {
    if (jobId !== props.jobId) return;
    rows.value = [];
  }
}
watch(() => [props.jobId, props.refreshKey], load, { immediate: true });

const total = computed(() => rows.value.reduce((t, r) => t + (Number(r?.hours) || 0), 0));

// The server sends rows newest first; keep that order and group by day.
const groups = computed(() => {
  const out = [];
  for (const r of rows.value) {
    const date = String(r?.date || "").slice(0, 10);
    let g = out.find((x) => x.date === date);
    if (!g) { g = { date, rows: [] }; out.push(g); }
    g.rows.push(r);
  }
  return out;
});

defineExpose({ load });
</script>

<style scoped>
.daily-log.compact { padding: 0; }
.daily-log .card-header { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; flex-wrap: wrap; }
.day-group + .day-group { margin-top: 0.75rem; }
.day-label { margin: 0 0 0.25rem; font-size: 0.95rem; }
.day-rows { list-style: none; margin: 0; padding: 0; }
.day-row { padding: 0.45rem 0; border-bottom: 1px solid var(--p-content-border-color); }
.day-row:last-child { border-bottom: none; }
.day-main { display: flex; flex-wrap: wrap; gap: 0.3rem 0.9rem; align-items: baseline; }
.day-note { margin: 0.25rem 0 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.muted { color: var(--p-text-muted-color); }
</style>
