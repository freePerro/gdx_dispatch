<!--
  Timesheets — the office's view of the crew's clock, and the only place a
  shift can be corrected.

  Why this exists (2026-08-10): the backend has had
  PATCH/POST /api/timeclock/entries since the module shipped — permission-gated
  (own entry, or dispatch/admin) and audit-logged — with ZERO frontend callers.
  Dispatch's "Timeclock needs a look" card had a Fix button that pushed to
  `/timeclock?entry=<id>`, but /timeclock is the SELF-SERVICE page: it never
  read `query.entry`, and its only fetch defaults to the caller's own tech id
  (routers/timeclock.py `_resolve_tech_id`). So the office clicked Fix on a
  tech's broken shift and landed on their own read-only timecard. This page is
  where that button now goes.

  Two traps this page is built around:

  1. `timeclock_entries.technician_id` holds a USER id despite the column name
     (verified on prod: 29/39 rows match `users`, 0 match `technicians`). Names
     therefore come from /api/timeclock/roster — not /api/technicians (wrong id
     space) and not /api/users (excludes soft-deleted staff, so a departed
     employee's hours lose their name). Ids matching nobody are shown as
     "Unknown" rather than dropped — an orphaned shift is still real payroll.

  2. Every stamp here means SHOP time (Settings → Time Clock → Tenant timezone),
     never the browser's — display, the DatePicker's input, and which DAY a
     shift belongs to. See the Time zones section in the script below for why
     that last one is not cosmetic.
-->
<template>
  <section class="timesheets-view view-card">
    <Toolbar class="timesheets-toolbar">
      <template #start>
        <h1 class="view-heading">Timesheets</h1>
      </template>
      <template #end>
        <DatePicker
          v-model="startDate"
          dateFormat="yy-mm-dd"
          showIcon
          placeholder="Start"
          class="range-picker"
          data-testid="timesheets-start"
          @update:modelValue="onDateEdited"
        />
        <span class="range-dash">→</span>
        <DatePicker
          v-model="endDate"
          dateFormat="yy-mm-dd"
          showIcon
          placeholder="End"
          class="range-picker"
          data-testid="timesheets-end"
          @update:modelValue="onDateEdited"
        />
        <Select
          v-model="techFilter"
          :options="techFilterOptions"
          optionLabel="label"
          optionValue="value"
          class="tech-filter"
          data-testid="timesheets-tech-filter"
        />
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          data-testid="timesheets-refresh"
          @click="load"
        />
        <Button
          label="Add Entry"
          icon="pi pi-plus"
          data-testid="timesheets-add-entry"
          @click="openCreate()"
        />
      </template>
    </Toolbar>

    <div class="range-shortcuts">
      <!-- Pay periods come FIRST and are the only shortcuts that mean anything
           to payroll. They appear only when the shop has actually configured a
           payroll calendar; a button that silently falls back to "this week"
           would be worse than no button, because the range it produced would
           look authoritative and be wrong. -->
      <template v-if="payPeriods?.configured">
        <Button
          label="This pay period"
          size="small"
          text
          data-testid="timesheets-this-pay-period"
          @click="setPayPeriod('current')"
        />
        <Button
          label="Last pay period"
          size="small"
          text
          data-testid="timesheets-last-pay-period"
          @click="setPayPeriod('previous')"
        />
        <span class="range-sep" aria-hidden="true">|</span>
      </template>
      <Button label="This week" size="small" text data-testid="timesheets-this-week" @click="setWeek(0)" />
      <Button label="Last week" size="small" text data-testid="timesheets-last-week" @click="setWeek(-1)" />
      <Button label="This month" size="small" text data-testid="timesheets-this-month" @click="setThisMonth" />
      <!-- Named explicitly: these hours get paid, and a bookkeeper in another
           state must know whose clock they are reading. -->
      <span v-if="tenantTimezone" class="zone-note" data-testid="timesheets-zone">
        Times shown in {{ tenantTimezone }}
      </span>
    </div>

    <p
      v-if="selectedPeriod"
      class="period-note"
      data-testid="timesheets-period-note"
    >
      <i class="pi pi-calendar" aria-hidden="true" />
      <span>
        <strong>{{ selectedPeriod.which === 'previous' ? 'Last pay period' : 'This pay period' }}:</strong>
        {{ selectedPeriod.label }}
        <span class="muted-inline"> — paid {{ selectedPeriod.pay_date }}</span>
      </span>
    </p>

    <Message
      v-if="deepLinkMiss"
      severity="warn"
      :closable="false"
      class="deep-link-miss"
      data-testid="timesheets-deeplink-miss"
    >
      That shift isn't in this date range — widen the dates to find it.
    </Message>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <template v-else>
      <div class="summary-strip" data-testid="timesheets-summary">
        <div class="summary-tile">
          <span class="summary-label">Worked hours</span>
          <strong class="summary-value">{{ crewHours.toFixed(2) }}h</strong>
          <span v-if="crewBreakHours > 0" class="summary-sub">
            after {{ crewBreakHours.toFixed(2) }}h of breaks
          </span>
        </div>
        <div class="summary-tile">
          <span class="summary-label">People</span>
          <strong class="summary-value">{{ timecards.length }}</strong>
        </div>
        <div class="summary-tile">
          <span class="summary-label">Entries</span>
          <strong class="summary-value">{{ shownEntryCount }}</strong>
        </div>
        <div class="summary-tile" :class="{ 'summary-tile-flagged': flaggedCount > 0 }">
          <span class="summary-label">Needs a look</span>
          <strong class="summary-value">{{ flaggedCount }}</strong>
        </div>
      </div>

      <!-- Says plainly what these hours are. The clock's record and payroll's
           record are two different tables (payroll reads `time_entries`; this
           reads `timeclock_entries_router`), so a number here is not a promise
           about a paycheck, and nobody should have to read a commit message to
           learn that. -->
      <p class="scope-note" data-testid="timesheets-scope-note">
        Clock record — what the timeclock captured, and what Dispatch flags.
        Payroll figures are kept separately.
      </p>

      <Card
        v-for="card in timecards"
        :key="card.technicianId"
        class="timecard"
        :data-testid="`timecard-${card.technicianId}`"
      >
        <template #title>
          <div class="timecard-header">
            <Avatar :label="card.initial" shape="circle" class="timecard-avatar" />
            <span class="timecard-name">{{ card.name }}</span>
            <Tag
              v-if="card.flagged"
              :value="`${card.flagged} to fix`"
              severity="warn"
              rounded
              :data-testid="`timecard-flagged-${card.technicianId}`"
            />
            <span class="timecard-total" :data-testid="`timecard-total-${card.technicianId}`">
              {{ card.hours.toFixed(2) }}h
            </span>
          </div>
        </template>
        <template #content>
          <DataTable
            :value="card.entries"
            size="small"
            stripedRows
            responsiveLayout="scroll"
            :rowClass="rowClass"
            :data-testid="`timecard-table-${card.technicianId}`"
          >
            <Column header="Date" style="width: 8rem">
              <template #body="{ data }">{{ formatDay(data.clock_in_at) }}</template>
            </Column>
            <Column header="Type" style="width: 6rem">
              <template #body="{ data }">
                <Tag :value="data.entry_type || 'work'" :severity="timeclockEntrySeverity(data.entry_type)" />
              </template>
            </Column>
            <Column header="In" style="width: 7rem">
              <template #body="{ data }">{{ formatClock(data.clock_in_at) }}</template>
            </Column>
            <Column header="Out" style="width: 7rem">
              <template #body="{ data }">
                <span v-if="data.clock_out_at">{{ formatClock(data.clock_out_at) }}</span>
                <span v-else class="muted">—</span>
              </template>
            </Column>
            <Column header="Break" style="width: 5rem">
              <template #body="{ data }">
                <span v-if="data.break_minutes" class="break-minutes">{{ data.break_minutes }}m</span>
                <span v-else class="muted">—</span>
              </template>
            </Column>
            <Column header="Worked" style="width: 9rem">
              <template #body="{ data }">
                <span v-if="data.minutes != null" :class="{ 'hours-implausible': isImplausible(data) }">
                  {{ (workedMinutes(data) / 60).toFixed(2) }}
                </span>
                <Tag v-else-if="!data.clock_out_at" value="Still clocked in" severity="info" />
                <Tag v-else value="Unknown" severity="danger" v-tooltip="'Auto-closed after a missed clock-out — set the real end time.'" />
              </template>
            </Column>
            <Column header="Notes">
              <template #body="{ data }">
                <span v-if="data.notes" class="entry-notes">{{ data.notes }}</span>
                <span v-else class="muted">—</span>
              </template>
            </Column>
            <Column header="" style="width: 5rem">
              <template #body="{ data }">
                <Button
                  icon="pi pi-pencil"
                  severity="secondary"
                  text
                  v-tooltip="'Correct this shift'"
                  aria-label="Correct this shift"
                  :data-testid="`edit-entry-${data.id}`"
                  @click="openEdit(data)"
                />
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <div v-if="!timecards.length" class="empty-message" data-testid="timesheets-empty">
        <i class="pi pi-clock"></i>
        <p>No clock entries in this range.</p>
      </div>
    </template>

    <!-- Correction dialog — shared with the tech's own weekly timesheet
         (TimeEntryDialog is the app's ONLY writer to PATCH/POST
         /api/timeclock/entries). Office mode: roster picker, note advised. -->
    <TimeEntryDialog
      v-model:visible="showEdit"
      :entry="dialogEntry"
      :tech-name="dialogTechName"
      :roster-options="rosterOptions"
      :default-technician-id="techFilter !== 'all' ? techFilter : ''"
      :anchor-date="startDate"
      @saved="load"
    />
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import Avatar from 'primevue/avatar';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';
import TimeEntryDialog from '../components/TimeEntryDialog.vue';
import { useApi } from '../composables/useApi';
import { formatDate, formatTime, localDateString } from '../composables/useFormatters';
import { dateKeyInZone, useTenantTimezone } from '../composables/useTenantTimezone';
import { timeclockEntrySeverity } from '../utils/statusSeverity';

// Mirrors MAX_SHIFT_HOURS in routers/timeclock.py. Display-only — the server
// is the authority on what counts as an exception; this just paints the row.
const IMPLAUSIBLE_SHIFT_MINUTES = 16 * 60;

const api = useApi();
const route = useRoute();
const router = useRouter();

// ── Time zones ──────────────────────────────────────────────────────────────
// Every stamp on this page means SHOP time (Settings → Time Clock → Tenant
// timezone), never the browser's. That distinction is not cosmetic here:
//
//  - Display: a bookkeeper working from another state must read the same
//    "7:56 AM" the tech clocked. TimeclockView already does this (its Intl
//    calls take `timeZone`); this page has to match or the two pages disagree
//    about the same row.
//  - Input: the DatePicker hands back a browser-local Date. Typing "5:00 pm"
//    has to book 5pm in the shop, not 5pm wherever the office laptop is.
//  - Bucketing: which DAY a shift belongs to is a shop-time question. Verified
//    against prod-copy data 2026-08-10 — 2 of 39 real rows are late-evening
//    Central shifts whose UTC calendar day is the NEXT day (10:02 PM CDT on
//    May 2 is May 3 UTC), so a UTC-day bucket files them under the wrong day.
const { tenantTimezone, ensureLoaded: ensureTimezone } = useTenantTimezone();

// The wall-clock conversions (toShopWallClock / shopWallClockToIso) moved to
// useTenantTimezone 2026-08-13 — the tech's own timesheet books hours through
// the same TimeEntryDialog, and two copies of "which wall clock is this" is
// how two pages end up recording different instants for the same shift.

/** 'YYYY-MM-DD' shop-time day a stamp belongs to. */
function shopDayKey(iso) {
  return iso ? dateKeyInZone(String(iso).replace(' ', 'T'), tenantTimezone.value) : '';
}

const loading = ref(false);
const entries = ref([]);
const roster = ref([]);
const startDate = ref(null);
const endDate = ref(null);
const techFilter = ref('all');
const showEdit = ref(false);
// null = the dialog creates a new entry; a row = it corrects that row.
const dialogEntry = ref(null);
const deepLinkMiss = ref(false);
// Consumed once on the first load after a Dispatch "Fix" click, then cleared so
// a later refresh doesn't reopen the dialog under the user.
const pendingEntryId = ref('');

const dialogTechName = computed(() =>
  dialogEntry.value ? techName(dialogEntry.value.technician_id) : '',
);

// --- roster -----------------------------------------------------------------
// technician_id on a timeclock row is a USER id (see the file header). The name
// map comes from /api/timeclock/roster, NOT /api/users: /api/users excludes
// soft-deleted people and caps at 100, so a departed employee's hours rendered
// as "Unknown (<user-id>…)" on a page whose whole job is reviewing hours —
// including, especially, a former employee's. Caught in the browser 2026-08-10.
const rosterById = computed(() => {
  const map = new Map();
  for (const u of roster.value) {
    if (u.name) map.set(String(u.technician_id), u.name);
  }
  return map;
});

function techName(id) {
  const known = rosterById.value.get(String(id));
  if (known) return known;
  // Never drop an unmatched id — an orphaned shift is still real payroll.
  return `Unknown (${String(id).slice(0, 8)})`;
}

// Add-entry picker: active staff only. Someone who has left should not be
// gaining new shifts, but their existing rows stay readable and correctable.
const rosterOptions = computed(() =>
  roster.value
    .filter((u) => u.active !== false)
    .map((u) => ({
      label: u.name || `Unknown (${String(u.technician_id).slice(0, 8)})`,
      value: String(u.technician_id),
    }))
    .sort((a, b) => a.label.localeCompare(b.label)),
);

// From allTimecards, never the filtered list — see the note there.
const techFilterOptions = computed(() => [
  { label: 'Everyone', value: 'all' },
  ...allTimecards.value.map((c) => ({ label: c.name, value: c.technicianId })),
]);

// --- grouping ---------------------------------------------------------------
function isImplausible(entry) {
  return entry.minutes != null && entry.minutes > IMPLAUSIBLE_SHIFT_MINUTES;
}

/** Worked minutes: gross elapsed LESS break time (see break_minutes on the API).
 *
 * SAME RULE as `Shift.worked_minutes` in gdx_dispatch/core/timesheet_hours.py,
 * which is what the CSV, the PDF and the emailed timesheet are built from.
 * Change one and you change what somebody is paid on one surface but not the
 * other; change both or neither. */
function workedMinutes(entry) {
  if (entry.minutes == null) return null;
  return Math.max(0, entry.minutes - (entry.break_minutes || 0));
}

/**
 * Matches the three kinds routers/timeclock.py `/exceptions` reports, so a row
 * flagged here is a row the Dispatch card is nagging about — and ONLY those.
 *
 * The open-shift arm carries the threshold on purpose. Flagging every null
 * clock_out would light up every tech currently on the clock: at 10am with
 * four techs working, this page would read "Needs a look: 4" while Dispatch
 * reads 0. That is the wallpaper failure the exceptions endpoint is explicitly
 * built to avoid, and it would train the office to ignore the count.
 */
// Python twin: `flag_for()` in core/timesheet_hours.py. The scheduled send
// refuses to mail a period that has any of these, so a rule that disagrees
// with the screen means the office sees "0 to fix" while the send holds — or,
// far worse, the reverse.
function isFlagged(entry) {
  if (!entry.clock_out_at) {
    const openHours = (Date.now() - new Date(String(entry.clock_in_at).replace(' ', 'T')).getTime()) / 3_600_000;
    return Number.isFinite(openHours) && openHours > IMPLAUSIBLE_SHIFT_MINUTES / 60;
  }
  if (entry.minutes == null) return true;        // unknown_duration_shift
  return isImplausible(entry);                   // implausible_shift
}

/**
 * Entries whose SHOP-time day falls in the selected range.
 *
 * The server filters on `func.date(clock_in_at)`, which buckets by the UTC
 * calendar day — so a 10 PM Central shift is filed under tomorrow and would
 * drop out of the week it was actually worked (2 of 39 real rows, checked
 * 2026-08-10). load() therefore asks for a day of slack on each end and the
 * exact boundary is applied here, in shop time, where it belongs.
 */
const entriesInRange = computed(() => {
  const from = startDate.value ? localDateString(startDate.value) : null;
  const to = endDate.value ? localDateString(endDate.value) : null;
  if (!from && !to) return entries.value;
  return entries.value.filter((e) => {
    const day = shopDayKey(e.clock_in_at);
    if (!day) return true;  // unparseable stamp: show it, don't silently drop pay
    if (from && day < from) return false;
    if (to && day > to) return false;
    return true;
  });
});

// EVERY person in range, before the tech filter. The filter's own option list
// has to come from here: deriving it from the filtered result made the control
// a one-way door — pick one person and every other name vanished from the
// dropdown, so you could never switch from one tech to another.
const allTimecards = computed(() => {
  const groups = new Map();
  for (const e of entriesInRange.value) {
    const id = String(e.technician_id);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(e);
  }
  const cards = [];
  for (const [technicianId, rows] of groups) {
    rows.sort((a, b) => String(a.clock_in_at).localeCompare(String(b.clock_in_at)));
    const name = techName(technicianId);
    cards.push({
      technicianId,
      name,
      initial: (name || '?').charAt(0).toUpperCase(),
      entries: rows,
      // WORKED hours — gross elapsed less break time. `minutes` alone is what
      // the clock recorded end-to-end, so totalling it pays out every lunch.
      hours: rows.reduce((sum, r) => sum + (workedMinutes(r) || 0), 0) / 60,
      breakHours: rows.reduce((sum, r) => sum + (r.break_minutes || 0), 0) / 60,
      flagged: rows.filter(isFlagged).length,
    });
  }
  cards.sort((a, b) => a.name.localeCompare(b.name));
  return cards;
});

const timecards = computed(() =>
  techFilter.value === 'all'
    ? allTimecards.value
    : allTimecards.value.filter((c) => c.technicianId === techFilter.value),
);

const crewHours = computed(() => timecards.value.reduce((s, c) => s + c.hours, 0));
const crewBreakHours = computed(() => timecards.value.reduce((s, c) => s + c.breakHours, 0));
const flaggedCount = computed(() => timecards.value.reduce((s, c) => s + c.flagged, 0));
// Rows actually ON SCREEN. `entries` is the raw fetch and includes the ±1 day
// of slack the shop-day boundary needs, so counting it reported rows the page
// deliberately does not show.
const shownEntryCount = computed(() =>
  timecards.value.reduce((s, c) => s + c.entries.length, 0),
);

function rowClass(data) {
  return isFlagged(data) ? 'row-flagged' : '';
}

// --- formatting -------------------------------------------------------------
// Both render in SHOP time. Passing `timeZone: undefined` is the documented
// Intl no-op, so these degrade to browser-local before the tenant zone loads
// and correct themselves when it does (tenantTimezone is reactive).
function formatDay(iso) {
  if (!iso) return '—';
  return formatDate(String(iso).replace(' ', 'T'), {
    options: {
      year: 'numeric', month: 'short', day: 'numeric',
      timeZone: tenantTimezone.value || undefined,
    },
  });
}
function formatClock(iso) {
  if (!iso) return '—';
  return formatTime(String(iso).replace(' ', 'T'), {
    options: {
      hour: 'numeric', minute: '2-digit',
      timeZone: tenantTimezone.value || undefined,
    },
  });
}

// --- date range -------------------------------------------------------------
/**
 * Today's date as the SHOP sees it, returned as a browser-local Date carrying
 * those calendar fields (which is what DatePicker binds to).
 *
 * The rest of this file is built on "shop time, never the browser's", but the
 * default range and the week/month shortcuts were computing from a raw
 * `new Date()` — so near midnight a bookkeeper in another zone would land on
 * the wrong week on load, the one path every user hits.
 */
function shopToday() {
  const key = dateKeyInZone(new Date(), tenantTimezone.value);
  if (!key) return new Date();
  const [y, m, d] = key.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function addDays(d, n) {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

function startOfWeek(d) {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  // Monday-based: the shop's week, and what a payroll period looks like.
  const dow = (out.getDay() + 6) % 7;
  out.setDate(out.getDate() - dow);
  return out;
}

// ── Pay periods ─────────────────────────────────────────────────────────────
// The ranges come from GET /api/timeclock/pay-periods, never from arithmetic
// here. The same endpoint feeds the Settings preview and the scheduled send,
// so this page cannot show one fortnight while the emailed file covers
// another — the failure that makes a payroll export worth less than nothing.
const payPeriods = ref(null);
// Which named period is on screen, cleared the moment the operator picks any
// other range. Leaving the banner up while the dates say something else is
// how someone exports "last pay period" and gets three weeks.
const selectedPeriod = ref(null);

async function loadPayPeriods() {
  try {
    payPeriods.value = await api.get('/api/timeclock/pay-periods', { suppressErrorToast: true });
  } catch {
    // No payroll calendar configured, or the module is off. The week/month
    // shortcuts still work; only the pay-period buttons are absent.
    payPeriods.value = null;
  }
}

/** Parse 'YYYY-MM-DD' into a browser-local Date carrying those calendar
 *  fields — the same shape shopToday() returns and DatePicker binds to.
 *  `new Date('2026-08-10')` would parse as UTC midnight and render as the
 *  9th for anyone west of Greenwich. */
function dateFromKey(key) {
  const [y, m, d] = String(key).split('-').map(Number);
  return new Date(y, m - 1, d);
}

function onDateEdited() {
  selectedPeriod.value = null;
  load();
}

/** Move the pickers onto a named period. Returns false when the shop has no
 *  payroll calendar, so callers can fall back rather than land on nothing. */
function applyPayPeriod(which) {
  const period = payPeriods.value?.[which];
  if (!period) return false;
  startDate.value = dateFromKey(period.start);
  endDate.value = dateFromKey(period.end);
  selectedPeriod.value = { which, label: period.label, pay_date: period.pay_date };
  return true;
}

function setPayPeriod(which) {
  if (applyPayPeriod(which)) load();
}

function setWeek(offset) {
  selectedPeriod.value = null;
  const anchor = shopToday();
  anchor.setDate(anchor.getDate() + offset * 7);
  const start = startOfWeek(anchor);
  const end = new Date(start);
  end.setDate(end.getDate() + 6);
  startDate.value = start;
  endDate.value = end;
  load();
}

function setThisMonth() {
  selectedPeriod.value = null;
  const now = shopToday();
  startDate.value = new Date(now.getFullYear(), now.getMonth(), 1);
  endDate.value = new Date(now.getFullYear(), now.getMonth() + 1, 0);
  load();
}

// --- loading ----------------------------------------------------------------
async function loadRoster() {
  try {
    const data = await api.get('/api/timeclock/roster', { suppressErrorToast: true });
    roster.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    // Names degrade to "Unknown (id…)"; the hours are still correct and
    // correctable, so this must not empty the page.
    roster.value = [];
  }
}

async function load() {
  loading.value = true;
  // Cleared on every load so the "not in this range" notice reflects THIS
  // range, not the one the user has already widened away from.
  deepLinkMiss.value = false;
  try {
    // One day of slack on each end: the server buckets by UTC day, we mean shop
    // days. entriesInRange applies the real boundary. Without the slack, an
    // evening shift on the last day of the range never comes back at all.
    const params = new URLSearchParams({ all_technicians: 'true' });
    if (startDate.value) params.set('date_start', localDateString(addDays(startDate.value, -1)));
    if (endDate.value) params.set('date_end', localDateString(addDays(endDate.value, 1)));
    const data = await api.get(`/api/timeclock/entries?${params.toString()}`);
    entries.value = Array.isArray(data) ? data : data?.items || [];
  } catch (e) {
    entries.value = [];
    // The page shows its empty state; the API helper already toasts the error.
  } finally {
    loading.value = false;
  }
  consumeDeepLink();
}

// --- Dispatch "Fix" deep link -----------------------------------------------
// DispatchView pushes { entry, tech, on } from the "Timeclock needs a look"
// card. Open that row's correction dialog directly — the click already said
// which shift is wrong, so making the user find it again is the dead end this
// page replaces.
function consumeDeepLink() {
  const wanted = pendingEntryId.value;
  if (!wanted) return;
  const found = entries.value.find((e) => String(e.id) === String(wanted));
  if (found) {
    pendingEntryId.value = '';
    openEdit(found);
  } else {
    // Deliberately NOT cleared on a miss: the user clicked Fix on a specific
    // shift, and the notice tells them to widen the dates. Keeping the id
    // pending means doing so actually opens it, instead of making them hunt
    // for the row the click already identified.
    deepLinkMiss.value = true;
  }
  // Drop the query either way, so a plain refresh doesn't reopen the dialog.
  router.replace({ path: '/timesheets' }).catch(() => {});
}

// --- edit / create ----------------------------------------------------------
// The dialog itself (snapshotting, canSave, the PATCH/POST) lives in
// TimeEntryDialog — shared with the tech's own timesheet. This page only
// decides WHICH row it opens on.
function openEdit(entry) {
  dialogEntry.value = entry;
  showEdit.value = true;
}

function openCreate() {
  dialogEntry.value = null;
  showEdit.value = true;
}

onMounted(async () => {
  const q = route.query || {};
  pendingEntryId.value = q.entry ? String(q.entry) : '';
  if (q.tech) techFilter.value = String(q.tech);

  // Before the default range is chosen: the answer decides what it is.
  await loadPayPeriods();

  if (q.on) {
    // Anchor on the flagged shift's week. Padded a day either side: `on` is the
    // UTC date of the clock-in, and the week is computed locally — without the
    // pad, a late-evening shift can fall outside its own week.
    const anchor = new Date(`${String(q.on).slice(0, 10)}T12:00:00`);
    const start = startOfWeek(anchor);
    start.setDate(start.getDate() - 1);
    const end = startOfWeek(anchor);
    end.setDate(end.getDate() + 7);
    startDate.value = start;
    endDate.value = end;
  } else if (!applyPayPeriod('current')) {
    // No payroll calendar configured — the week, exactly as before. When one
    // IS configured the page opens on the pay period, because that is the
    // range this screen exists to produce a file for; making the operator
    // know to click for it is the "they'd have to know the URL" failure in
    // button form. One click returns to a plain week.
    const start = startOfWeek(shopToday());
    const end = new Date(start);
    end.setDate(end.getDate() + 6);
    startDate.value = start;
    endDate.value = end;
  }

  // The zone MUST resolve before any row loads. The display formatters are
  // reactive and self-heal when it arrives, but openEdit() snapshots converted
  // Dates into `editing` ONCE — so if the deep-link dialog opened first, the
  // picker would fill with browser-local values that save() then re-encodes as
  // shop time, silently shifting a shift by the offset and audit-logging it
  // under the office's name. Awaiting here closes that race.
  await ensureTimezone();
  await loadRoster();
  await load();
});
</script>

<style scoped>
/* Theme tokens only — this banner sits on the page in dark mode too, where a
   hardcoded pale background would put near-white text on near-white. */
.period-note {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin: 0 0 0.75rem;
  padding: 0.55rem 0.8rem;
  border: 1px solid var(--p-content-border-color);
  border-left: 4px solid var(--p-primary-color);
  border-radius: var(--p-content-border-radius, 6px);
  background: var(--p-content-background);
  color: var(--p-text-color);
  font-size: 0.92rem;
}
.period-note .pi {
  color: var(--p-primary-color);
}
.muted-inline {
  color: var(--p-text-muted-color);
}
.range-sep {
  color: var(--p-content-border-color);
  padding: 0 0.15rem;
}
.timesheets-toolbar {
  margin-bottom: 0.5rem;
}
.view-heading {
  font-size: 1.35rem;
  margin: 0;
}
.range-picker {
  width: 10rem;
}
.range-dash {
  color: var(--p-text-muted-color);
  padding: 0 0.15rem;
}
.tech-filter {
  min-width: 11rem;
}
.range-shortcuts {
  display: flex;
  align-items: center;
  gap: 0.25rem;
  margin-bottom: 0.75rem;
}
.zone-note {
  margin-left: auto;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.deep-link-miss {
  margin-bottom: 0.75rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem 0;
}

.summary-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}
.summary-tile {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  padding: 0.75rem 1rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: var(--p-border-radius, 6px);
  background: var(--p-content-background);
}
.summary-tile-flagged {
  border-color: var(--p-amber-500);
}
.summary-label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.summary-value {
  font-size: 1.4rem;
  line-height: 1.1;
}
.summary-sub {
  font-size: 0.72rem;
  color: var(--p-text-muted-color);
}
.scope-note {
  margin: -0.5rem 0 1rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.break-minutes {
  color: var(--p-text-muted-color);
  font-variant-numeric: tabular-nums;
}

.timecard {
  margin-bottom: 1rem;
}
.timecard-header {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}
.timecard-avatar {
  width: 2rem;
  height: 2rem;
  font-size: 0.9rem;
}
.timecard-name {
  font-weight: 600;
}
.timecard-total {
  margin-left: auto;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.entry-notes {
  white-space: pre-wrap;
  word-break: break-word;
}
.muted {
  color: var(--p-text-muted-color);
}
.hours-implausible {
  color: var(--p-red-500);
  font-weight: 600;
}
/* Deep selector: PrimeVue owns the <tr>, so the scoped attribute never lands
   on it. Tint only — the Hours cell carries the actual explanation. */
:deep(.row-flagged) > td {
  background: color-mix(in srgb, var(--p-amber-500) 10%, transparent);
}

.empty-message {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--p-text-muted-color);
}
.empty-message i {
  font-size: 2rem;
  display: block;
  margin-bottom: 0.5rem;
}
</style>
