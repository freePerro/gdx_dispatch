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
    <div v-if="error" class="error-banner">{{ error }}</div>
    <div class="path-grid">
      <button class="path-card" :class="{ active: path === 'warranty' }"
              @click="path = 'warranty'" data-testid="path-warranty">
        <i class="pi pi-shield" />
        <strong>Warranty / callback</strong>
        <span>Spawn a new linked job. The original stays {{ stateLabel }}.</span>
      </button>
      <button v-if="job?.lifecycle_stage === 'completed'" class="path-card" :class="{ active: path === 'uncomplete' }"
              @click="path = 'uncomplete'" data-testid="path-uncomplete">
        <i class="pi pi-undo" />
        <strong>Un-complete (mistake)</strong>
        <span>Revert this job back to in-progress.</span>
      </button>
      <button v-if="job?.lifecycle_stage === 'cancelled'" class="path-card" :class="{ active: path === 'reactivate' }"
              @click="path = 'reactivate'" data-testid="path-reactivate">
        <i class="pi pi-refresh" />
        <strong>Reactivate (was cancelled in error)</strong>
        <span>Bring this job back to scheduled.</span>
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
      <Button :label="applyLabel" icon="pi pi-check" :disabled="!canSubmit" :loading="busy"
              @click="apply" data-testid="state-override-apply" />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Calendar from "primevue/calendar";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  job: { type: Object, default: null },
});
const emit = defineEmits(["update:modelValue", "applied", "cancel"]);

const api = useApi();
const path = ref("");
const reason = ref("");
const scheduledAt = ref(null);
const overrideTitle = ref("");
const busy = ref(false);
const error = ref("");

const jobTitle = computed(() => props.job?.title || "");
const stateLabel = computed(() =>
  props.job?.lifecycle_stage === "cancelled" ? "cancelled" : "completed",
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
  }
});

function cancel() {
  emit("cancel");
  emit("update:modelValue", false);
}

async function apply() {
  if (!props.job?.id) return;
  busy.value = true;
  error.value = "";
  try {
    let result = null;
    const id = props.job.id;
    if (path.value === "warranty") {
      result = await api.post(`/api/jobs/${id}/spawn-return-visit`, {
        reason: reason.value || null,
        scheduled_at: scheduledAt.value || null,
        title: overrideTitle.value || null,
      }, { successMessage: "Return visit created" });
    } else if (path.value === "uncomplete") {
      result = await api.post(`/api/jobs/${id}/uncomplete`, {
        reason: reason.value,
        scheduled_at: scheduledAt.value || null,
      }, { successMessage: "Job re-opened" });
    } else if (path.value === "reactivate") {
      result = await api.post(`/api/jobs/${id}/reactivate`, {
        reason: reason.value,
        scheduled_at: scheduledAt.value || null,
      }, { successMessage: "Job reactivated" });
    } else if (path.value === "other") {
      // "Other" defaults to un-complete-style override on a completed job,
      // reactivate-style on a cancelled one — the audit row carries the reason.
      const target = props.job.lifecycle_stage === "cancelled" ? "reactivate" : "uncomplete";
      result = await api.post(`/api/jobs/${id}/${target}`, {
        reason: `[other] ${reason.value}`,
        scheduled_at: scheduledAt.value || null,
      }, { successMessage: "Override applied" });
    }
    emit("applied", { result, path: path.value });
    emit("update:modelValue", false);
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || "Action failed";
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
.error-banner {
  background: var(--p-red-50);
  color: var(--p-red-700);
  border: 1px solid var(--p-red-200);
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  margin-bottom: 0.5rem;
}
</style>
