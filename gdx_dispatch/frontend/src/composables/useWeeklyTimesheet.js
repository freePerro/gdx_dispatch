/**
 * useWeeklyTimesheet — the tech's own week of clock entries, bucketed and
 * totalled the way the office's /timesheets page does it, so the two surfaces
 * can never disagree about the same shift.
 *
 * The rules it encodes (all inherited from TimesheetsView, 2026-08-10, and
 * docs/design/tech-weekly-timesheet-plan.md):
 *
 *  - Weeks are MONDAY-based and computed from the SHOP's today (tenant
 *    timezone), never the browser's. The old TimeclockView week card was
 *    Sunday-based and browser-local — one of the defects this replaces.
 *  - Which DAY a shift belongs to is a shop-time question. The server buckets
 *    by UTC day, so the fetch asks for a day of slack each side and the real
 *    boundary is applied here (2 of 39 real prod rows are late-evening
 *    Central shifts whose UTC calendar day is the next day).
 *  - Worked = gross elapsed LESS break minutes. The fetch opts into
 *    include_breaks=true; totalling `minutes` alone pays out every lunch
 *    (the old card's break column filtered an entry_type that never exists,
 *    so it was ALWAYS 0.00).
 *  - Future days are not rendered — a Thursday showing "0.00" on Wednesday
 *    reads as a day that already happened.
 */
import { computed, ref } from 'vue';
import { useApi } from './useApi';
import { localDateString } from './useFormatters';
import { dateKeyInZone, useTenantTimezone } from './useTenantTimezone';

// Mirrors SELF_SERVICE_EDIT_WINDOW_DAYS in routers/timeclock.py. Display-only
// affordance gating — the server is the authority and 422s past it either way.
export const SELF_EDIT_WINDOW_DAYS = 14;

const DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

