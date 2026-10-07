// Multi-day jobs, PR 3 (day-close). Shared wording and parsing for the
// "Is this job finished?" sheet, the daily log, the job page's
// Close-without-work dialog and the offline queue's failed list.
//
// The wording lives here once so the sheet and the failed list can never tell
// a tech two different things about the same refused day.
import { formatDate } from '../composables/useFormatters'

/** Hours a person may attest for one day: more than 0, at most 24. */
export function validDayHours(value) {
  if (value === '' || value === null || value === undefined) return false
  const n = Number(value)
  return Number.isFinite(n) && n > 0 && n <= 24
}

/** "8", "7.5", "7.25" — never "8.00", which reads like money. */
export function formatHours(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return '0'
  return String(Math.round(n * 100) / 100)
}

/** "YYYY-MM-DD" → "Tuesday, Oct 6" (a local calendar date, never UTC-shifted). */
export function formatDayLong(day) {
  if (!day) return ''
  return formatDate(String(day).slice(0, 10), {
    options: { weekday: 'long', month: 'short', day: 'numeric' },
  })
}

/**
 * A refusal's machine fields, wherever the server put them. The contract puts
 * `code` beside `detail` at the top level; FastAPI's HTTPException(detail={...})
 * nests them under `detail`. Both are read so neither shape is silently lost.
 * Accepts an Error from useApi/useOfflineSync (reads `.body`) or a bare body.
 */
export function refusalOf(errOrBody) {
  const body = errOrBody && typeof errOrBody === 'object' && 'body' in errOrBody
    ? errOrBody.body
    : errOrBody
  const nested = body && typeof body.detail === 'object' && body.detail && !Array.isArray(body.detail)
    ? body.detail
    : null
  const src = nested || body || {}
  const detailText = typeof src.detail === 'string'
    ? src.detail
    : typeof src.message === 'string'
      ? src.message
      : (typeof body?.detail === 'string' ? body.detail : '')
  return {
    code: src.code || body?.code || null,
    date: src.date || body?.date || null,
    detail: detailText || (errOrBody instanceof Error ? errOrBody.message : '') || '',
    already_closed: src.already_closed || body?.already_closed || null,
  }
}

/** Σ people hours + Σ added hours of a day-close body — "N h" for the office. */
export function dayCloseTotalHours(body) {
  const sum = (rows) => (Array.isArray(rows) ? rows : [])
    .reduce((t, r) => t + (Number(r?.hours) || 0), 0)
  return sum(body?.people) + sum(body?.added)
}

function _closers(rows) {
  const names = [...new Set((rows || []).map((r) => r?.closed_by).filter(Boolean))]
  return names.length ? names.join(', ') : 'someone else'
}

/**
 * "Already closed by <name> with N h. Tell the office if your N h on <date>
 * differ." — `alreadyClosed` is the 409's `already_closed` ({rows: [...]}).
 */
export function alreadyClosedText(alreadyClosed, yourHours, day) {
  const rows = Array.isArray(alreadyClosed?.rows) ? alreadyClosed.rows : []
  const theirs = rows.reduce((t, r) => t + (Number(r?.hours) || 0), 0)
  return `Already closed by ${_closers(rows)} with ${formatHours(theirs)} h. `
    + `Tell the office if your ${formatHours(yourHours)} h on ${formatDayLong(day)} differ.`
}

/** A queued "No" replayed onto a job somebody finished meanwhile. */
export function jobFinishedText(hours, day) {
  return `Day not recorded: the job was already finished. Tell the office: ${formatHours(hours)} h on ${formatDayLong(day)}.`
}

/** "Close <weekday, date> first: answer No for that day" — Yes is disabled. */
export function earlierDayOpenText(day) {
  return `Close ${formatDayLong(day)} first: answer No for that day`
}
