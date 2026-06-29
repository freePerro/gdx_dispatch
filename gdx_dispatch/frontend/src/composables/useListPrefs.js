import { watch } from "vue";
import { readPrefs, writePrefs } from "../utils/listPrefs";

// Persist a set of existing reactive refs (a view's filter/tab/search state)
// to localStorage so the user's chosen view survives a page reload. Power
// users (dispatchers living on "Scheduled", AR clerks on "Overdue") otherwise
// re-pick their filter on every reload.
//
//   namespace  storage bucket, e.g. "jobs" -> "gdx.listprefs.jobs"
//   refs       { key: Ref } — the view's own refs, mutated in place on load
//   schema     { key: { default, valid? } } — see utils/listPrefs.js
//
// On mount: loads + validates stored values and assigns them to the refs
// BEFORE first paint. Thereafter: any change to a tracked ref is written back.
// localStorage access is wrapped so private-mode / disabled-storage degrades
// to in-memory state (matches useViewMode's posture).
const PREFIX = "gdx.listprefs.";

export function useListPrefs(namespace, refs, schema) {
  const storageKey = PREFIX + namespace;
  const keys = Object.keys(schema);

  let raw = null;
  try {
    raw = localStorage.getItem(storageKey);
  } catch {
    /* storage unavailable — keep defaults, persistence becomes a no-op */
  }

  const initial = readPrefs(raw, schema);
  for (const key of keys) {
    if (refs[key]) refs[key].value = initial[key];
  }

  // Watch every tracked ref via getter sources; primitives so no deep watch.
  watch(
    keys.map((key) => () => refs[key]?.value),
    () => {
      const values = {};
      for (const key of keys) values[key] = refs[key]?.value;
      try {
        localStorage.setItem(storageKey, writePrefs(values, schema));
      } catch {
        /* storage unavailable — silently skip persistence */
      }
    },
  );

  return { storageKey };
}
