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

/**
 * Parse a date-ONLY string ("YYYY-MM-DD") as a LOCAL calendar date. A bare
 * new Date("2026-07-14") is UTC midnight — one day EARLY in every US
 * timezone. Use this anywhere a backend `date` field feeds a DatePicker or
 * a formatter (caught in the GL S8 headed browser walk: the table showed
 * Jul 13 for a Jul 14 expense, and UTC-parsed edit dialogs walked the date
 * back a day on every save). Returns null for non-date-only input.
 */
export function parseLocalDateString(input) {
  if (typeof input !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(input)) return null;
  const [y, m, d] = input.split('-').map(Number);
  const local = new Date(y, m - 1, d);
  return isNaN(local) ? null : local;
}

function _toDate(input) {
  if (input === null || input === undefined || input === '') return null;
  if (input instanceof Date) return isNaN(input) ? null : input;
  const asLocalDate = parseLocalDateString(input);
  if (asLocalDate) return asLocalDate;
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

/**
 * Backfilled timestamps (QB sync: modules/quickbooks/sync.py stamps sent_at /
 * paid_at as UTC MIDNIGHT of the business date) mean "we know the day, not
 * the minute". Rendering them through the normal datetime path walks the day
 * back in every US timezone and invents a phantom evening time. These helpers
 * detect the convention and render the UTC calendar date as a local calendar
 * date instead. A real send at exactly 00:00:00 UTC degrades to date-only
 * display — acceptable.
 */
const DATE_ONLY_STAMP = /T00:00:00(?:\.0+)?(?:\+00:00|Z)?$/;

export function isDateOnlyStamp(input) {
  return typeof input === 'string' && DATE_ONLY_STAMP.test(input);
}

export function formatStampDate(input, opts) {
  return formatDate(isDateOnlyStamp(input) ? input.slice(0, 10) : input, opts);
}

export function formatStampDateTime(input, opts) {
  return isDateOnlyStamp(input)
    ? formatDate(input.slice(0, 10), opts)
    : formatDateTime(input, opts);
}

/**
 * Epoch millis for date-window FILTERING, honoring the same conventions as
 * the display helpers above: date-only strings parse as LOCAL calendar
 * dates (a bare new Date("2026-07-14") is UTC midnight — previous evening
 * in every US timezone, so "Today" filters would miss today's rows), and
 * UTC-midnight backfill stamps are treated as date-only so a Jan 1 payment
 * doesn't land in last year. Returns null for empty/unparseable input.
 */
export function stampTime(input) {
  if (!input) return null;
  const d = isDateOnlyStamp(input)
    ? parseLocalDateString(input.slice(0, 10))
    : _toDate(input);
  const t = d ? d.getTime() : NaN;
  return Number.isNaN(t) ? null : t;
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
 * US phone → "(111)222-3333". Strips non-digits, drops a leading US country
 * code, and only reformats a clean 10-digit number — anything else (extensions,
 * international, still-being-typed partials) passes through unchanged so a
 * number is never mangled or hidden. Empty → '' (not the em-dash placeholder,
 * so callers can render their own "no phone" affordance / tel: link guard).
 */
export function formatPhone(value) {
  if (value === null || value === undefined || value === '') return '';
  const digits = String(value).replace(/\D/g, '');
  const ten = digits.length === 11 && digits.startsWith('1') ? digits.slice(1) : digits;
  if (ten.length !== 10) return String(value);
  return `(${ten.slice(0, 3)})${ten.slice(3, 6)}-${ten.slice(6)}`;
}

/**
 * Composable form for views that prefer destructured imports of the
 * functions all in one go. No setup required, but matches the project
 * `useX()` convention.
 */
export function useFormatters() {
  return { formatDate, formatDateTime, formatTime, formatMoney, formatPercent, formatNumber, formatPhone };
}
