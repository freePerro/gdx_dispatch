<!--
  JobVisitsCard — a job's visits on its Schedule tab (multi-day jobs plan
  §5.3a). One row per visit, "Day k of n", oldest first; a cancelled visit is
  listed but is not a day of work.

  Reads GET /api/jobs/{jobId}/visits. For a dispatch role (canEdit):
    - Add day(s): POST, picked days or a range, a start time, a length, techs
      (the crew by default).
    - Move an OPEN visit: PATCH with the day, time, length and tech.
    - Remove an OPEN visit: DELETE (the server keeps the row, soft-deleted).
    - Complete an ON SITE visit: POST /api/appointments/{id}/complete.
    - Undo arrival on a visit someone arrived for: UndoArrivalDialog.
  Days and times are the shop's (the list carries its timezone); the server
  converts, so this component never does zone arithmetic on a write.

  Emits `changed` after any write, so the page re-reads the job date and crew.
-->
<template>
  <div class="card" data-testid="job-visits">
    <div class="card-header">
      <h3>Visits</h3>
      <div class="visits-header-actions">
        <slot name="actions" />
        <Button v-if="canEdit" label="Add day(s)" icon="pi pi-plus" severity="secondary"
          data-testid="visits-add" @click="openAdd" />
      </div>
    </div>

    <div v-if="loading" class="spinner-wrap small"><ProgressSpinner /></div>
    <div v-else-if="loadError" class="error-banner" data-testid="visits-load-error">{{ loadError }}</div>
    <p v-else-if="!items.length" class="muted" data-testid="visits-empty">
      No visits booked. Schedule the job, or add a day.
    </p>
    <ul v-else class="visit-list">
      <li v-for="v in items" :key="v.id" class="visit-row" :class="`is-${v.state}`"
        :data-testid="`visit-row-${v.id}`">
        <div class="visit-main">
          <strong class="visit-index">{{ v.day_index ? `Day ${v.day_index} of ${dayCount}` : "Cancelled" }}</strong>
          <span>{{ formatShopDay(v.day) }}</span>
          <span class="muted">{{ timeRange(v) }}</span>
          <span>{{ v.tech_name || (v.tech_id ? "Unknown tech" : "Unassigned") }}</span>
          <Tag :value="STATE_LABEL[v.state] || v.state" :severity="STATE_SEVERITY[v.state] || 'secondary'"
            :data-testid="`visit-state-${v.id}`" />
        </div>
        <div v-if="canEdit" class="visit-actions">
          <template v-if="v.state === 'open'">
            <Button label="Move" icon="pi pi-calendar" text size="small"
              :data-testid="`visit-move-${v.id}`" @click="openMove(v)" />
            <Button label="Remove" icon="pi pi-times" text size="small" severity="danger"
              :data-testid="`visit-remove-${v.id}`" @click="removeVisit(v)" />
          </template>
          <Button v-if="v.state === 'on_site'" label="Complete" icon="pi pi-check" text size="small"
            severity="success" :loading="busyId === v.id"
            :data-testid="`visit-complete-${v.id}`" @click="completeVisit(v)" />
          <Button v-if="v.arrived_at && v.state !== 'cancelled'" label="Undo arrival" icon="pi pi-undo" text
            size="small" severity="secondary"
            :data-testid="`visit-undo-${v.id}`" @click="openUndo(v)" />
        </div>
      </li>
    </ul>

    <!-- Add day(s) -->
    <Dialog v-model:visible="add.visible" modal header="Add day(s)" :style="{ width: '480px' }"
      :breakpoints="{ '768px': '95vw' }">
      <div class="form-section" data-testid="visits-add-dialog">
        <div v-if="add.error" class="error-banner" data-testid="visits-add-error">{{ add.error }}</div>
        <SelectButton v-model="add.mode" :options="ADD_MODES" option-label="label" option-value="value"
          :allow-empty="false" data-testid="visits-add-mode" />
        <div class="form-field">
          <label>{{ add.mode === "range" ? "From – to" : "Days" }}</label>
          <DatePicker v-if="add.mode === 'range'" v-model="add.range" selection-mode="range"
            :min-date="today" :manual-input="false" show-icon data-testid="visits-add-range" />
          <DatePicker v-else v-model="add.days" selection-mode="multiple"
            :min-date="today" :manual-input="false" show-icon data-testid="visits-add-days" />
        </div>
        <label v-if="add.mode === 'range'" class="check-row">
          <Checkbox v-model="add.skipWeekends" binary data-testid="visits-add-skip-weekends" />
          Skip Saturdays and Sundays
        </label>
        <div class="form-row">
          <div class="form-field">
            <label for="visits-add-time">Start</label>
            <InputText id="visits-add-time" v-model="add.startTime" type="time" data-testid="visits-add-time" />
          </div>
          <div class="form-field">
            <label for="visits-add-hours">Hours</label>
            <InputNumber input-id="visits-add-hours" v-model="add.hours" :min="0.25" :max="24"
              :min-fraction-digits="0" :max-fraction-digits="2" data-testid="visits-add-hours" />
          </div>
        </div>
        <div class="form-field">
          <label>Techs</label>
          <MultiSelect v-model="add.techIds" :options="techOptions" option-label="label" option-value="value"
            placeholder="Unassigned" display="chip" data-testid="visits-add-techs" />
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="add.visible = false" />
        <Button label="Book" icon="pi pi-check" :loading="add.busy" :disabled="!addReady"
          data-testid="visits-add-submit" @click="submitAdd" />
      </template>
    </Dialog>

    <!-- Move -->
    <Dialog v-model:visible="move.visible" modal header="Move visit" :style="{ width: '440px' }"
      :breakpoints="{ '768px': '95vw' }">
      <div class="form-section" data-testid="visits-move-dialog">
        <div v-if="move.error" class="error-banner" data-testid="visits-move-error">{{ move.error }}</div>
        <div class="form-field">
          <label>Day</label>
          <DatePicker v-model="move.day" :manual-input="false" show-icon data-testid="visits-move-day" />
        </div>
        <div class="form-row">
          <div class="form-field">
            <label for="visits-move-time">Start</label>
            <InputText id="visits-move-time" v-model="move.startTime" type="time" data-testid="visits-move-time" />
          </div>
          <div class="form-field">
            <label for="visits-move-hours">Hours</label>
            <InputNumber input-id="visits-move-hours" v-model="move.hours" :min="0.25" :max="moveMaxHours"
              :min-fraction-digits="0" :max-fraction-digits="2" data-testid="visits-move-hours" />
          </div>
        </div>
        <div class="form-field">
          <label>Tech</label>
          <Select v-model="move.techId" :options="moveTechOptions" option-label="label" option-value="value"
            data-testid="visits-move-tech" />
        </div>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="move.visible = false" />
        <Button label="Move" icon="pi pi-check" :loading="move.busy" :disabled="!moveReady"
          data-testid="visits-move-submit" @click="submitMove" />
      </template>
    </Dialog>

    <UndoArrivalDialog
      v-model="undo.visible"
      mode="visit"
      :appointment="undo.visit"
      :job-id="jobId"
      :tech-id="undo.visit?.tech_id || ''"
      :tech-name="undo.visit?.tech_name || ''"
      @done="afterWrite()"
      @open-appointments="undo.visible = false; $emit('open-appointments')"
    />
  </div>
