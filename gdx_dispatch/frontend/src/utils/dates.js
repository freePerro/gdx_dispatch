/**
 * Render a date value as a local-zone short string (e.g. "5/3/2026").
 *
 * Date-only ISO strings ('YYYY-MM-DD') parse as UTC midnight; rendering them
 * via toLocaleDateString in a negative-offset zone (e.g. CST) shifts to the
 * previous day. Detect the date-only shape and format the components directly
 * so the on-screen date matches the source. Full ISO timestamps with a time
 * portion fall through to the locale renderer.
 */
export function formatDate(value) {
  if (value === null || value === undefined || value === '') return ''
  if (typeof value === 'string') {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
    if (m) {
      const [, y, mo, d] = m
      return `${parseInt(mo, 10)}/${parseInt(d, 10)}/${y}`
    }
  }
  const t = Date.parse(value)
  if (Number.isNaN(t)) return String(value)
  return new Date(t).toLocaleDateString()
}
