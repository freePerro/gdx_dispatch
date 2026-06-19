// Phase 1.3 C4 in-app fallback — track per-job last-seen timestamps for
// the tech's parts list. Pulled out of MobileTodayView so the unseen-count
// math is unit-testable without mounting the whole view.
//
// Storage shape: { [job_id]: ISOString }. Stored under SEEN_KEY in the
// caller's storage backend (defaults to window.localStorage, overridable
// for tests). Best-effort — quota / private-mode errors are swallowed.

export const SEEN_KEY = "gdx_mobile_parts_seen_v1";

export function readSeen(storage = globalThis?.localStorage) {
  if (!storage) return {};
  try {
    const raw = storage.getItem(SEEN_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function writeSeen(map, storage = globalThis?.localStorage) {
  if (!storage) return;
  try {
    storage.setItem(SEEN_KEY, JSON.stringify(map));
  } catch {
    /* quota / private mode */
  }
}

export function markJobSeen(jobId, storage = globalThis?.localStorage, now = new Date()) {
  const seen = readSeen(storage);
  seen[jobId] = now.toISOString();
  writeSeen(seen, storage);
  return seen;
}

// Count parts where dispatch acted (status ∈ ordered/received) AFTER the
// tech last viewed this job's panel. New parts (no last-seen) don't
// count — they show up via the parts_summary pill itself.
export function countUnseenForJob(jobId, partsList, storage = globalThis?.localStorage) {
  const seen = readSeen(storage);
  const lastSeen = seen[jobId];
  if (!lastSeen) return 0;
  const cutoff = new Date(lastSeen).getTime();
  if (Number.isNaN(cutoff)) return 0;
  let count = 0;
  for (const p of partsList || []) {
    if (!p?.updated_at) continue;
    const u = new Date(p.updated_at).getTime();
    if (Number.isNaN(u)) continue;
    if (u > cutoff && (p.status === "ordered" || p.status === "received")) {
      count += 1;
    }
  }
  return count;
}