</template>

<script setup>
import { computed, reactive, ref, watch } from "vue";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import DatePicker from "primevue/datepicker";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import MultiSelect from "primevue/multiselect";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import SelectButton from "primevue/selectbutton";
import Tag from "primevue/tag";
import UndoArrivalDialog from "./UndoArrivalDialog.vue";
import { useApi } from "../composables/useApi";
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import { localDateString, parseLocalDateString } from "../composables/useFormatters";
import { formatShopDay } from "../utils/visitRefusals";

const props = defineProps({
  jobId: { type: String, required: true },
  technicians: { type: Array, default: () => [] },
  crewTechIds: { type: Array, default: () => [] },
  canEdit: { type: Boolean, default: false },
});
const emit = defineEmits(["changed", "open-appointments"]);

const STATE_LABEL = { open: "Booked", on_site: "On site", closed: "Done", cancelled: "Cancelled" };
const STATE_SEVERITY = { open: "info", on_site: "warn", closed: "success", cancelled: "secondary" };
const ADD_MODES = [{ label: "Pick days", value: "days" }, { label: "Range", value: "range" }];
const DEFAULT_MINUTES = 480;

const api = useApi();
const { confirmAsync } = useDestructiveConfirm();

const items = ref([]);
const dayCount = ref(0);
const timezone = ref("");
const loading = ref(false);
const loadError = ref("");
const busyId = ref("");

