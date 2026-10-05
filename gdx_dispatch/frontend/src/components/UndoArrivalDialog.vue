<!--
  UndoArrivalDialog — multi-day jobs plan §5.2a, *Arrival is always recorded*,
  "Undo arrival": a wrong arrival is undone by asking, reason required.

  Two modes, one dialog:
    - visit: an Appointments-page row with an arrival (arrived_at set, or an
      old status-only "arrived"). GET/POST /api/appointments/{id}/undo-arrival.
      The preview says whether the visit's tap was found; when it was not,
      the office may name it from the tech's unmatched taps. Later taps of the
      same tech that day are asked about too (`also_undo`).
    - job: the Job Detail crew row, for taps no visit holds.
      GET/POST /api/jobs/{job_id}/undo-arrival (tech_id, tap_record_ids).

  The 200 carries `not_reverted: [{field, reason}]`; it is shown, never
  swallowed. Hours are not touched: a clock-in the tap opened stays.

  Emits:
    - done: { result } after a successful undo (parent reloads).
-->
<template>
  <Dialog
    :visible="modelValue"
    @update:visible="$emit('update:modelValue', $event)"
    modal
    :style="{ width: '560px' }"
    :breakpoints="{ '768px': '95vw' }"
    header="Undo arrival"
  >
    <div v-if="loading" class="muted" data-testid="undo-loading">Loading…</div>
    <div v-else-if="loadError" class="error-banner" data-testid="undo-load-error">{{ loadError }}</div>

    <!-- After the undo: what was done, and what was not put back. -->
    <div v-else-if="result" class="form-section" data-testid="undo-result">
      <p class="lead">Arrival undone.</p>
      <ul v-if="notReverted.length" class="not-reverted" data-testid="undo-not-reverted">
        <li v-for="line in notReverted" :key="line">{{ line }}</li>
      </ul>
      <p class="muted">Any clock-in the tap opened stays — fix it on Timesheets.</p>
      <div v-if="recordOffers.length" class="form-field" data-testid="undo-record-offers">
        <span>If the crew really arrived at one of these times, record it on the visit:</span>
        <div class="offer-row">
          <Button
            v-for="t in recordOffers" :key="t.id"
            :label="`Record arrival at ${formatTapTime(t.arrived_at)}`"
            size="small" severity="secondary"
            :loading="recording === t.id"
            :data-testid="`undo-record-${t.id}`"
            @click="recordArrival(t)"
          />
        </div>
      </div>
      <div v-if="error" class="error-banner">{{ error }}</div>
    </div>

    <div v-else class="form-section">
      <div v-if="error" class="error-banner" data-testid="undo-error">{{ error }}</div>

      <!-- ── visit mode ── -->
      <template v-if="mode === 'visit'">
        <p class="lead" data-testid="undo-question">{{ visitQuestion }}</p>
        <p v-if="preview?.tap" class="muted" data-testid="undo-matched-tap">
          The tap at {{ formatTapTime(preview.tap.arrived_at) }} is undone with it.
        </p>
        <div v-else-if="unmatched.length" class="form-field" data-testid="undo-pick-tap">
          <span>The tap that wrote this arrival was not found. Was it one of these?</span>
          <label v-for="t in unmatched" :key="t.id" class="choice">
            <RadioButton v-model="pickedTap" :value="t.id" :input-id="`undo-tap-${t.id}`"
                         :data-testid="`undo-tap-${t.id}`" />
            <span>{{ formatShopDay(t.day) }} at {{ formatTapTime(t.arrived_at) }}</span>
          </label>
          <label class="choice">
            <RadioButton v-model="pickedTap" :value="NONE" input-id="undo-tap-none"
                         data-testid="undo-tap-none" />
            <span>None of these</span>
          </label>
        </div>
      </template>

      <!-- ── job (crew row) mode ── -->
      <template v-else>
        <div v-if="!unmatched.length" class="muted" data-testid="undo-no-taps">
          No tap to undo here. Each of {{ who }}'s taps stamped a visit — undo the
          arrival on that visit on the
          <a href="/appointments" @click.prevent="$emit('open-appointments')">Appointments page</a>.
        </div>
        <div v-else class="form-field" data-testid="undo-job-taps">
          <span>Which of {{ who }}'s taps on this job were wrong?</span>
          <label v-for="t in unmatched" :key="t.id" class="choice">
            <Checkbox v-model="jobTaps" :value="t.id" :input-id="`undo-jobtap-${t.id}`"
                      :data-testid="`undo-jobtap-${t.id}`" />
            <span>{{ formatShopDay(t.day) }} at {{ formatTapTime(t.arrived_at) }}</span>
          </label>
        </div>
      </template>

      <div v-if="laterTaps.length" class="form-field" data-testid="undo-later-taps">
        <span>{{ laterQuestion }}</span>
        <label v-for="t in laterTaps" :key="t.id" class="choice">
          <Checkbox v-model="alsoUndo" :value="t.id" :input-id="`undo-later-${t.id}`"
                    :data-testid="`undo-later-${t.id}`" />
          <span>{{ formatTapTime(t.arrived_at) }}</span>
        </label>
      </div>

      <p v-if="canSubmitShape" class="muted">Any clock-in the tap opened stays — fix it on Timesheets.</p>

      <div v-if="canSubmitShape" class="form-field">
        <label for="undo-arrival-reason">Reason <span class="required">*</span></label>
        <Textarea id="undo-arrival-reason" v-model="reason" rows="2"
                  placeholder="Why was this arrival wrong? Kept in the audit log."
                  data-testid="undo-reason" />
      </div>
    </div>

    <template #footer>
      <template v-if="result">
        <Button label="Done" data-testid="undo-close" @click="close" />
      </template>
      <template v-else>
        <Button label="Cancel" severity="secondary" data-testid="undo-cancel" @click="close" />
        <Button label="Undo arrival" icon="pi pi-undo" severity="danger"
                :disabled="!canSubmit" :loading="busy"
                data-testid="undo-submit" @click="submit" />
      </template>
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import {
  formatClock, formatShopDay, formatTapTime, joinNames, notRevertedLines,
} from "../utils/visitRefusals";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Dialog from "primevue/dialog";
import RadioButton from "primevue/radiobutton";
import Textarea from "primevue/textarea";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  mode: { type: String, default: "visit" }, // "visit" | "job"
  appointment: { type: Object, default: null },
  jobId: { type: String, default: "" },
  techId: { type: String, default: "" },
  techName: { type: String, default: "" },
});
const emit = defineEmits(["update:modelValue", "done", "open-appointments"]);

