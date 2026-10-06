<!--
  JobStateOverrideDialog — UX audit F-32 / 2026-04-29.

  Intercept point when someone tries to put a completed or cancelled
  job back on the schedule. Three named paths, one cancel, plus a
  required reason note for the non-warranty paths so reporting can
  later split warranty work from accidental un-completes from
  legitimate "other" cases.

  Per Doug 2026-04-29: "Other reason" does NOT require admin role —
  anyone can pick it — but the note IS mandatory: "otherwise people
  will find work arounds for it. and we want the real data."

  Emits:
    - applied: { newJobId? } when the action succeeded; parent reloads.
    - cancel: dialog closed without action.
-->
<template>
  <Dialog
    :visible="modelValue"
    @update:visible="$emit('update:modelValue', $event)"
    modal
    :style="{ width: '600px' }"
    :breakpoints="{ '768px': '95vw' }"
    :header="`'${jobTitle || 'Job'}' is ${stateLabel} — what should we do?`"
  >
    <div v-if="error" class="error-banner" data-testid="state-override-error">
      {{ error }}
      <RouterLink v-if="errorToAppointments" to="/appointments" data-testid="state-override-appointments">
        Open the Appointments page
      </RouterLink>
    </div>
    <!-- The re-open's question (multi-day jobs plan §5.2a, R0): a crew tech
         already holds a closed visit on the new day. Asked, never guessed. -->
    <div v-if="rebook" class="question-banner" data-testid="rebook-question">
      <p>{{ rebook.text }}</p>
      <p class="muted">No leaves them unbooked on that day: their tap will find no visit.</p>
      <div class="question-actions">
        <Button label="No" severity="secondary" :loading="busy" data-testid="rebook-no"
                @click="answerRebook(false)" />
        <Button label="Yes, book them again" :loading="busy" data-testid="rebook-yes"
                @click="answerRebook(true)" />
      </div>
    </div>
    <div class="path-grid">
      <button class="path-card" :class="{ active: path === 'warranty' }"
              @click="path = 'warranty'" data-testid="path-warranty">
        <i class="pi pi-shield" />
        <strong>Warranty / callback</strong>
        <span>Spawn a new linked job. The original stays {{ stateLabel }}.</span>
      </button>
      <button v-if="storedStage === 'completed'" class="path-card" :class="{ active: path === 'uncomplete' }"
              @click="path = 'uncomplete'" data-testid="path-uncomplete">
        <i class="pi pi-undo" />
        <strong>Un-complete (mistake)</strong>
        <span>Revert this job back to in-progress.</span>
      </button>
      <button v-if="storedStage === 'cancelled'" class="path-card" :class="{ active: path === 'reactivate' }"
              @click="path = 'reactivate'" data-testid="path-reactivate">
        <i class="pi pi-refresh" />
        <strong>Reactivate (was cancelled in error)</strong>
        <span>Bring this job back. With a date it is scheduled; without one it goes back to New Jobs to Schedule.</span>
      </button>
      <button class="path-card" :class="{ active: path === 'other' }"
              @click="path = 'other'" data-testid="path-other">
        <i class="pi pi-question-circle" />
        <strong>Other reason</strong>
        <span>Note required so we keep real data on why this happened.</span>
      </button>
    </div>

    <div v-if="path" class="form-section">
      <div v-if="needsReason" class="form-field">
        <label for="state-override-reason">
          Reason <span class="required">*</span>
        </label>
        <Textarea
          id="state-override-reason" v-model="reason" rows="2"
          placeholder="At least 4 characters. The team will see this in the audit log."
          data-testid="state-override-reason"
        />
      </div>
      <div v-if="needsSchedule" class="form-field">
        <label for="state-override-when">When (optional)</label>
        <Calendar id="state-override-when" v-model="scheduledAt" showTime hourFormat="12"
                  data-testid="state-override-when" />
      </div>
      <div v-if="path === 'warranty'" class="form-field">
        <label>Title</label>
        <InputText v-model="overrideTitle" :placeholder="`Return visit: ${jobTitle}`" />
      </div>
    </div>

    <template #footer>
      <Button label="Cancel" severity="secondary" @click="cancel" data-testid="state-override-cancel" />
      <Button :label="applyLabel" icon="pi pi-check" :disabled="!canSubmit || !!rebook" :loading="busy"
              @click="apply" data-testid="state-override-apply" />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { isNeedsAnswer, pointsAtAppointments, rebookQuestion, refusalOf } from "../utils/visitRefusals";