function startOfToday() {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}
const today = ref(startOfToday());

async function reload() {
  if (!props.jobId) return;
  loading.value = !items.value.length;
  loadError.value = "";
  try {
    applyList(await api.get(`/api/jobs/${props.jobId}/visits`, { suppressErrorToast: true }));
  } catch (e) {
    loadError.value = e?.message || "Could not load the visits.";
  } finally {
    loading.value = false;
  }
}

function applyList(body) {
  items.value = Array.isArray(body?.items) ? body.items : [];
  dayCount.value = Number(body?.day_count) || 0;
  timezone.value = body?.timezone || "";
}

watch(() => props.jobId, reload, { immediate: true });
defineExpose({ reload });

// ── display, on the shop's clock ─────────────────────────────────────

function clock(iso) {
  if (!iso) return "";
  const opts = { hour: "numeric", minute: "2-digit" };
  try {
    return new Intl.DateTimeFormat(undefined, { ...opts, timeZone: timezone.value || undefined }).format(new Date(iso));
  } catch {
    return new Intl.DateTimeFormat(undefined, opts).format(new Date(iso));
  }
}

function timeRange(v) {
  const start = clock(v.start_at);
  const end = clock(v.end_at);
  return end ? `${start} – ${end}` : start;
}

/** "HH:MM" of an instant in the shop's zone, for the Move dialog. */
function shopTime(iso) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: timezone.value || undefined,
    }).formatToParts(new Date(iso));
    const get = (t) => parts.find((p) => p.type === t)?.value || "00";
    return `${get("hour")}:${get("minute")}`;
  } catch {
    return "08:00";
  }
}

function lengthMinutes(v) {
  const ms = new Date(v?.end_at) - new Date(v?.start_at);
  return Number.isFinite(ms) && ms > 0 ? Math.round(ms / 60000) : null;
}

const techOptions = computed(() => props.technicians
  .filter((t) => t && t.active !== false)
  .map((t) => ({ label: t.name || t.display_name || t.email || `Tech ${String(t.id).slice(0, 8)}`, value: String(t.id) })));

const moveTechOptions = computed(() => {
  const opts = [{ label: "Unassigned", value: null }, ...techOptions.value];
  const current = move.visit?.tech_id;
  if (current && !opts.some((o) => o.value === String(current))) {
    opts.push({ label: move.visit.tech_name || `Tech ${String(current).slice(0, 8)}`, value: String(current) });
  }
  return opts;
});

function minutesOf(hours) {
  const n = Number(hours);
  return Number.isFinite(n) && n > 0 ? Math.round(n * 60) : null;
}

function afterWrite(body) {
  if (body && Array.isArray(body.items)) applyList(body);
  else reload();
  emit("changed");
}

// ── Add day(s) ───────────────────────────────────────────────────────

const add = reactive({
  visible: false, busy: false, error: "", mode: "days",
  days: [], range: null, skipWeekends: true, startTime: "08:00", hours: 8, techIds: [],
});

function lastLive() {
  const live = items.value.filter((v) => v.state !== "cancelled");
  return live[live.length - 1] || null;
}

function openAdd() {
  today.value = startOfToday();
  const last = lastLive();
  Object.assign(add, {
    visible: true, busy: false, error: "", mode: "days", days: [], range: null, skipWeekends: true,
    startTime: last ? shopTime(last.start_at) : "08:00",
    hours: (lengthMinutes(last) || DEFAULT_MINUTES) / 60,
    techIds: props.crewTechIds.map(String).filter(Boolean),
  });
}

const addReady = computed(() => {
  if (!add.startTime || !minutesOf(add.hours)) return false;
  if (add.mode === "range") return Array.isArray(add.range) && !!add.range[0];
  return Array.isArray(add.days) && add.days.length > 0;
});

async function submitAdd() {
  if (!addReady.value) return;
  const body = { start_time: add.startTime, duration_minutes: minutesOf(add.hours), tech_ids: add.techIds };
  if (add.mode === "range") {
    const [from, to] = add.range;
    body.range = { from: localDateString(from), to: localDateString(to || from), skip_weekends: add.skipWeekends };
  } else {
    body.days = add.days.map(localDateString).sort();
  }
  add.busy = true;
  add.error = "";
  try {
    const res = await api.post(`/api/jobs/${props.jobId}/visits`, body, {
      successMessage: "Visits booked", suppressErrorToast: true,
    });
    add.visible = false;
    afterWrite(res);
  } catch (e) {
    // Shown in the dialog: a toast would sit behind the modal.
    add.error = e?.message || "Nothing was booked.";
  } finally {
    add.busy = false;
  }
}

