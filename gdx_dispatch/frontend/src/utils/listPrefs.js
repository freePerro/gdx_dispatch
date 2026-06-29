// Pure, storage-agnostic helpers for persisted list-view preferences
// (status tab, search text, date preset). Kept free of localStorage and Vue
// so the merge/validation logic is fully unit-testable — same split as
// sidebarFavorites.js (pure resolver) + the composable that wires storage.
//
// A "schema" maps a pref key to { default, valid? }:
//   default      value used when the stored blob omits the key
//   valid(value) optional guard — if it returns false the stored value is
//                discarded and `default` is used. This is the important
//                robustness guard: a stale persisted status (e.g. a tab that
//                was later removed from the taxonomy) would otherwise filter
//                the list down to nothing with no obvious cause.

// Parse a raw storage string into a validated prefs object. Never throws.
export function readPrefs(raw, schema) {
  let parsed = null;
  if (raw != null) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
  }
  // Reject non-plain-object payloads (null, arrays, primitives) — fall back
  // to an empty object so every key resolves to its default.
  if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
    parsed = {};
  }

  const out = {};
  for (const [key, spec] of Object.entries(schema)) {
    const has = Object.prototype.hasOwnProperty.call(parsed, key);
    let value = has ? parsed[key] : spec.default;
    if (spec.valid && !spec.valid(value)) {
      value = spec.default;
    }
    out[key] = value;
  }
  return out;
}

// Serialize the current values, restricted to keys declared in the schema
// (so a view passing extra refs never leaks unrelated state into storage).
export function writePrefs(values, schema) {
  const out = {};
  for (const key of Object.keys(schema)) {
    out[key] = values[key];
  }
  return JSON.stringify(out);
}
