/**
 * useTenantTimezone — the office's configured display timezone.
 *
 * Scheduling data (jobs, appointments) is stored/serialized in UTC. Rendering
 * it in the *browser's* zone produces off-by-one day bugs for anyone east or
 * west of the office: a job scheduled "Jul 10 8:00 PM" in US-Central serializes
 * to "2026-07-11T01:00:00Z", so naively slicing the UTC date string (or reading
 * a UTC-based day) drops it into the Jul 11 column. The fix is to bucket and
 * display everything in the tenant's configured zone (Settings → Time Clock →
 * Tenant timezone, e.g. America/Chicago), which is the single source of truth
 * for "what day/time is this job on".
 *
 * The timezone is a tenant-wide setting, so it's fetched once and shared via a
 * module-level singleton. `zonedDateKey(value)` is the reactive bound helper;
 * `dateKeyInZone(value, tz)` is the pure form (unit-testable, no Vue).
 */
import { ref } from 'vue';
import { useApi } from './useApi';

// Singleton: shared across every component, survives unmount, re-fetched on
// a full page reload.
const tenantTimezone = ref(null);
let _inFlight = null;

/**
 * 'YYYY-MM-DD' for `value` (Date | ISO string | epoch) in IANA zone `tz`.
 * Falls back to the browser-local calendar day when `tz` is falsy or invalid —
 * still better than a raw UTC slice, and correct once the tz loads.
 */
export function dateKeyInZone(value, tz) {
  // Guard null/undefined/'' explicitly — `new Date(null)` is epoch 0, not an
  // Invalid Date, so it would otherwise resolve to 1969/1970.
  if (value === null || value === undefined || value === '') return '';
  const d = value instanceof Date ? value : new Date(value);
  if (!d || Number.isNaN(d.getTime())) return '';
  const localKey = () => {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  };
  if (!tz) return localKey();
  try {
    // en-CA renders as YYYY-MM-DD; formatToParts is used so we never depend on
    // that locale quirk.
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(d);
    const get = (t) => parts.find((p) => p.type === t)?.value;
    const y = get('year');
    const m = get('month');
    const day = get('day');
    if (!y || !m || !day) return localKey();
    return `${y}-${m}-${day}`;
  } catch {
    // Unknown/invalid IANA name → don't throw, fall back to browser-local.
    return localKey();
  }
}

/**
 * Offset of IANA zone `tz` from UTC, in ms, at the instant `date`.
 * Positive west-of-nothing semantics: instant + offset = that zone's wall time
 * read as if it were UTC.
 */
function zoneOffsetMs(date, tz) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).formatToParts(date);
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
  // hour12:false renders midnight as "24" in some engines — normalize.
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second);
  return asUtc - date.getTime();
}

/**
 * Stored instant → a Date whose BROWSER-LOCAL fields read as the shop wall
 * clock. That is what a DatePicker renders, so the office sees shop time.
 *
 * Extracted from TimesheetsView (2026-08-13) so the tech's own timesheet uses
 * the identical conversion — two implementations of "which wall clock is this"
 * is how the two pages end up booking different hours for the same shift.
 */
export function toShopWallClock(iso, tz) {
  if (!iso) return null;
  const d = new Date(String(iso).replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return null;
  if (!tz) return d;
  try {
    const shifted = new Date(d.getTime() + zoneOffsetMs(d, tz));
    return new Date(
      shifted.getUTCFullYear(), shifted.getUTCMonth(), shifted.getUTCDate(),
      shifted.getUTCHours(), shifted.getUTCMinutes(), 0, 0,
    );
  } catch {
    return d;  // unknown IANA name — degrade to browser-local, never throw
  }
}

/**
 * Inverse: the picker's local fields are a shop wall clock → the real instant,
 * as a UTC ISO string for the wire. The offset is resolved twice because the
 * correct offset depends on the instant we are still solving for (DST).
 */
export function shopWallClockToIso(dateObj, tz) {
  if (!dateObj) return null;
  if (!tz) return dateObj.toISOString();
  const wallAsUtc = Date.UTC(
    dateObj.getFullYear(), dateObj.getMonth(), dateObj.getDate(),
    dateObj.getHours(), dateObj.getMinutes(), 0, 0,
  );
  try {
    let instant = new Date(wallAsUtc - zoneOffsetMs(new Date(wallAsUtc), tz));
    instant = new Date(wallAsUtc - zoneOffsetMs(instant, tz));
    return instant.toISOString();
  } catch {
    return dateObj.toISOString();
  }
}

export function useTenantTimezone() {
  const api = useApi();

  async function ensureLoaded(force = false) {
    if (tenantTimezone.value && !force) return tenantTimezone.value;
    if (_inFlight && !force) return _inFlight;
    _inFlight = (async () => {
      try {
        const data = await api.get('/api/me/timezone', { suppressErrorToast: true });
        tenantTimezone.value = data?.tenant_timezone || null;
      } catch {
        tenantTimezone.value = null;
      } finally {
        _inFlight = null;
      }
      return tenantTimezone.value;
    })();
    return _inFlight;
  }

  // Kick off the fetch on first use; callers read `tenantTimezone` reactively,
  // so computeds/renders that use it recompute once it resolves.
  ensureLoaded();

  return {
    tenantTimezone,
    ensureLoaded,
    zonedDateKey: (value) => dateKeyInZone(value, tenantTimezone.value),
  };
}