// ── Move ─────────────────────────────────────────────────────────────

const move = reactive({
  visible: false, busy: false, error: "", visit: null, day: null, startTime: "", hours: 8, techId: null,
});

function openMove(v) {
  Object.assign(move, {
    visible: true, busy: false, error: "", visit: v,
    day: parseLocalDateString(String(v.day || "")),
    startTime: shopTime(v.start_at),
    hours: (lengthMinutes(v) || DEFAULT_MINUTES) / 60,
    techId: v.tech_id ? String(v.tech_id) : null,
  });
}

const moveReady = computed(() => !!(move.day && move.startTime && minutesOf(move.hours)));

// A visit booked over 24 hours (man-hours over the crew are not capped)
// keeps its length through a Move. InputNumber clamps to its max on blur,
// so the max is never below the stored length.
const moveMaxHours = computed(() => Math.max(24, (lengthMinutes(move.visit) || 0) / 60));

async function submitMove() {
  if (!moveReady.value || !move.visit) return;
  const body = { day: localDateString(move.day), start_time: move.startTime, tech_id: move.techId };
  // The length goes only when someone changed it, compared in whole
  // minutes: the input re-reads its two-decimal display on blur, so an
  // hours comparison would call an untouched 2000-minute visit changed.
  // Absent, the server keeps the visit's own length.
  const typed = minutesOf(move.hours);
  if (typed !== lengthMinutes(move.visit)) {
    if (typed > 24 * 60) {
      move.error = "A day is at most 24 hours. Book the rest as another day.";
      return;
    }
    body.duration_minutes = typed;
  }
  move.busy = true;
  move.error = "";
  try {
    const res = await api.patch(`/api/jobs/${props.jobId}/visits/${move.visit.id}`, body,
      { successMessage: "Visit moved", suppressErrorToast: true });
    move.visible = false;
    afterWrite(res);
  } catch (e) {
    move.error = e?.message || "The visit was not moved.";
  } finally {
    move.busy = false;
  }
}

// ── Remove / Complete / Undo arrival ─────────────────────────────────

async function removeVisit(v) {
  const ok = await confirmAsync({
    header: "Remove this day?",
    message: `${formatShopDay(v.day)} (${v.tech_name || "unassigned"}) comes off the schedule. The record of it is kept.`,
    acceptLabel: "Remove day",
  });
  if (!ok) return;
  busyId.value = v.id;
  try {
    // A refusal (the job's last booked day) is toasted by useApi.
    afterWrite(await api.del(`/api/jobs/${props.jobId}/visits/${v.id}`, { successMessage: "Day removed" }));
  } catch {
    reload();
  } finally {
    busyId.value = "";
  }
}

async function completeVisit(v) {
  busyId.value = v.id;
  try {
    await api.post(`/api/appointments/${v.id}/complete`, {}, { successMessage: "Visit completed" });
    afterWrite();
  } catch {
    reload();
  } finally {
    busyId.value = "";
  }
}

const undo = reactive({ visible: false, visit: null });
function openUndo(v) {
  undo.visit = { ...v };
  undo.visible = true;
}
</script>

<style scoped>
.visits-header-actions { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.visit-list { list-style: none; margin: 0; padding: 0; }
.visit-row {
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.5rem;
  padding: 0.6rem 0; border-bottom: 1px solid var(--p-content-border-color);
}
.visit-row:last-child { border-bottom: none; }
.visit-row.is-cancelled { opacity: 0.65; }
.visit-main { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem 0.9rem; }
.visit-index { min-width: 6.5rem; }
.visit-actions { display: flex; flex-wrap: wrap; gap: 0.25rem; }
.form-section { display: flex; flex-direction: column; gap: 0.85rem; }
.form-row { display: flex; gap: 0.75rem; flex-wrap: wrap; }
.form-row .form-field { flex: 1 1 8rem; }
.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.check-row { display: flex; align-items: center; gap: 0.5rem; }
.muted { color: var(--p-text-muted-color); }
.error-banner {
  background: var(--color-danger-bg);
  color: var(--color-danger-500);
  border: 1px solid var(--color-danger-border);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
</style>