const NONE = "__none__";
const api = useApi();
const loading = ref(false);
const loadError = ref("");
const preview = ref(null);
const pickedTap = ref(NONE);
const alsoUndo = ref([]);
const jobTaps = ref([]);
const reason = ref("");
const busy = ref(false);
const error = ref("");
const result = ref(null);
const recording = ref("");
// The visit as it was when the dialog opened: the parent reloads its list on
// `done`, which may swap the prop out from under the result view.
const visit = ref(null);

const who = computed(() => props.techName || "the tech");
const unmatched = computed(() => preview.value?.unmatched_taps || []);

const visitQuestion = computed(() => {
  const at = preview.value?.arrived_at;
  const whose = props.techName ? `${props.techName}'s arrival` : "the arrival";
  return at
    ? `Undo ${whose} at ${formatClock(at)}?`
    : `Undo ${whose}? No arrival time was recorded.`;
});

// The tap being undone in visit mode: the matched one, or the one picked.
const undoneTapId = computed(() => {
  if (props.mode !== "visit") return null;
  if (preview.value?.tap) return preview.value.tap.id;
  return pickedTap.value !== NONE ? pickedTap.value : null;
});
const laterTaps = computed(() => {
  const id = undoneTapId.value;
  if (!id) return [];
  return preview.value?.later_taps?.[id] || [];
});
const laterQuestion = computed(() => {
  const times = joinNames(laterTaps.value.map((t) => formatTapTime(t.arrived_at)));
  return `${props.techName || "The tech"} also tapped at ${times} — were these wrong too?`;
});

const canSubmitShape = computed(() => {
  if (!preview.value) return false;
  if (props.mode === "job") return unmatched.value.length > 0;
  return true;
});
const canSubmit = computed(() => {
  if (!canSubmitShape.value || busy.value) return false;
  if (!reason.value.trim()) return false;
  if (props.mode === "job" && !jobTaps.value.length) return false;
  return true;
});

