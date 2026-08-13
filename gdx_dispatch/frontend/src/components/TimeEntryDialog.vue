<!--
  TimeEntryDialog — the ONE dialog that corrects or adds a timeclock entry.

  Extracted from TimesheetsView (2026-08-13) so the tech's own weekly timesheet
  and the office page share a single writer to PATCH/POST
  /api/timeclock/entries — the canSave rules below were all browser-found, and
  two copies of them would drift.

  Two modes:
   - office (default): technician picker on create, read-only name on edit,
     note recommended. This is the /timesheets behavior, unchanged.
   - selfMode: the entry is always the caller's own. No technician field
     (POST omits technician_id; the backend resolves "me"), and the note is
     REQUIRED — mirroring the server rule: a tech's correction to their own
     clock record needs the why on the row, and the backend also holds
     self-edits to a recent window (the 422 renders in the dialog's error
     slot if a row slips past the page's own affordance gating).

  Every stamp means SHOP time (Settings → Time Clock → Tenant timezone) —
  display, the DatePicker's input, and what goes on the wire. Conversion lives
  in useTenantTimezone (toShopWallClock / shopWallClockToIso), shared with the
  pages that open this dialog.
-->
<template>
  <Dialog
    :visible="visible"
    :header="isNew ? 'Add a time entry' : 'Correct this shift'"
    modal
    class="entry-dialog"
    :style="{ width: 'min(30rem, 95vw)' }"
    data-testid="entry-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <div class="form-grid">
      <div v-if="!selfMode" class="form-field">
        <label for="entry-tech">Technician</label>
        <Select
          v-if="isNew"
          id="entry-tech"
          v-model="editing.technicianId"
          :options="rosterOptions"
          optionLabel="label"
          optionValue="value"
          filter
          placeholder="Pick a person"
          data-testid="entry-tech-select"
        />
        <InputText v-else id="entry-tech" :value="techName" readonly data-testid="entry-tech-readonly" />
      </div>
      <!-- showOnFocus=false is deliberate, and was found in the browser: with
           the default (panel opens on focus), a showTime picker's panel stays
           open after you pick a date so you can set the time — and inside a
           dialog it covers the Notes field AND the Save button beneath it.
           Escape dismisses the whole dialog rather than just the panel, so the
           correction you just typed is gone. Opening the panel only from the
           calendar icon keeps both paths: type the time, or click to pick. -->
      <div class="form-field">
        <label for="entry-in">Clocked in</label>
        <DatePicker
          id="entry-in"
          v-model="editing.clockIn"
          showTime
          showIcon
          :showOnFocus="false"
          hourFormat="12"
          dateFormat="yy-mm-dd"
          data-testid="entry-clock-in"
        />
      </div>
      <div class="form-field">
        <label for="entry-out">Clocked out</label>
        <DatePicker
          id="entry-out"
          v-model="editing.clockOut"
          showTime
          showIcon
          :showOnFocus="false"
          hourFormat="12"
          dateFormat="yy-mm-dd"
          data-testid="entry-clock-out"
        />
        <small v-if="editing.wasOpen" class="field-hint">
          <template v-if="selfMode">
            This shift was never clocked out. Set the time you actually finished.
          </template>
          <template v-else>
            This shift was never clocked out. Setting a real end time here is what clears it from Dispatch.
          </template>
        </small>
      </div>
      <div class="form-field">
        <label for="entry-notes">Notes<span v-if="selfMode" class="required-mark"> *</span></label>
        <Textarea id="entry-notes" v-model="editing.notes" rows="2" data-testid="entry-notes" />
        <small class="field-hint">
          <template v-if="selfMode">
            Required — say what happened (e.g. "forgot to clock out, left at 5").
            It's written to the record and the audit log with your name on it.
          </template>
          <template v-else>
            Every correction is written to the audit log with your name on it. Say why.
          </template>
        </small>
      </div>
      <!-- ELAPSED, not worked. This dialog edits clock times only; it cannot
           edit breaks. Labelling this "total" made the dialog contradict the
           row it was opened from — a shift with an hour's lunch read 7.00 in
           the table and "8.00h" here. Say which number this is, and show the
           worked figure alongside it whenever a break applies. -->
      <div v-if="editing.hours != null" class="form-preview" data-testid="entry-hours-preview">
        Elapsed: <strong>{{ editing.hours.toFixed(2) }}h</strong>
        <span v-if="editing.breakMinutes" class="preview-sub" data-testid="entry-worked-preview">
          − {{ editing.breakMinutes }}m break = <strong>{{ editingWorkedHours.toFixed(2) }}h</strong> worked
        </span>
      </div>
      <Message v-if="editError" severity="error" :closable="false" data-testid="entry-error">
        {{ editError }}
      </Message>
    </div>
    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="$emit('update:visible', false)" />
      <Button
        label="Save"
        icon="pi pi-check"
        :loading="saving"
        :disabled="!canSave"
        data-testid="entry-save"
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
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Select from 'primevue/select';
import Textarea from 'primevue/textarea';
import { useApi } from '../composables/useApi';
import { shopWallClockToIso, toShopWallClock, useTenantTimezone } from '../composables/useTenantTimezone';

const props = defineProps({
  visible: { type: Boolean, default: false },
  /** The row to correct, or null to create a new entry. */
  entry: { type: Object, default: null },
  /** Tech self-service: entry is always the caller's own; note required. */
  selfMode: { type: Boolean, default: false },
  /** Display name for the row being edited (office mode). */
  techName: { type: String, default: '' },
  /** Office create picker options: [{label, value}]. */
  rosterOptions: { type: Array, default: () => [] },
  /** Office create: preselected technician (e.g. the page's active filter). */
  defaultTechnicianId: { type: String, default: '' },
  /** Create: the day the default 8am–4pm shift lands on. */
  anchorDate: { type: Date, default: null },
});

const emit = defineEmits(['update:visible', 'saved']);

const api = useApi();
const { tenantTimezone, ensureLoaded: ensureTimezone } = useTenantTimezone();

const saving = ref(false);
const editError = ref('');
const isNew = computed(() => !props.entry);

const editing = reactive({
  id: '', technicianId: '',
  clockIn: null, clockOut: null, notes: '', wasOpen: false,
  hours: null, breakMinutes: 0,
});

// Snapshot ON OPEN, not reactively while open: the parent may reload its rows
// underneath a half-typed correction, and yanking the fields out from under
// the user would lose their input.
//
// The zone is awaited HERE, not trusted to the parent (audit 2026-08-13): the
// snapshot converts instants to shop wall clocks ONCE, so if it ran before the
// tenant timezone resolved, the picker would fill browser-local and save()
// would re-encode that as shop time — silently shifting the shift by the
// offset. TimesheetsView happens to await ensureLoaded() in onMounted, but a
// future parent (the tech pages) must not be able to reintroduce that race.
watch(
  () => props.visible,
  async (open) => {
    if (!open) return;
    editError.value = '';
    await ensureTimezone();
    if (!props.visible) return; // closed while the zone was loading
    if (props.entry) {
      Object.assign(editing, {
        id: String(props.entry.id),
        technicianId: String(props.entry.technician_id),
        // Shop wall clock, so the picker shows the same time the table does.
        clockIn: toShopWallClock(props.entry.clock_in_at, tenantTimezone.value),
        clockOut: toShopWallClock(props.entry.clock_out_at, tenantTimezone.value),
        notes: props.entry.notes || '',
        wasOpen: !props.entry.clock_out_at,
        breakMinutes: props.entry.break_minutes || 0,
      });
    } else {
      const anchor = props.anchorDate ? new Date(props.anchorDate) : new Date();
      anchor.setHours(8, 0, 0, 0);
      const out = new Date(anchor);
      out.setHours(16, 0, 0, 0);
      Object.assign(editing, {
        id: '', technicianId: props.defaultTechnicianId || '',
        clockIn: anchor, clockOut: out, notes: '', wasOpen: false,
        breakMinutes: 0,
      });
    }
  },
);

// Worked = elapsed less this entry's break time. The dialog cannot edit breaks,
// so this is shown for reconciliation against the row, not as an input.
const editingWorkedHours = computed(() =>
  Math.max(0, (editing.hours || 0) - (editing.breakMinutes || 0) / 60),
);

// Live preview of what the correction will record, so nobody saves a 14-hour
// typo and finds out on payday.
watch(
  () => [editing.clockIn, editing.clockOut],
  () => {
    if (editing.clockIn && editing.clockOut) {
      const mins = (editing.clockOut.getTime() - editing.clockIn.getTime()) / 60000;
      editing.hours = mins > 0 ? mins / 60 : null;
    } else {
      editing.hours = null;
    }
  },
  { immediate: true },
);

const canSave = computed(() => {
  if (saving.value) return false;
  // Self-service: the note is the tech's attestation and the server rejects a
  // blank one — disabling Save here just says so before the round trip.
  if (props.selfMode && !editing.notes.trim()) return false;
  if (isNew.value) {
    if (!props.selfMode && !editing.technicianId) return false;
    return !!editing.clockIn && !!editing.clockOut && editing.clockOut > editing.clockIn;
  }
  if (!editing.clockIn) return false;
  if (editing.clockOut && editing.clockOut <= editing.clockIn) return false;
  // Never let a CLOSED shift be saved back to open. PATCH accepts an explicit
  // null and nulls both clock_out_at and minutes, and the audit row records
  // only the new values — so the original clock-out would be unrecoverable
  // from inside the app, and the shift would reappear on Dispatch's card.
  // Reopening a shift is not a correction anyone needs from this dialog.
  if (!editing.wasOpen && !editing.clockOut) return false;
  return true;
});

async function save() {
  saving.value = true;
  editError.value = '';
  try {
    // The picker's fields are a SHOP wall clock; the wire is UTC. Sending
    // .toISOString() directly would book the office laptop's zone instead —
    // "5:00 pm" typed in Denver would land as 4:00 pm shop time.
    if (isNew.value) {
      const body = {
        clock_in_at: shopWallClockToIso(editing.clockIn, tenantTimezone.value),
        clock_out_at: shopWallClockToIso(editing.clockOut, tenantTimezone.value),
        notes: editing.notes || null,
      };
      // selfMode omits technician_id: the backend resolves "me", and never
      // trusting the client with an id here is the point.
      if (!props.selfMode) body.technician_id = editing.technicianId;
      await api.post('/api/timeclock/entries', body, { successMessage: 'Time entry added' });
    } else {
      await api.patch(
        `/api/timeclock/entries/${editing.id}`,
        {
          clock_in_at: shopWallClockToIso(editing.clockIn, tenantTimezone.value),
          clock_out_at: editing.clockOut
            ? shopWallClockToIso(editing.clockOut, tenantTimezone.value)
            : null,
          notes: editing.notes || null,
        },
        { successMessage: 'Shift corrected' },
      );
    }
    emit('update:visible', false);
    emit('saved');
  } catch (e) {
    editError.value = e?.message || 'Save failed';
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
.form-preview {
  padding: 0.5rem 0.75rem;
  border-radius: var(--p-border-radius, 6px);
  background: var(--p-content-hover-background);
}
</style>
