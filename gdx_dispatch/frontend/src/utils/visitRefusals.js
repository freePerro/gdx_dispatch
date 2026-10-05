/**
 * Visit refusals and arrival-undo wording (multi-day jobs plan §5.2a).
 *
 * The backend answers a refused visit change with 409 and a flat body:
 *   { detail: "<user-facing sentence>", code: "<kind>", ...fields }
 * (routers/jobs.py `_visit_refused`, routers/appointments.py `_conflict`,
 * services/arrival_undo.py `UndoRefused.body`). `useApi` throws an Error whose
 * message is `detail` and whose `body` is the parsed JSON — `err.code` is only
 * filled for a nested `{detail: {code}}`, so the code is read off `err.body`.
 */
import { formatTime, parseLocalDateString } from '../composables/useFormatters';
import { ADMIN, DISPATCHER, MANAGER, OWNER, normalizeRole } from '../constants/roles';

/** Refusal codes whose way out is the Appointments page. */
export const APPOINTMENTS_PAGE_CODES = new Set([
  'crew_on_site',
  'onto_booked_day',
  'two_open_visits',
  'double_booked',
  'visit_arrived',
]);

/** The structured refusal carried by an API error, or null. */
export function refusalOf(err) {
  const body = err?.body;
  if (!body || typeof body !== 'object' || typeof body.code !== 'string') return null;
  return {
    ...body,
    code: body.code,
    detail: typeof body.detail === 'string' ? body.detail : (err?.message || ''),
  };
}

export function isNeedsAnswer(err, question) {
  const r = refusalOf(err);
  return !!r && r.code === 'needs_answer' && (!question || r.question === question);
}

/** True when the refusal's own sentence sends the office to the Appointments page. */
export function pointsAtAppointments(refusal) {
  if (!refusal) return false;
  if (APPOINTMENTS_PAGE_CODES.has(refusal.code)) return true;
  return refusal.code === 'needs_answer' && refusal.question === 'status_only_arrival';
}

/** Undo arrival is a dispatch-manager action: core/roles.py DISPATCH_MANAGER_ROLES. */
export function isDispatchManagerRole(role) {
  const r = normalizeRole(role);
  return r === OWNER || r === ADMIN || r === DISPATCHER || r === MANAGER;
}

/** "2026-10-06" → "Tue 10/6" (a shop day, read as a local calendar date). */
export function formatShopDay(iso) {
  const d = parseLocalDateString(String(iso || '').slice(0, 10));
  if (!d) return String(iso || '');
  const wd = new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(d);
  return `${wd} ${d.getMonth() + 1}/${d.getDate()}`;
}

/** "8:14 AM" from an ISO timestamp. */
export function formatClock(iso) {
  return formatTime(iso);
}

/** A tap's time to the second — two presses 1.3 s apart must read apart. */
export function formatTapTime(iso) {
  return formatTime(iso, { options: { hour: 'numeric', minute: '2-digit', second: '2-digit' } });
}

export function joinNames(names) {
  const list = (names || []).filter(Boolean);
  if (list.length <= 1) return list[0] || '';
  if (list.length === 2) return `${list[0]} and ${list[1]}`;
  return `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;
}

/** The Re-open dialog's question (plan §5.2a R0). */
export function rebookQuestion(names, dayIso) {
  const who = joinNames(names) || 'A crew tech';
  const verb = (names || []).filter(Boolean).length > 1 ? 'have' : 'has';
  return `${who} already ${verb} a closed visit on ${formatShopDay(dayIso)}. Book them on that day again?`;
}

const NOT_REVERTED_REASON = {
  edited_since: 'it was changed since the tap',
  double_book: 'the tech already has another visit on that day',
  closed: 'the day or the job is closed',
  crew_came: 'the tech tapped again on that day',
  other_arrival: 'another arrival on the job still stands',
  no_record: 'no tap record matches it',
};

function fieldLabel(field) {
  const f = String(field || '');
  if (f.startsWith('visit.start_at')) return 'The visit stays where it is';
  if (f === 'job.arrived_at') return "The job's arrival time was left";
  if (f === 'dispatch_status') return 'The job stays On site';
  if (f.startsWith('assignment.')) return "The tech's arrival on the crew was left";
  return `${f} was left`;
}

/** One sentence per skipped revert (`not_reverted: [{field, reason}]`). */
export function notRevertedLines(list) {
  return (list || []).map((item) => {
    if (item?.field === 'tap' && item?.reason === 'no_record') {
      return 'No tap was undone — none was found or named for this visit.';
    }
    const why = NOT_REVERTED_REASON[item?.reason] || String(item?.reason || 'unknown reason');
    return `${fieldLabel(item?.field)} — ${why}.`;
  });
}
