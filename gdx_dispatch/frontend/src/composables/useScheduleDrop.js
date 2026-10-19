/**
 * First scheduling from a drop (Doug, 2026-10-16).
 *
 * A job with no date dropped onto a tech asks, before anything is written,
 * how many hours it takes and who the whole crew is, prefilled (initialCrew)
 * and open to adding anyone else. A job that already has a date just moves —
 * this is only for the drop that books it. Cancel writes nothing.
 */
import { ref } from 'vue';

// The PATCH body for an answer: the date, the crew with its lead, and the
// hours unless they are the job's own saved hours left as shown.
export function scheduleDropPatch(answer, scheduledAt) {
  const patch = {
    scheduled_at: scheduledAt,
    assigned_tech_ids: answer.tech_ids,
    lead_tech_id: answer.lead,
  };
  if (answer.hours != null) patch.scheduled_duration_hours = answer.hours;
  return patch;
}

// An estimate's man-hours over a crew, rounded up to the quarter hour and
// never under one (GDXA-1262): the figure a prompt prefills.
export function estimateHoursOver(manHours, crewSize) {
  const crew = Math.max(1, Number(crewSize) || 1);
  return Math.max(0.25, Math.ceil((Number(manHours) / crew) * 4) / 4);
}

// The crew a dropped job starts the dialog with, matching what the drop did
// before the dialog: a job with one tech or none goes to the tech it was
// dropped on (a self-assigned job is handed over, not shared); a crew keeps
// its lead and members, and gains the dropped-on tech if they were not on it.
// `existingCrew` is lead first (crewOf).
export function initialCrew(techId, existingCrew = []) {
  const dropped = String(techId);
  const crew = existingCrew.map(String).filter(Boolean);
  if (crew.length < 2) return { techIds: [dropped], leadId: dropped };
  return { techIds: crew.includes(dropped) ? crew : [...crew, dropped], leadId: crew[0] };
}

// State for one ScheduleDropDialog. `ask` resolves { tech_ids, lead, hours }
// (hours is null when blank or left at the job's own saved hours), or null on
// Cancel / close. A question still open when another is asked is answered
// Cancel. `suggest`, given when the job has no hours of its own, resolves
// { manHours, accepted } or null; its answer reaches only the dialog it was
// asked for (`seq`), never one opened since.
export function useScheduleDropPrompt() {
  const prompt = ref({
    visible: false, seq: 0, jobLabel: '', whenLabel: '', techIds: [], leadId: null, droppedId: null, hours: '', estimate: null,
  });
  let pending = null;
  let seq = 0;

  function ask({ jobLabel, whenLabel, techId, crew = [], hours = null, suggest = null }) {
    if (pending) answer(null);
    const { techIds, leadId } = initialCrew(techId, crew);
    const mine = ++seq;
    prompt.value = {
      visible: true,
      seq: mine,
      jobLabel: jobLabel || 'This job',
      whenLabel: whenLabel || '',
      techIds,
      leadId,
      droppedId: String(techId),
      hours: Number(hours) > 0 ? String(hours) : '',
      estimate: null,
    };
    if (suggest) {
      Promise.resolve().then(suggest).then((estimate) => {
        if (!estimate || mine !== seq || !prompt.value.visible) return;
        prompt.value = { ...prompt.value, estimate };
      }, () => {});
    }
    return new Promise((resolve) => { pending = resolve; });
  }

  function answer(result) {
    const resolve = pending;
    pending = null;
    prompt.value = { ...prompt.value, visible: false };
    if (resolve) resolve(result);
  }

  return { prompt, ask, answer };
}

/*
 * A booking longer than the tech's day (Doug, 2026-10-18).
 *
 * A job booked from a drop that runs past the end of the dropped-on tech's
 * shift asks first: add a tech, book the rest on that tech's next workdays,
 * or keep it all on the one day. Hours here are wall clock, as
 * scheduled_duration_hours is; man-hours are conserved when the crew grows.
 */

const quarter = (h) => Math.round(Number(h) * 4) / 4;

