<script setup>
// The "No" half of the "Is this job finished?" sheet (multi-day jobs, PR 3,
// plan §5.4a "The sheet"). One submit closes the crew's day (R-P4): every
// checked visit of `open_day.date`, and every checked person's hours.
//
// Rows come from GET /api/jobs/{id}/day-log's `open_day`, read when the sheet
// opened, and the submit names exactly those ids — so a replay names the
// original day. Hours are per PERSON, never per visit: a person is one row
// however many visits they were on, so one submit bills each person once.
//
// The parent (MobileJobCloseoutDialog) owns the footer, so this exposes
// `canSubmit`, `saving`, `isDirty` and `submit()` instead of rendering a button.
import { ref, computed, watch } from 'vue'
import Textarea from 'primevue/textarea'
import Button from 'primevue/button'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { formatTime, formatDate } from '../composables/useFormatters'
import {
  validDayHours, formatHours, formatDayLong, refusalOf,
  alreadyClosedText, jobFinishedText, dayCloseTotalHours,
} from '../utils/dayClose'

const props = defineProps({
  jobId: { type: String, default: null },
  // The day-log payload; only `open_day` is read here.
  dayLog: { type: Object, default: null },
  // A technician gets the "you didn't tap I'm here" warning when they have no
  // row of their own; a desk user simply has no "You" row.
  callerIsTech: { type: Boolean, default: false },
})
const emit = defineEmits(['day-closed'])

const api = useApi()
const toast = useToast()

const MAX_ADDED = 10

const day = ref(null)
const visits = ref([])
const people = ref([])
const added = ref([])
const note = ref('')
const saving = ref(false)
// The refusal the sheet keeps on screen — a 409 is an answer the tech has to
// act on ("tell the office"), not a toast that vanishes.
const refusal = ref('')

function _build() {
  const od = props.dayLog?.open_day || null
  day.value = od?.date || null
  visits.value = (od?.visits || []).map((v) => ({
    id: String(v.id),
    name: (v.tech_name || '').trim() || 'Unassigned',
    time: v.start_at ? formatTime(v.start_at) : '',
    checked: true,
  }))
  const rows = (od?.people || []).map((p) => ({
    user_id: String(p.user_id),
    name: p.mine ? 'You' : ((p.name || '').trim() || 'Someone'),
    mine: !!p.mine,
    logged: Array.isArray(p.logged) ? p.logged : [],
    checked: true,
    hours: '',
    edited: false,
  }))
  // "You" first.
  rows.sort((a, b) => Number(b.mine) - Number(a.mine))
  people.value = rows
  added.value = []
  note.value = ''
  refusal.value = ''
}
watch(() => props.dayLog, _build, { immediate: true })

const youRow = computed(() => people.value.find((p) => p.mine) || null)

// Every other row shows the "You" value until edited. A desk sheet (no "You"
// row) starts every row blank, and so does a person who already has hours
// logged for that day: copying a whole day onto someone whose first stint is
// already logged bills that stint twice (round 33).
function shownHours(row) {
  if (row.mine || row.edited || row.logged.length || !youRow.value) return row.hours
  return youRow.value.hours
}
function onHoursInput(row, value) {
  row.hours = value
  row.edited = true
}

function loggedText(row) {
  const total = row.logged.reduce((t, l) => t + (Number(l?.hours) || 0), 0)
  const by = [...new Set(row.logged.map((l) => l?.closed_by).filter(Boolean))].join(', ')
  return `Already logged for ${formatDayLong(day.value)}: ${formatHours(total)} h`
    + (by ? ` (by ${by})` : '')
    + '. Enter only the time since.'
}

function uncheckedText(row) {
  return row.mine
    ? 'Your time stays open until you answer this sheet or the job is finished'
    : `${row.name}'s time stays open until they answer this sheet or the job is finished`
}

function addHelper() {
  if (added.value.length >= MAX_ADDED) return
  added.value.push({ hours: '' })
}
function removeHelper(idx) {
  added.value.splice(idx, 1)
}

const nothingToClose = computed(() => !visits.value.length && !people.value.length)
const anyVisitUnchecked = computed(() => visits.value.some((v) => !v.checked))
const noTimerWarning = computed(() => props.callerIsTech && !youRow.value && !nothingToClose.value)

const canSubmit = computed(() => {
  if (!props.jobId || !day.value || nothingToClose.value || saving.value) return false
  const checkedVisits = visits.value.filter((v) => v.checked)
  const checkedPeople = people.value.filter((p) => p.checked)
  if (!checkedVisits.length && !checkedPeople.length && !added.value.length) return false
  if (!checkedPeople.every((p) => validDayHours(shownHours(p)))) return false
  if (!added.value.every((a) => validDayHours(a.hours))) return false
  return true
})