import Button from "primevue/button";
import Calendar from "primevue/calendar";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  job: { type: Object, default: null },
  // For naming techs in the re-open question; falls back to GET /api/technicians.
  technicians: { type: Array, default: () => [] },
});
const emit = defineEmits(["update:modelValue", "applied", "cancel"]);

const api = useApi();
const path = ref("");
const reason = ref("");
const scheduledAt = ref(null);
const overrideTitle = ref("");
const busy = ref(false);
const error = ref("");
const errorToAppointments = ref(false);
const rebook = ref(null); // { text, tech_ids, day } while the question is open

const jobTitle = computed(() => props.job?.title || "");
// /api/jobs/{id} overwrites `lifecycle_stage` with the display label
// ("Complete", "Cancelled") and carries the stored enum value in
// `lifecycle_stage_raw`. /uncomplete and /reactivate gate on the stored value,
// so this dialog must too — comparing the label against 'completed' meant
// Un-complete and Reactivate never rendered, and "Other" on a cancelled job
// posted /uncomplete and got a 409. Normalized so a payload that still sends
// either form resolves the same way.
const STAGE_ALIASES = { complete: "completed", canceled: "cancelled" };
const storedStage = computed(() => {
  const s = String(props.job?.lifecycle_stage_raw || props.job?.lifecycle_stage || "")
    .trim().toLowerCase();
  return STAGE_ALIASES[s] || s;
});
const stateLabel = computed(() =>
  storedStage.value === "cancelled" ? "cancelled" : "completed",
);
const needsReason = computed(() => path.value !== "warranty");
const needsSchedule = computed(() =>
  path.value === "uncomplete" || path.value === "reactivate" || path.value === "warranty",
);
const applyLabel = computed(() => {
  if (path.value === "warranty") return "Spawn return visit";
  if (path.value === "uncomplete") return "Un-complete job";
  if (path.value === "reactivate") return "Reactivate job";
  if (path.value === "other") return "Apply with reason";
  return "Apply";
});
const canSubmit = computed(() => {
  if (!path.value) return false;
  if (needsReason.value && reason.value.trim().length < 4) return false;
  return true;
});

watch(() => props.modelValue, (v) => {
  if (v) {
    path.value = "";
    reason.value = "";
    scheduledAt.value = null;
    overrideTitle.value = "";
    error.value = "";
    errorToAppointments.value = false;
    rebook.value = null;
  }
});
// A changed path or date is a different request: its answer is not this one.
watch([path, scheduledAt], () => { rebook.value = null; });

async function techNames(ids) {
  let list = props.technicians || [];
  const missing = (ids || []).some((id) => !list.find((t) => String(t.id) === String(id)));
  if (missing) {
    try {
      const data = await api.get("/api/technicians", { suppressErrorToast: true });
      list = Array.isArray(data) ? data : (data?.items || data?.data || []);
    } catch { /* fall back to a generic name */ }
  }
  return (ids || []).map((id) => {
    const t = list.find((x) => String(x.id) === String(id));
    return t ? (t.name || t.display_name || t.email || "A crew tech") : "A crew tech";
  });
}

function cancel() {
  emit("cancel");
  emit("update:modelValue", false);
}

async function apply() {
  await send(undefined);
}

async function answerRebook(answer) {
  await send(answer);
}