// A tech's shift as decimal hours, or null when either end is unknown or
// the end is not after the start (nothing to measure against).
export function shiftHours(start, end) {
  const parse = (s) => {
    const m = /^(\d{1,2}):(\d{2})/.exec(typeof s === 'string' ? s : '');
    return m ? Number(m[1]) + Number(m[2]) / 60 : null;
  };
  const a = parse(start);
  const b = parse(end);
  return a == null || b == null || b <= a ? null : { start: a, end: b };
}

// Hours left in the shift from a start (decimal hours past shop midnight).
// A start before the shift counts from shift start; null (no set time)
// gets the whole shift.
export function hoursLeftInDay(startHour, shift) {
  if (!shift) return null;
  const from = startHour == null ? shift.start : Math.max(startHour, shift.start);
  return Math.max(0, quarter(shift.end - from));
}

// The same work over a different crew: total man-hours kept, rounded up to
// the quarter hour.
export function hoursForCrew(hours, fromCrew, toCrew) {
  const from = Math.max(1, Number(fromCrew) || 1);
  const to = Math.max(1, Number(toCrew) || 1);
  return Math.max(0.25, Math.ceil(((Number(hours) * from) / to) * 4) / 4);
}

// Sunday 64, Monday 1 … Saturday 32: the bitmask effective_workdays uses.
function workdayBit(date) {
  const dow = date.getDay();
  return dow === 0 ? 64 : 1 << (dow - 1);
}

const MAX_SPLIT_DAYS = 20; // the visits route's own cap on one booking

// "YYYY-MM-DD" of the days after `dayStr` that are workdays in `mask`, up
// to `count` of them, looking no further than 60 calendar days.
export function nextWorkdays(dayStr, mask, count) {
  const [y, m, d] = String(dayStr).split('-').map(Number);
  const cur = new Date(y, m - 1, d);
  const out = [];
  const bits = Number(mask) || 0;
  for (let i = 0; i < 60 && out.length < count; i += 1) {
    cur.setDate(cur.getDate() + 1);
    if (bits & workdayBit(cur)) {
      const pad = (n) => String(n).padStart(2, '0');
      out.push(`${cur.getFullYear()}-${pad(cur.getMonth() + 1)}-${pad(cur.getDate())}`);
    }
  }
  return out;
}

// The days a booking of `hours` takes: `firstHours` on the drop day, then
// whole shifts on the next workdays, the last one holding what is left.
// [{ day, hours }], or null when the rest cannot be placed (no shift
// length, no workdays, or more than the route's cap).
export function splitOverDays({ day, hours, firstHours, shiftLength, mask }) {
  const total = quarter(hours);
  const first = quarter(Math.min(firstHours, total));
  if (!(shiftLength > 0) || first <= 0) return null;
  let rest = quarter(total - first);
  const need = Math.ceil(rest / shiftLength);
  if (need + 1 > MAX_SPLIT_DAYS) return null;
  const later = nextWorkdays(day, mask, need);
  if (later.length < need) return null;
  const out = [{ day, hours: first }];
  for (const d of later) {
    const h = quarter(Math.min(shiftLength, rest));
    out.push({ day: d, hours: h });
    rest = quarter(rest - h);
  }
  return out;
}

// State for one DayOverflowDialog. `ask` resolves
// { tech_ids, lead, hours, days } — days null for one day, else the split —
// or null on Cancel / close.
export function useDayOverflowPrompt() {
  const prompt = ref({
    visible: false, seq: 0, jobLabel: '', techName: '', day: '', dayLabel: '', startHour: null,
    shift: null, mask: 0, hours: 0, techIds: [], leadId: null, droppedId: null,
  });
  let pending = null;
  let seq = 0;

  function ask(question) {
    if (pending) answer(null);
    prompt.value = { ...question, visible: true, seq: ++seq };
    return new Promise((resolve) => { pending = resolve; });
  }

  function answer(result) {
    const resolve = pending;
    pending = null;
    prompt.value = { ...prompt.value, visible: false };
    if (resolve) resolve(result);
  }

  return { prompt, ask, answer };
}