const isDirty = computed(() =>
  people.value.some((p) => p.hours !== '' || !p.checked)
  || visits.value.some((v) => !v.checked)
  || added.value.length > 0
  || note.value.trim() !== '',
)

function _body() {
  return {
    day: day.value,
    visits: visits.value.filter((v) => v.checked).map((v) => v.id),
    people: people.value
      .filter((p) => p.checked)
      .map((p) => ({ user_id: p.user_id, hours: Number(shownHours(p)) })),
    added: added.value.map((a) => ({ hours: Number(a.hours) })),
    closed_at: new Date().toISOString(),
    note: note.value.trim() || null,
  }
}

async function submit() {
  if (!canSubmit.value) return null
  saving.value = true
  refusal.value = ''
  const body = _body()
  const yourHours = youRow.value?.checked ? Number(shownHours(youRow.value)) : dayCloseTotalHours(body)
  try {
    // conflictIsError: the queue files any 409 as synced unless told
    // otherwise, and a refused "No" filed as synced is a day of hours that
    // vanished (plan §5.4a, round 2).
    const r = await api.postQueued(`/api/jobs/${props.jobId}/day-close`, body, {
      actionType: 'job.day_close',
      resourceId: String(props.jobId),
      conflictIsError: true,
    })
    if (r?.queued) {
      toast.add({
        severity: 'warn',
        summary: 'Saved offline',
        detail: `${formatHours(dayCloseTotalHours(body))} h for ${formatDayLong(body.day)} is stored on this phone and sends when you reconnect.`,
        life: 6000,
      })
    } else {
      const next = r?.next_visit?.start_at
      toast.add({
        severity: 'success',
        summary: `${formatDayLong(body.day)} closed`,
        detail: next
          ? `Next visit: ${formatDate(next, { options: { weekday: 'long', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' } })}.`
          : 'No next visit is booked yet — the office books the next day.',
        life: 6000,
      })
    }
    emit('day-closed', r)
    return r
  } catch (err) {
    const ref_ = refusalOf(err)
    if (err?.status === 409) {
      if (ref_.already_closed) refusal.value = alreadyClosedText(ref_.already_closed, yourHours, body.day)
      else if (ref_.code === 'job_finished') refusal.value = jobFinishedText(dayCloseTotalHours(body), body.day)
      else refusal.value = ref_.detail || 'The server refused this day. Reopen the sheet.'
    } else {
      toast.add({
        severity: 'error',
        summary: 'Could not close the day',
        detail: ref_.detail || err?.message || 'Try again.',
        life: 5000,
      })
    }
    return null
  } finally {
    saving.value = false
  }
}

defineExpose({ canSubmit, saving, isDirty, submit })
</script>

