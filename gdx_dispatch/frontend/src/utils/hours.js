// Hours → short label, the way the dispatch board reads them: "8h", "7.5h",
// "10h", "0h".
//
// Every copy of this before 2026-09-01 (#521) did
//   (Math.round(n * 100) / 100).toString().replace(/\.?0+$/, '')
// to turn "2.50" into "2.5". Nothing in that regex requires a decimal point,
// so it also ate the zeros off whole numbers: 10 → "1", 20 → "2", 100 → "1",
// 0 → "". A 07:00–17:00 shift read "of 1h" and an idle tech read "h of 9h".
// The bar and the over-capacity styling used the number, so only the label
// lied — which is why it survived.
//
// JS already renders 10 as "10" and 2.5 as "2.5"; rounding to two places is
// the whole job. Never add a trailing-zero trim here.

/**
 * @param {unknown} value hours, numeric or numeric string
 * @returns {string|null} "10", "2.5", "0" — or null when it is not a number
 */
export function formatHoursNumber(value) {
  if (value == null || value === '') return null
  const n = Number(value)
  if (!Number.isFinite(n)) return null
  return String(Math.round(n * 100) / 100)
}

/**
 * "10h" / "2.5h" / "0h"; "?h" when there is no usable number (the board's
 * "no estimate" marker).
 */
export function formatDurationHours(value) {
  const text = formatHoursNumber(value)
  return text == null ? '?h' : `${text}h`
}