async function send(rebookAnswer) {
  if (!props.job?.id) return;
  busy.value = true;
  error.value = "";
  errorToAppointments.value = false;
  try {
    let result = null;
    const id = props.job.id;
    if (path.value === "warranty") {
      result = await api.post(`/api/jobs/${id}/spawn-return-visit`, {
        reason: reason.value || null,
        scheduled_at: scheduledAt.value || null,
        title: overrideTitle.value || null,
      }, { successMessage: "Return visit created" });
    } else {
      // Un-complete / Reactivate, and "Other" — which defaults to the
      // un-complete-style override on a completed job, reactivate-style on a
      // cancelled one; the audit row carries the reason.
      let target = path.value;
      let note = reason.value;
      let message = path.value === "uncomplete" ? "Job re-opened" : "Job reactivated";
      if (path.value === "other") {
        target = storedStage.value === "cancelled" ? "reactivate" : "uncomplete";
        note = `[other] ${reason.value}`;
        message = "Override applied";
      }
      const body = { reason: note, scheduled_at: scheduledAt.value || null };
      if (rebookAnswer !== undefined) body.rebook_closed_day = rebookAnswer;
      // The api toast is suppressed: a needs_answer is a question, not an
      // error, and every other failure shows in this dialog's banner.
      result = await api.post(`/api/jobs/${id}/${target}`, body,
        { successMessage: message, suppressErrorToast: true });
    }
    rebook.value = null;
    emit("applied", { result, path: path.value });
    emit("update:modelValue", false);
  } catch (e) {
    const refusal = refusalOf(e);
    if (isNeedsAnswer(e, "rebook_closed_day")) {
      const names = await techNames(refusal.tech_ids || []);
      rebook.value = { text: rebookQuestion(names, refusal.day), tech_ids: refusal.tech_ids, day: refusal.day };
    } else {
      rebook.value = null;
      error.value = refusal?.detail || e?.response?.data?.detail || e?.message || "Action failed";
      errorToAppointments.value = pointsAtAppointments(refusal);
    }
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
/*
  Tokens migrated 2026-05-10 from pre-v4 surface/primary/red names to v4
  p-prefixed tokens per gdx/docs/frontend_view_pattern.md. Pre-fix the
  legacy names had hex fallbacks (white, light-gray) which overrode dark
  mode entirely; Doug saw white-on-white path cards.
*/
.path-grid {
  display: grid;
  gap: 0.6rem;
  margin: 0.5rem 0 1rem 0;
}
.path-card {
  /* 32px icon column + flexible content column. Strong+span both pinned
     to col 2; without grid-column they auto-flow into col 1 (32px wide)
     and force every word to wrap onto its own line. */
  display: grid;
  grid-template-columns: 32px 1fr;
  grid-template-rows: auto auto;
  align-items: start;
  column-gap: 0.65rem;
  row-gap: 0.15rem;
  text-align: left;
  padding: 0.75rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  background: var(--p-content-background);
  color: var(--p-text-color);
  cursor: pointer;
  transition: background 0.1s, border-color 0.1s;
  font: inherit;
  width: 100%;
}
.path-card:focus-visible {
  outline: 2px solid var(--p-primary-color);
  outline-offset: 2px;
}
.path-card i {
  font-size: 1.4rem;
  color: var(--p-primary-color);
  grid-column: 1;
  grid-row: 1 / span 2;
  align-self: center;
}
.path-card strong {
  grid-column: 2;
  grid-row: 1;
  color: var(--p-text-color);
  font-size: 0.95rem;
  line-height: 1.3;
}
.path-card span {
  grid-column: 2;
  grid-row: 2;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
  line-height: 1.35;
}
.path-card:hover {
  /* --p-surface-100 (the doc-recommended hover bg) is light-gray in BOTH
     themes, so on dark mode the hover state inverts to bright-on-dark
     and looks like a selected/disabled card. --p-content-hover-background
     is the theme-aware token (dark zinc-800 in dark mode, light gray in
     light mode). */
  background: var(--p-content-hover-background);
  color: var(--p-content-hover-color);
}
.path-card.active {
  border-color: var(--p-primary-color);
  background: var(--p-highlight-background);
  color: var(--p-highlight-color);
}
.path-card.active strong,
.path-card.active span {
  color: var(--p-highlight-color);
}
.form-section {
  display: grid; gap: 0.75rem;
  margin-top: 0.5rem;
}
.form-field { display: grid; gap: 0.25rem; }
.form-field label { color: var(--p-text-color); font-size: 0.9rem; font-weight: 500; }
.required { color: var(--p-red-500); }
.question-banner {
  border: 1px solid var(--p-content-border-color);
  background: var(--p-content-background);
  color: var(--p-text-color);
  padding: 0.6rem 0.75rem;
  border-radius: 6px;
  margin-bottom: 0.5rem;
}
.question-banner p { margin: 0 0 0.4rem 0; }
.question-banner .muted { color: var(--p-text-muted-color); font-size: 0.85rem; }
.question-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
.error-banner {
  background: var(--color-danger-bg);
  color: var(--color-danger-500);
  border: 1px solid var(--color-danger-border);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  margin-bottom: 0.5rem;
}
</style>