<template>
  <div class="day-close" data-testid="mjco-day-close">
    <p v-if="nothingToClose" class="muted hint" data-testid="mjco-day-nothing">
      No visit is booked on this job today — the office books the next day
    </p>

    <template v-else>
      <!-- Visits: checkboxes only. Hours are per person, below. -->
      <section class="section" data-testid="mjco-day-visits">
        <header class="section-head">
          <h3>Visits done for {{ formatDayLong(day) }}</h3>
        </header>
        <p v-if="!visits.length" class="muted hint">No visit is booked on this day.</p>
        <label
          v-for="v in visits"
          :key="v.id"
          class="check-row"
          :data-testid="`mjco-day-visit-${v.id}`"
        >
          <input v-model="v.checked" type="checkbox" />
          <span>{{ v.name }}<span v-if="v.time" class="muted"> · {{ v.time }}</span></span>
        </label>
        <p v-if="anyVisitUnchecked" class="warn-line" data-testid="mjco-day-visit-warning">
          This job stays on the board until every visit is closed.
        </p>
      </section>

      <!-- Hours: one row per person who tapped in that day. -->
      <section class="section" data-testid="mjco-day-people">
        <header class="section-head"><h3>Hours</h3></header>
        <p v-if="noTimerWarning" class="warn-line" data-testid="mjco-day-no-timer">
          You didn't tap I'm here today, so these hours aren't added to your pay. Tell the office.
        </p>
        <div
          v-for="p in people"
          :key="p.user_id"
          class="person-row"
          :data-testid="`mjco-day-person-${p.user_id}`"
        >
          <label class="check-row person-name">
            <input v-model="p.checked" type="checkbox" :data-testid="`mjco-day-person-check-${p.user_id}`" />
            <span>{{ p.name }}</span>
          </label>
          <input
            :value="shownHours(p)"
            type="number"
            min="0.25"
            max="24"
            step="0.25"
            inputmode="decimal"
            class="hours-input"
            :disabled="!p.checked"
            :aria-label="`Hours for ${p.name}`"
            placeholder="h"
            :data-testid="`mjco-day-hours-${p.user_id}`"
            @input="onHoursInput(p, $event.target.value)"
          />
          <small v-if="p.logged.length" class="muted person-note" :data-testid="`mjco-day-logged-${p.user_id}`">
            {{ loggedText(p) }}
          </small>
          <small v-if="!p.checked" class="warn-line person-note" :data-testid="`mjco-day-person-warning-${p.user_id}`">
            {{ uncheckedText(p) }}
          </small>
          <small
            v-else-if="p.hours !== '' && !validDayHours(shownHours(p))"
            class="warn-line person-note"
          >
            Enter more than 0 and at most 24 hours.
          </small>
        </div>

        <div v-for="(a, idx) in added" :key="`added-${idx}`" class="person-row" :data-testid="`mjco-day-added-${idx}`">
          <span class="person-name muted">Helper (not tapped in)</span>
          <input
            v-model="a.hours"
            type="number"
            min="0.25"
            max="24"
            step="0.25"
            inputmode="decimal"
            class="hours-input"
            aria-label="Helper hours"
            placeholder="h"
            :data-testid="`mjco-day-added-hours-${idx}`"
          />
          <Button
            type="button"
            icon="pi pi-times"
            aria-label="Remove helper"
            text
            severity="danger"
            size="small"
            :data-testid="`mjco-day-added-remove-${idx}`"
            @click="removeHelper(idx)"
          />
        </div>
        <Button
          type="button"
          label="+ Add a helper who didn't tap in"
          size="small"
          severity="secondary"
          text
          :disabled="added.length >= MAX_ADDED"
          data-testid="mjco-day-add-helper"
          @click="addHelper"
        />
        <small class="muted">Only for someone with no row above — anyone who tapped I'm here already has one.</small>
      </section>

      <section class="section" data-testid="mjco-day-note">
        <header class="section-head"><h3>Note for the office <span class="muted">(optional)</span></h3></header>
        <Textarea
          v-model="note"
          rows="2"
          auto-resize
          maxlength="2000"
          class="w-full"
          placeholder="What's left?"
          data-testid="mjco-day-note-input"
        />
        <small class="muted">
          Parts you used go on the job's Parts card as you use them — they are not entered here.
        </small>
      </section>

      <p v-if="refusal" class="refusal" role="alert" data-testid="mjco-day-refusal">{{ refusal }}</p>
    </template>
  </div>
</template>

<style scoped>
.day-close { display: flex; flex-direction: column; gap: 0.85rem; }
.section {
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.65rem;
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  background: var(--p-content-background);
  color: var(--p-text-color);
}
.section-head h3 { margin: 0; font-size: 0.95rem; font-weight: 700; }
.muted { color: var(--p-text-muted-color); font-size: 0.85rem; font-weight: 400; }
.hint { margin: 0; }
.check-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-height: 44px;
  font-size: 0.95rem;
}
.check-row input { width: 1.15rem; height: 1.15rem; flex: 0 0 auto; }
/* name | hours | (remove); notes span the full row under it. min-width: 0
   keeps a long name from pushing the hours box off a 390px phone. */
.person-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 5.5rem auto;
  gap: 0.4rem;
  align-items: center;
}
.person-name { min-width: 0; overflow-wrap: anywhere; }
.person-note { grid-column: 1 / -1; }
.hours-input {
  padding: 0.6rem 0.5rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  text-align: center;
  font: inherit;
  background: var(--p-content-background);
  color: var(--p-text-color);
  min-height: 44px;
  width: 100%;
}
.hours-input:disabled { opacity: 0.55; }
.warn-line {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-color);
  border-left: 3px solid var(--p-amber-400, #fbbf24);
  padding-left: 0.5rem;
}
.refusal {
  margin: 0;
  padding: 0.6rem 0.75rem;
  border-radius: 8px;
  border: 1px solid var(--p-red-400, #f87171);
  background: color-mix(in srgb, var(--p-red-400, #f87171) 12%, transparent);
  color: var(--p-text-color);
}
.w-full { width: 100%; }
</style>
