// Single date/time formatter for the frontend.
//
// Background: PG timestamptz serializes as "2026-04-08 16:12:20.838053+00"
// (space-separated, microseconds, +00 offset). Several views previously
// rendered this raw — F-066 in the 2026-04-29 audit. The cheap one-liner
// `value.split("T")[0]` only matches ISO-8601 form and falls through to the
// raw string for the PG shape.
//
// Strategy: replace the space with 'T' before handing to Date(), then defer
// to toLocaleDateString / toLocaleString. Falls back to the raw value if
// parsing fails (so a malformed timestamp shows itself, not a silent dash).
//
// Style options:
//   'date'     → 4/8/2026          (default)
//   'datetime' → 4/8/2026, 4:12 PM
//   'short'    → Apr 8

const PLACEHOLDER = '—';

function _parse(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const normalized = typeof value === 'string' ? value.replace(' ', 'T') : value;
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function formatTimestamp(value, style = 'date') {
  const d = _parse(value);
  if (!d) return value ? String(value) : PLACEHOLDER;

  if (style === 'datetime') {
    return d.toLocaleString(undefined, {
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  }
  if (style === 'short') {
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  return d.toLocaleDateString();
}

export default formatTimestamp;
