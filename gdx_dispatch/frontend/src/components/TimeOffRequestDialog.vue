<!--
  TimeOffRequestDialog — a tech asks for paid days off.

  Self-service only: the request is always the caller's own (POST omits
  technician_id; the backend resolves "me"). The note is REQUIRED, mirroring
  the server rule for any self-service clock write, and the day length is
  capped at the shop's default, which is read from
  GET /api/timeclock/time-off/options rather than guessed — a guess that
  disagrees with the server's cap is a 422 the tech cannot understand.

  The office rules on it from /timesheets; approval writes the timeclock
  entries. Nothing here touches the clock record.

  `mobile` renders it as a bottom sheet (the pattern MobileBillingView and
  MobileInboxView use) so the thumb reaches the Save button.
-->
<template>
  <Dialog
    :visible="visible"
    header="Request time off"
    modal
    :position="mobile ? 'bottom' : 'center'"
    :style="mobile ? { width: '100vw', maxWidth: '100vw', margin: 0 } : { width: 'min(28rem, 95vw)' }"
    class="time-off-dialog"
    data-testid="time-off-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <div class="form-grid">
      <div class="form-row">
        <div class="form-field">
          <label for="to-start">First day</label>
          <DatePicker
            id="to-start"
            v-model="form.start"
            showIcon
            :showOnFocus="false"
            dateFormat="yy-mm-dd"
            :minDate="minDate"
            data-testid="to-start"
          />
        </div>
        <div class="form-field">
          <label for="to-end">Last day</label>
          <DatePicker
            id="to-end"
            v-model="form.end"
            showIcon
            :showOnFocus="false"
            dateFormat="yy-mm-dd"
            :minDate="form.start || minDate"
            data-testid="to-end"
          />
        </div>
      </div>
      <div class="form-field">
        <label for="to-hours">Hours per day</label>
        <InputNumber
          id="to-hours"
          v-model="form.hours"
          :min="0.5"
          :max="maxHours"
          :step="0.5"
          :minFractionDigits="0"
          :maxFractionDigits="2"
          showButtons
          data-testid="to-hours"
        />
        <small class="field-hint">
          A full day here is {{ maxHours }} hours. Only the shop's workdays count —
          <span v-if="workdayCount != null" data-testid="to-workday-preview">
            {{ workdayCount }} {{ workdayCount === 1 ? 'workday' : 'workdays' }} in this range.
          </span>
        </small>
      </div>
      <div class="form-field">
        <label for="to-notes">Reason<span class="required-mark"> *</span></label>
        <Textarea id="to-notes" v-model="form.notes" rows="2" data-testid="to-notes" />
        <small class="field-hint">
          Required. The office sees it, and it is written to the record with your name on it.
        </small>
      </div>
      <Message v-if="error" severity="error" :closable="false" data-testid="to-error">
        {{ error }}
      </Message>
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="$emit('update:visible', false)" />
      <Button
        label="Send request"
        icon="pi pi-send"
        :loading="saving"
        :disabled="!canSave"
        data-testid="to-save"
        @click="save"
      />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue';
import Button from 'primevue/button';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import Message from 'primevue/message';
import Textarea from 'primevue/textarea';
import { useApi } from '../composables/useApi';
import { localDateString } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  /** Bottom-sheet presentation for the phone. */
  mobile: { type: Boolean, default: false },
});
const emit = defineEmits(['update:visible', 'saved']);

const api = useApi();

// Mon=1 … Sun=64, the AppSettings.default_workdays bitmask. Mon–Fri until the
// options load says otherwise. Preview only — the server decides for real,
// honouring a per-person override this form cannot see.
const WEEKDAY_BITS = [1, 2, 4, 8, 16, 32, 64];
const options = ref({ default_minutes: 480, workdays: 31, self_service_backdate_days: 14 });

const form = reactive({ start: null, end: null, hours: 8, notes: '' });
const saving = ref(false);
const error = ref('');

const maxHours = computed(() => Math.max(0.5, (options.value.default_minutes || 480) / 60));

// Older than the self-service window is entered by the office (server 422).
const minDate = computed(() => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - (options.value.self_service_backdate_days ?? 14));
  return d;
});

const workdayCount = computed(() => {
  if (!form.start || !form.end || form.end < form.start) return null;
  const mask = Number(options.value.workdays ?? 31);
  let n = 0;
  const cursor = new Date(form.start);
  cursor.setHours(0, 0, 0, 0);
  const last = new Date(form.end);
  last.setHours(0, 0, 0, 0);
  for (let guard = 0; cursor <= last && guard < 400; guard += 1) {
    if (mask & WEEKDAY_BITS[(cursor.getDay() + 6) % 7]) n += 1;
    cursor.setDate(cursor.getDate() + 1);
  }
  return n;
});

const canSave = computed(() => {
  if (saving.value) return false;
  if (!form.start || !form.end || form.end < form.start) return false;
  if (!(form.hours > 0)) return false;
  return !!form.notes.trim();
});

watch(
  () => props.visible,
  async (open) => {
    if (!open) return;
    error.value = '';
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    Object.assign(form, { start: today, end: today, notes: '' });
    try {
      const opts = await api.get('/api/timeclock/time-off/options', { suppressErrorToast: true });
      if (opts && typeof opts === 'object') options.value = { ...options.value, ...opts };
    } catch {
      // Defaults stand; the server still enforces its own cap.
    }
    form.hours = maxHours.value;
  },
  // immediate: a parent may mount this already open (tests do; a deep link
  // could). Without it the form would sit empty with Send disabled.
  { immediate: true },
);

async function save() {
  saving.value = true;
  error.value = '';
  try {
    const created = await api.post(
      '/api/timeclock/time-off/requests',
      {
        // Calendar dates, shop-local by construction: the picker's fields
        // are what the tech meant, and the server expands workdays itself.
        start_date: localDateString(form.start),
        end_date: localDateString(form.end),
        minutes_per_day: Math.round(Number(form.hours) * 60),
        notes: form.notes.trim(),
      },
      { successMessage: 'Time off requested — the office will review it', suppressErrorToast: true },
    );
    emit('update:visible', false);
    emit('saved', created);
  } catch (e) {
    const detail = e?.body?.detail;
    error.value = (typeof detail === 'string' && detail) || e?.message || 'The request was not sent';
  } finally {
    saving.value = false;
  }
}
</script>

<style scoped>
.form-grid {
  display: grid;
  gap: 0.75rem;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}
.form-field {
  display: grid;
  gap: 0.25rem;
}
.field-hint {
  color: var(--p-text-muted-color);
  font-size: 0.78rem;
}
.required-mark {
  color: var(--p-red-500);
}
</style>
