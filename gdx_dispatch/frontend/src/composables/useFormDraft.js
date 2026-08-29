/**
 * useFormDraft — keep what the user TYPED across an accidental dismissal.
 *
 * 2026-08-29: the Planner's create dialogs held their form in a plain ref, so
 * an accidental dismissal destroyed whatever had been typed. Two vectors:
 * the browser/Android back button (PrimeVue overlays push no history entry, so
 * back navigates the route away and unmounts the view — there is no keep-alive
 * on /planner), and, on mobile only, reopening the dialog (MobilePlannerView's
 * openCreate() reset the form on every open, so the X discarded for good what
 * the desktop view happened to keep).
 *
 * Two rules, both learned from the adversarial review of the first draft of
 * this fix. Neither is optional:
 *
 *   1. A draft restores TEXT, never REFERENCES. `assigned_to`, `job_id` and
 *      `customer_id` are foreign keys the user chose deliberately and cannot
 *      see once a dialog reopens. Resurrecting one invisibly files work
 *      against the wrong customer or job — strictly worse than losing a
 *      sentence. Callers pass an explicit `fields` allow-list; there is no
 *      "persist the whole form" mode, on purpose.
 *
 *   2. Dates round-trip as the CALENDAR STRING the API wants, never as a
 *      JSON-serialized Date. `JSON.stringify(new Date())` yields an instant
 *      ("2026-08-29T05:00:00.000Z"); restoring that leaves a string, so the
 *      caller's `x instanceof Date` test goes false, `localDateString()` is
 *      skipped, and an instant ships where every other writer ships
 *      'YYYY-MM-DD'. That is the off-by-one-day shape this repo has already
 *      fixed twice (see planner.py `_date_out`).
 *
 * Storage is sessionStorage, NOT localStorage. It is per-tab, so a draft
 * cannot outlive the tab or surface for the next person on a shared office
 * workstation, while still surviving the two things that actually lose work:
 * a route change (the back button) and a reload.
 *
 * Deliberately NOT cleared by auth's `_clearSession()`. Adding it there is the
 * obvious-looking move and it is a trap: `useIdleLogout` calls `logout()` on
 * the inactivity timer, so wiping drafts on that path means walking away from
 * the desk destroys the note — the original bug, re-entered sideways.
 */
import { getCurrentInstance, onUnmounted, ref } from 'vue';
import { localDateString, parseLocalDateString } from './useFormatters';

/**
 * @param {string} name  storage suffix, e.g. 'planner_task_new'
 * @param {object} opts
 * @param {string[]} opts.fields    allow-list of field names to persist
 * @param {string[]} [opts.dates]   subset of `fields` holding Date objects
 * @param {object}  [opts.defaults] per-field pristine value; a field equal to
 *                                  its default is not "content", so an
 *                                  untouched form never writes a draft
 * @param {number}  [opts.debounceMs]
 */
export function useFormDraft(name, { fields, dates = [], defaults = {}, debounceMs = 400, getState } = {}) {
  const key = `gdx_draft_${name}`;
  const restored = ref(false);
  let timer = null;

  /** Reduce a live form object to the persistable, allow-listed subset. */
  function _pick(state) {
    const out = {};
    for (const f of fields) {
      let v = state?.[f];
      if (v === undefined || v === null || v === '') continue;
      if (Object.prototype.hasOwnProperty.call(defaults, f) && v === defaults[f]) continue;
      if (dates.includes(f)) {
        // Store the calendar day, never the instant. See rule 2 above.
        // localDateString normalizes a Date, a 'YYYY-MM-DD' string, and an
        // already-serialized ISO instant to one shape, so a draft written by
        // an older build can never reintroduce the instant.
        v = localDateString(v);
        if (!v) continue;
      }
      out[f] = v;
    }
    return out;
  }

  function _write(state) {
    const picked = _pick(state);
    try {
      if (!Object.keys(picked).length) sessionStorage.removeItem(key);
      else sessionStorage.setItem(key, JSON.stringify(picked));
    } catch {
      // Private mode / quota-blocked storage: a draft is a convenience, never
      // a correctness guarantee. Degrade silently rather than break the form.
    }
  }

  /** Debounced write — wire to a deep watcher on the form. */
  function save(state) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => _write(state), debounceMs);
  }

  /**
   * Write immediately, cancelling any pending debounce. Bind to the dialog's
   * @hide, and see the unmount/pagehide wiring below: without those, text typed
   * inside the debounce window before an accidental close or a hard unload
   * would never reach storage at all.
   */
  function flush(state) {
    if (timer) { clearTimeout(timer); timer = null; }
    _write(state);
  }

  function read() {
    try {
      const raw = sessionStorage.getItem(key);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }

  /**
   * Apply a stored draft onto a freshly-reset form object, in place.
   * Returns true when anything was restored (drives the "Draft restored" hint).
   */
  function applyTo(target) {
    const draft = read();
    restored.value = false;
    if (!draft || !target) return false;
    let any = false;
    for (const f of fields) {
      if (!Object.prototype.hasOwnProperty.call(draft, f)) continue;
      let v = draft[f];
      if (dates.includes(f)) {
        // Parse as a LOCAL calendar date so the picker doesn't walk it back a
        // day — same reason editTask() uses parseLocalDateString.
        v = parseLocalDateString(v) || null;
        if (!v) continue;
      }
      if (v === undefined || v === null || v === '') continue;
      target[f] = v;
      any = true;
    }
    restored.value = any;
    return any;
  }

  function clear() {
    if (timer) { clearTimeout(timer); timer = null; }
    restored.value = false;
    try {
      sessionStorage.removeItem(key);
    } catch {
      /* nothing to clear */
    }
  }

  // The debounce is a 400ms hole: an unmount or a real page unload inside that
  // window would drop the very keystrokes the user is about to lose. A route
  // change is survivable without this (the timer outlives the component), but
  // a hard reload, a discarded tab, or Android back off the FIRST history
  // entry — where the PWA share target lands straight on /mobile/planner —
  // unloads the document before the timer fires. Callers that pass `getState`
  // get a synchronous flush on all three.
  if (typeof getState === 'function') {
    const flushNow = () => flush(getState());
    // pagehide covers reload/close/bfcache; visibilitychange is the one iOS
    // Safari reliably fires when the app is backgrounded mid-note.
    const onHidden = () => { if (document.visibilityState === 'hidden') flushNow(); };
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', flushNow);
      document.addEventListener('visibilitychange', onHidden);
    }
    if (getCurrentInstance()) {
      onUnmounted(() => {
        if (typeof window !== 'undefined') {
          window.removeEventListener('pagehide', flushNow);
          document.removeEventListener('visibilitychange', onHidden);
        }
        flushNow();
      });
    }
  }

  return { restored, save, flush, applyTo, clear, read, storageKey: key };
}