function addDays(d, n) {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

/** Local-field 'YYYY-MM-DD' of a plain calendar Date. */
function keyOf(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function useWeeklyTimesheet() {
  const api = useApi();
  const { tenantTimezone, ensureLoaded } = useTenantTimezone();

  const weekStart = ref(null);   // Monday, as a plain shop-calendar Date
  const weekLoading = ref(false);
  const rawEntries = ref([]);    // the fetch, incl. the ±1 day of slack

  /** Today as the SHOP sees it, as a plain calendar Date. */
  function shopToday() {
    const key = dateKeyInZone(new Date(), tenantTimezone.value);
    if (!key) return new Date();
    const [y, m, d] = key.split('-').map(Number);
    return new Date(y, m - 1, d);
  }

  /** Monday of the week containing `d` — the shop's payroll week. */
  function startOfWeek(d) {
    const out = new Date(d);
    out.setHours(0, 0, 0, 0);
    const dow = (out.getDay() + 6) % 7;
    out.setDate(out.getDate() - dow);
    return out;
  }

  const weekEnd = computed(() => (weekStart.value ? addDays(weekStart.value, 6) : null));

  const currentWeekStart = () => startOfWeek(shopToday());

  const isCurrentWeek = computed(() =>
    weekStart.value ? keyOf(weekStart.value) === keyOf(currentWeekStart()) : true,
  );
  // No browsing into weeks that haven't happened.
  const canGoNext = computed(() => !isCurrentWeek.value);

  const weekLabel = computed(() => {
    if (!weekStart.value) return '';
    const fmt = (d, withYear) => d.toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}),
    });
    const sameYear = weekStart.value.getFullYear() === weekEnd.value.getFullYear();
    return `${fmt(weekStart.value, !sameYear)} – ${fmt(weekEnd.value, true)}`;
  });

  /** 'YYYY-MM-DD' shop-time day a stored stamp belongs to. */
  function shopDayKey(iso) {
    return iso ? dateKeyInZone(String(iso).replace(' ', 'T'), tenantTimezone.value) : '';
  }

  /** Worked minutes for one entry: gross elapsed less its break time. */
  function workedMinutes(entry) {
    if (entry.minutes == null) return null;
    return Math.max(0, entry.minutes - (entry.break_minutes || 0));
  }

  /** Entries whose SHOP day falls inside the viewed week. */
  const weekEntries = computed(() => {
    if (!weekStart.value) return [];
    const from = keyOf(weekStart.value);
    const to = keyOf(weekEnd.value);
    return rawEntries.value.filter((e) => {
      const day = shopDayKey(e.clock_in_at);
      // Unparseable stamp: keep it — it gets its own bucket in `days` below.
      if (!day) return true;
      return day >= from && day <= to;
    });
  });

  /** Mon→Sun day buckets, future days skipped. */
  const days = computed(() => {
    if (!weekStart.value) return [];
    const todayKey = keyOf(shopToday());
    const out = [];
    for (let i = 0; i < 7; i += 1) {
      const date = addDays(weekStart.value, i);
      const key = keyOf(date);
      if (key > todayKey) continue;
      const entries = weekEntries.value
        .filter((e) => shopDayKey(e.clock_in_at) === key)
        .sort((a, b) => String(a.clock_in_at).localeCompare(String(b.clock_in_at)));
      out.push({
        key,
        dayName: DAY_NAMES[i],
        dateLabel: date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
        isToday: key === todayKey,
        entries,
        workedMinutes: entries.reduce((s, e) => s + (workedMinutes(e) || 0), 0),
        breakMinutes: entries.reduce((s, e) => s + (e.break_minutes || 0), 0),
      });
    }
    // A row whose stamp can't be parsed can't be placed on a day — but it is
    // still (possibly) real pay, so it gets a labeled bucket instead of
    // silently vanishing from both the list and the totals. (Audit 2026-08-13:
    // an earlier version claimed to keep these visible while the day filter
    // quietly dropped them.) canSelfEdit() is false for such rows, so the fix
    // path is the office, which is the right place for a corrupt stamp.
    const stray = weekEntries.value.filter((e) => !shopDayKey(e.clock_in_at));
    if (stray.length) {
      out.push({
        key: 'unknown-date',
        dayName: 'Unknown date',
        dateLabel: 'ask the office to fix these',
        isToday: false,
        entries: stray,
        workedMinutes: stray.reduce((s, e) => s + (workedMinutes(e) || 0), 0),
        breakMinutes: stray.reduce((s, e) => s + (e.break_minutes || 0), 0),
      });
    }
    return out;
  });

  const weekWorkedHours = computed(
    () => days.value.reduce((s, d) => s + d.workedMinutes, 0) / 60,
  );
  const weekBreakHours = computed(
    () => days.value.reduce((s, d) => s + d.breakMinutes, 0) / 60,
  );

  /**
   * Whether the self-service dialog should even be offered for this row.
   * Instant comparison — no timezone conversion needed for "is this recent".
   */
  function canSelfEdit(entry) {
    const t = new Date(String(entry.clock_in_at || '').replace(' ', 'T')).getTime();
    if (!Number.isFinite(t)) return false;
    return t >= Date.now() - SELF_EDIT_WINDOW_DAYS * 86_400_000;
  }

  async function reload() {
    if (!weekStart.value) return;
    weekLoading.value = true;
    try {
      // One day of slack each side: the server buckets by UTC day, we mean
      // shop days; weekEntries applies the real boundary. include_breaks so
      // the totals net lunches exactly like the office page.
      const params = new URLSearchParams({ include_breaks: 'true' });
      params.set('date_start', localDateString(addDays(weekStart.value, -1)));
      params.set('date_end', localDateString(addDays(weekEnd.value, 1)));
      const data = await api.get(`/api/timeclock/entries?${params.toString()}`);
      const payload = data?.data || data;
      rawEntries.value = Array.isArray(payload) ? payload : payload?.items || [];
    } catch {
      rawEntries.value = [];
    } finally {
      weekLoading.value = false;
    }
  }

  /** Await the shop zone BEFORE computing the week — near midnight, a
   * browser-local "today" lands the whole card on the wrong week. */
  async function init() {
    await ensureLoaded();
    weekStart.value = currentWeekStart();
    await reload();
  }

  async function prevWeek() {
    weekStart.value = addDays(weekStart.value, -7);
    await reload();
  }

  async function nextWeek() {
    if (!canGoNext.value) return;
    weekStart.value = addDays(weekStart.value, 7);
    await reload();
  }

  async function thisWeek() {
    weekStart.value = currentWeekStart();
    await reload();
  }

  /** Shop-zone clock rendering, so this card and /timesheets read alike. */
  function formatClock(iso) {
    if (!iso) return '—';
    try {
      const opts = { hour: 'numeric', minute: '2-digit' };
      if (tenantTimezone.value) opts.timeZone = tenantTimezone.value;
      return new Date(String(iso).replace(' ', 'T')).toLocaleTimeString('en-US', opts);
    } catch {
      return String(iso);
    }
  }

  return {
    tenantTimezone,
    weekStart, weekLoading, weekEntries, days,
    weekWorkedHours, weekBreakHours, weekLabel,
    isCurrentWeek, canGoNext,
    init, reload, prevWeek, nextWeek, thisWeek,
    canSelfEdit, workedMinutes, formatClock, shopToday,
  };
}
