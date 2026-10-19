// The vacation year a balance counts over, as a label (GDXA-1902).
//
// GET /api/timeclock/time-off/balances answers each person over their own
// vacation year (core/time_off.vacation_window): the calendar year, or under
// `hire_anniversary` the year from their hire anniversary. `year` on a row is
// only the year that window STARTS in, so "Vacation 2025" over a Jun 3 2025 –
// Jun 2 2026 window told a tech in January the wrong year. The label comes
// from `window_start` / `window_end`; a bare year is said only when the window
// is exactly Jan 1 – Dec 31 of it.

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

// "2026-06-03" → [2026, 6, 3]. Parsed by hand: `new Date('2026-06-03')` is UTC
// midnight, which is the day before anywhere west of Greenwich.
function parseDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''))
  if (!m) return null
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])]
  if (mo < 1 || mo > 12 || d < 1 || d > 31) return null
  return [y, mo, d]
}

function dayText([y, mo, d]) {
  return `${MONTHS[mo - 1]} ${d} ${y}`
}

/**
 * "2026" for a calendar-year window, "Jun 3 2025 – Jun 2 2026" otherwise.
 * A row with no readable window (an older server) falls back to its `year`;
 * with neither, ''.
 *
 * @param {{year?: number, window_start?: string|null, window_end?: string|null}} balance
 * @returns {string}
 */
export function vacationWindowLabel(balance) {
  const start = parseDay(balance?.window_start)
  const end = parseDay(balance?.window_end)
  if (!start || !end) return balance?.year != null ? String(balance.year) : ''
  const calendarYear = start[0] === end[0]
    && start[1] === 1 && start[2] === 1
    && end[1] === 12 && end[2] === 31
  if (calendarYear) return String(start[0])
  return `${dayText(start)} – ${dayText(end)}`
}
