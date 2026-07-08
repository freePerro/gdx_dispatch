<template>
  <!-- Quick-capture a phone-call note without stopping to find a customer.
       One text box + an optional number (live-matched to a customer) + a strip
       of recent inbound calls to tap. Save → a PlannerTask due today. -->
  <Drawer
    v-model:visible="open"
    position="bottom"
    header="Quick note"
    class="capture-drawer"
    @show="onShow"
  >
    <div class="capture-body">
      <Textarea
        ref="noteRef"
        v-model="note"
        class="capture-note"
        rows="3"
        autofocus
        placeholder="What do you need to remember? e.g. “wants 2 openers quoted, call back Thu”"
        aria-label="Call note"
        @keydown.meta.enter="save"
        @keydown.ctrl.enter="save"
      />

      <!-- Recent inbound calls — tap one to attach it. Hidden when the
           phone_com module is off or nothing has come in yet. -->
      <div v-if="recentCalls.length" class="capture-recent">
        <span class="capture-label">Recent calls</span>
        <div class="capture-chips">
          <button
            v-for="c in recentCalls"
            :key="c.call_id"
            type="button"
            class="capture-chip"
            :class="{ selected: selectedCallId === c.call_id }"
            @click="pickCall(c)"
          >
            <span class="capture-chip-who">{{ c.customer_name || formatPhone(c.from_number) }}</span>
            <span class="capture-chip-when">{{ agoLabel(c.started_at) }}</span>
          </button>
        </div>
      </div>

      <div class="capture-phone">
        <span class="capture-label">Phone <span class="capture-optional">(optional)</span></span>
        <InputText
          v-model="phone"
          class="capture-phone-input"
          type="tel"
          inputmode="tel"
          placeholder="(555) 123-4567"
          aria-label="Caller phone number"
          @input="onPhoneInput"
        />
        <p v-if="matchName" class="capture-match capture-match--hit">
          <i class="pi pi-user" aria-hidden="true" /> Matched: {{ matchName }}
        </p>
        <p v-else-if="phone && matchChecked" class="capture-match capture-match--miss">
          No customer on file — the number will be saved with the note.
        </p>
      </div>
    </div>

    <template #footer>
      <div class="capture-footer">
        <Button label="Cancel" text severity="secondary" @click="open = false" />
        <Button
          label="Save note"
          icon="pi pi-check"
          :loading="saving"
          :disabled="!note.trim()"
          @click="save"
        />
      </div>
    </template>
  </Drawer>
</template>

<script setup>
import { ref } from 'vue';
import Drawer from 'primevue/drawer';
import Textarea from 'primevue/textarea';
import InputText from 'primevue/inputtext';
import Button from 'primevue/button';
import { useApiWithToast } from '../composables/useApiWithToast';

const open = defineModel('visible', { type: Boolean, default: false });
const emit = defineEmits(['saved']);

const api = useApiWithToast();

const note = ref('');
const phone = ref('');
const recentCalls = ref([]);
const selectedCallId = ref(null);
const matchName = ref(null);
const matchChecked = ref(false);
const saving = ref(false);

let matchTimer = null;

function reset() {
  note.value = '';
  phone.value = '';
  selectedCallId.value = null;
  matchName.value = null;
  matchChecked.value = false;
}

async function onShow() {
  reset();
  try {
    const data = await api.get('/api/planner/recent-calls?limit=5');
    recentCalls.value = Array.isArray(data?.items) ? data.items : [];
  } catch {
    // phone_com module off or transport hiccup — hide the strip, keep capture.
    recentCalls.value = [];
  }
}

function pickCall(c) {
  if (selectedCallId.value === c.call_id) {
    // Tap again to unselect.
    selectedCallId.value = null;
    if (!matchName.value) phone.value = '';
    return;
  }
  selectedCallId.value = c.call_id;
  phone.value = c.from_number || '';
  matchName.value = c.customer_name || null;
  matchChecked.value = true;
}