const notReverted = computed(() => notRevertedLines(result.value?.not_reverted));

// Later taps the office kept as real: the visit now has no arrival, so offer
// to record one of those times through office Arrived (`source: manual`).
// A visit is never restamped from a tap on its own (plan §5.2a).
const recordOffers = computed(() => {
  if (props.mode !== "visit" || !result.value) return [];
  const status = String(visit.value?.status || "").toLowerCase();
  if (status === "completed" || status === "cancelled") return [];
  const undone = new Set(result.value.tap_record_ids || []);
  return laterTaps.value.filter((t) => !undone.has(t.id));
});

function reset() {
  preview.value = null;
  loadError.value = "";
  pickedTap.value = NONE;
  alsoUndo.value = [];
  jobTaps.value = [];
  reason.value = "";
  error.value = "";
  result.value = null;
  recording.value = "";
  visit.value = props.appointment ? { ...props.appointment } : null;
}

async function load() {
  reset();
  loading.value = true;
  try {
    if (props.mode === "job") {
      const q = new URLSearchParams({ tech_id: String(props.techId || "") });
      preview.value = await api.get(`/api/jobs/${props.jobId}/undo-arrival?${q.toString()}`,
        { suppressErrorToast: true });
    } else {
      preview.value = await api.get(`/api/appointments/${props.appointment?.id}/undo-arrival`,
        { suppressErrorToast: true });
    }
  } catch (e) {
    loadError.value = e?.message || "Couldn't load this arrival.";
  } finally {
    loading.value = false;
  }
}

watch(() => props.modelValue, (open) => { if (open) load(); }, { immediate: true });
// A different tap picked means a different set of later taps.
watch(pickedTap, () => { alsoUndo.value = []; });

function close() {
  emit("update:modelValue", false);
}

async function submit() {
  if (!canSubmit.value) return;
  busy.value = true;
  error.value = "";
  try {
    let res;
    if (props.mode === "job") {
      res = await api.post(`/api/jobs/${props.jobId}/undo-arrival`, {
        tech_id: String(props.techId),
        tap_record_ids: jobTaps.value,
        reason: reason.value.trim(),
      }, { suppressErrorToast: true, successMessage: "Arrival undone" });
    } else {
      const body = { reason: reason.value.trim(), also_undo: alsoUndo.value };
      if (!preview.value?.tap && undoneTapId.value) body.tap_record_id = undoneTapId.value;
      res = await api.post(`/api/appointments/${visit.value?.id}/undo-arrival`, body,
        { suppressErrorToast: true, successMessage: "Arrival undone" });
    }
    result.value = res || { not_reverted: [], tap_record_ids: [] };
    emit("done", { result: result.value });
  } catch (e) {
    error.value = e?.message || "Couldn't undo the arrival.";
  } finally {
    busy.value = false;
  }
}

async function recordArrival(tap) {
  if (!visit.value?.id) return;
  recording.value = tap.id;
  error.value = "";
  try {
    await api.post(`/api/appointments/${visit.value.id}/arrived`, { arrived_at: tap.arrived_at },
      { suppressErrorToast: true, successMessage: "Arrival recorded" });
    emit("done", { result: result.value, recorded: tap.arrived_at });
    close();
  } catch (e) {
    error.value = e?.message || "Couldn't record the arrival.";
  } finally {
    recording.value = "";
  }
}
</script>

<style scoped>
.form-section { display: grid; gap: 0.75rem; }
.form-field { display: grid; gap: 0.35rem; }
.form-field label { color: var(--p-text-color); font-size: 0.9rem; font-weight: 500; }
.form-field .choice { display: flex; align-items: center; gap: 0.5rem; font-weight: 400; }
.lead { margin: 0; color: var(--p-text-color); font-weight: 600; }
.muted { margin: 0; color: var(--p-text-muted-color); font-size: 0.85rem; }
.offer-row { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.not-reverted { margin: 0; padding-left: 1.1rem; color: var(--p-text-color); }
.required { color: var(--p-red-500); }
.error-banner {
  background: var(--color-danger-bg);
  color: var(--color-danger-500);
  border: 1px solid var(--color-danger-border);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
</style>
