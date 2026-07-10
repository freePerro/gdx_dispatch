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
