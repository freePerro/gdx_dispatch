/**
 * useFormatters — single source of truth for date / currency / percent.
 *
 * Replaces 18+ local `formatDate` / `formatCurrency` / `formatDateTime`
 * definitions scattered across views (audit_ux_2026-05-05_data_density_and_tables.md §H).
 *
 * Conventions:
 *   - null / undefined / '' → returns '—' (em-dash placeholder)
 *   - Locale defaults to navigator.language; pass `locale` to override.
 *   - Currency defaults to USD.
 *   - Date inputs accept Date | ISO string | epoch number.
 *
 * Pure functions — no Vue setup required. The "use" prefix is conventional
 * (matches useApi / usePermission) but no reactivity is involved.
 */

const PLACEHOLDER = '—';

function _toDate(input) {
  if (input === null || input === undefined || input === '') return null;
  if (input instanceof Date) return isNaN(input) ? null : input;
  const d = new Date(input);
  return isNaN(d) ? null : d;
}

export function formatDate(input, { locale, options } = {}) {
  const d = _toDate(input);
  if (!d) return PLACEHOLDER;
  const opts = options || { year: 'numeric', month: 'short', day: 'numeric' };
  return new Intl.DateTimeFormat(locale, opts).format(d);
}

export function formatDateTime(input, { locale, options } = {}) {
  const d = _toDate(input);
  if (!d) return PLACEHOLDER;
  const opts = options || {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  };
  return new Intl.DateTimeFormat(locale, opts).format(d);
}

export function formatTime(input, { locale, options } = {}) {
  const d = _toDate(input);
  if (!d) return PLACEHOLDER;
  return new Intl.DateTimeFormat(locale, options || { hour: 'numeric', minute: '2-digit' }).format(d);
}

/**
 * Currency. Accepts number or numeric string. Cents are NOT auto-divided —
 * callers should pass dollars (e.g., 12.50 not 1250). For cent-stored values
 * divide before formatting.
 */
export function formatMoney(value, { currency = 'USD', locale, digits = 2 } = {}) {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return PLACEHOLDER;
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency,
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
}

/**
 * Percent. Accepts a fraction (0.25 → 25%) by default. Pass `whole: true` if
 * the input is already in percent units (25 → 25%).
 */
export function formatPercent(value, { digits = 1, locale, whole = false } = {}) {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return PLACEHOLDER;
  const v = whole ? n / 100 : n;
  return new Intl.NumberFormat(locale, {
    style: 'percent',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(v);
}

export function formatNumber(value, { locale, digits } = {}) {
  if (value === null || value === undefined || value === '') return PLACEHOLDER;
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) return PLACEHOLDER;
  const opts = digits !== undefined
    ? { minimumFractionDigits: digits, maximumFractionDigits: digits }
    : undefined;
  return new Intl.NumberFormat(locale, opts).format(n);
}

/**
 * Composable form for views that prefer destructured imports of the
 * functions all in one go. No setup required, but matches the project
 * `useX()` convention.
 */
export function useFormatters() {
  return { formatDate, formatDateTime, formatTime, formatMoney, formatPercent, formatNumber };
}