function onPhoneInput() {
  // Typing a number breaks any selected-call link and re-runs the matcher.
  selectedCallId.value = null;
  matchName.value = null;
  matchChecked.value = false;
  if (matchTimer) clearTimeout(matchTimer);
  const val = phone.value.trim();
  if (!val) return;
  matchTimer = setTimeout(async () => {
    try {
      const data = await api.get(`/api/planner/match-phone?phone=${encodeURIComponent(val)}`);
      matchName.value = data?.name || null;
      matchChecked.value = true;
    } catch {
      matchChecked.value = true;
    }
  }, 400);
}

async function save() {
  const body = note.value.trim();
  if (!body || saving.value) return;
  saving.value = true;
  // First line becomes the task title; the rest spills into the description.
  const lines = body.split('\n');
  const title = lines[0].slice(0, 300);
  const description = lines.length > 1 ? body : '';
  // Due today so an unhandled note goes overdue tomorrow and surfaces loudly.
  const dueToday = new Date().toISOString().slice(0, 10);
  try {
    await api.post(
      '/api/planner/tasks',
      {
        title,
        description,
        due_date: dueToday,
        source: 'quick_capture',
        contact_phone: phone.value.trim() || null,
        phone_com_call_id: selectedCallId.value || null,
      },
      { successMessage: 'Note saved to your planner' },
    );
    open.value = false;
    emit('saved');
  } catch {
    // useApi already toasted the error; keep the sheet open so nothing is lost.
  } finally {
    saving.value = false;
  }
}

// ── display helpers ──
function formatPhone(raw) {
  if (!raw) return 'Unknown caller';
  const d = String(raw).replace(/\D/g, '').replace(/^1/, '');
  if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return raw;
}

function agoLabel(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}
</script>

<style scoped>
.capture-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.capture-note {
  width: 100%;
  font-size: 1rem; /* ≥16px so iOS Safari doesn't zoom on focus */
  resize: vertical;
}
.capture-label {
  display: block;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin-bottom: var(--space-1);
}
.capture-optional {
  text-transform: none;
  letter-spacing: 0;
  font-weight: 400;
}
.capture-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.capture-chip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.1rem;
  padding: 0.4rem 0.7rem;
  min-height: 44px;
  border: 1px solid var(--border-subtle);
  border-radius: 0.625rem;
  background: var(--surface-elevated);
  color: var(--text-primary);
  cursor: pointer;
}
.capture-chip.selected {
  border-color: var(--interactive-primary);
  background: color-mix(in srgb, var(--interactive-primary) 12%, transparent);
}
.capture-chip-who {
  font-weight: 600;
  font-size: 0.85rem;
}
.capture-chip-when {
  font-size: 0.7rem;
  color: var(--text-muted);
}
.capture-phone-input {
  width: 100%;
  font-size: 1rem; /* ≥16px — no iOS zoom */
}
.capture-match {
  margin: var(--space-1) 0 0;
  font-size: 0.8rem;
}
.capture-match--hit {
  color: var(--interactive-primary);
}
.capture-match--miss {
  color: var(--text-muted);
}
.capture-footer {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
}
</style>

<!-- PrimeVue teleports the Drawer to <body>, so the height override must be
     UNSCOPED (the [data-v-*] scoped attribute never matches the teleported
     panel). Same pattern + rationale as AppBottomNav's More drawer: a bottom
     Drawer defaults to ~10rem, which on a phone shows only the header + a
     sliver — the phone field and recent-calls strip get cut off. The
     0,4,0-specificity mask+panel chain beats the default unambiguously. -->
<style>
.p-drawer-mask.p-drawer-bottom .capture-drawer.p-drawer {
  /* vh fallback first (iOS Safari < 15.4 has no dvh; calc() invalidates the
     whole rule if any unit is unsupported), then upgrade to dvh. */
  height: min(70vh, 34rem);
  height: min(70dvh, 34rem);
  max-height: 90vh;
  max-height: 90dvh;
}
.p-drawer-mask.p-drawer-bottom .capture-drawer.p-drawer .p-drawer-content {
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
</style>
