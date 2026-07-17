/**
 * Offline job-photo capture.
 *
 * Live data says 0 photos across 205 jobs — the endpoint has been built and
 * slot-tagged since Sprint 3 with no UI to reach it, and the Dexie `photos`
 * store ("captured photo blobs awaiting upload") has sat there with zero
 * writers and zero readers.
 *
 * A tech shoots a door inside a garage, in a rural driveway, behind a
 * building. The dead zones ARE the use case, so the blob is stored FIRST and
 * uploaded when there's signal. `postQueued` can't do this — _drainOne
 * hardcodes JSON and JSON.stringify — which is a limit of that one function,
 * not of the offline layer.
 *
 * Pinned here:
 *  1. Online: uploads as multipart, with the kind, THROUGH the shared API
 *     client — a bare fetch loses the tenant header and, fatally, the 401
 *     refresh-and-retry that a photo drained hours after capture depends on.
 *  2. Offline: kept, reported as queued — never silently dropped, never
 *     reported as uploaded.
 *  3. Reconnect drains it.
 *  4. A permanently-rejected photo (400/404/413/415/422) stops retrying but
 *     KEEPS its blob — the tech was told it was saved and has driven away from
 *     the door. Rejection is a reason to tell someone, not to destroy the only
 *     copy. 401 is NOT in that set: it's transient and must retry.
 *  5. A 5xx/network failure keeps the photo for the next attempt.
 *  6. An uploaded blob is released — a truck of full-size photos would
 *     otherwise evict the whole IndexedDB origin, taking the JSON queue (and
 *     the tech's unsynced closeouts) with it.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";

vi.mock("../../lib/offlineDb", () => {
  const rows = new Map();
  const table = {
    put: vi.fn(async (r) => { rows.set(r.id, { ...r }); return r.id; }),
    get: vi.fn(async (id) => (rows.has(id) ? { ...rows.get(id) } : undefined)),
    delete: vi.fn(async (id) => { rows.delete(id); }),
    update: vi.fn(async (id, patch) => {
      if (!rows.has(id)) return 0;
      rows.set(id, { ...rows.get(id), ...patch });
      return 1;
    }),
    where: vi.fn(() => ({
      equals: vi.fn((status) => ({
        count: vi.fn(async () => [...rows.values()].filter((r) => r.status === status).length),
        sortBy: vi.fn(async () => [...rows.values()].filter((r) => r.status === status)),
      })),
    })),
    __rows: rows,
  };
  return {
    db: { photos: table },
    QUEUE_STATUS: Object.freeze({
      PENDING: "pending", SYNCING: "syncing", SYNCED: "synced", FAILED: "failed",
    }),
  };
});

// The upload goes through the shared API client — that's what buys the 401
// refresh-and-retry a queued photo needs when it drains hours later.
const postMock = vi.fn();
vi.mock("../useApi", () => ({
  createApiClient: () => ({ post: postMock }),
}));

const { db } = await import("../../lib/offlineDb");
const { capturePhoto, drainPhotos, usePhotoQueue } = await import("../usePhotoQueue");

/** Reject the way useApi does — a thrown error carrying `status`. */
function httpError(status) {
  return Object.assign(new Error(`HTTP ${status}`), { status });
}

function fakeBlob(name = "door.jpg") {
  const b = new Blob(["xxxx"], { type: "image/jpeg" });
  b.name = name;
  return b;
}

function setOnline(v) {
  Object.defineProperty(navigator, "onLine", { value: v, configurable: true });
}

beforeEach(() => {
  db.photos.__rows.clear();
  vi.clearAllMocks();
  setOnline(true);
  postMock.mockResolvedValue({ ok: true, photo_id: "srv-1" });
});

afterEach(() => { vi.unstubAllGlobals?.(); });

describe("online capture", () => {
  it("uploads as multipart with the kind", async () => {
    const r = await capturePhoto("job-1", fakeBlob(), "before");
    expect(r.queued).toBe(false);

    const [url, body] = postMock.mock.calls[0];
    // The SAME route the desktop posts to — a tech photo and an office
    // photo must land in one place, or the office can never find it.
    expect(url).toBe("/api/jobs/job-1/photos");
    expect(body).toBeInstanceOf(FormData);
    expect(body.get("kind")).toBe("before");
    expect(body.get("file")).toBeTruthy();
  });

  it("uploads through the shared client, not a bare fetch", async () => {
    // useApi already detects FormData (leaving Content-Type alone so the
    // browser sets the multipart boundary), adds the tenant header, and
    // refreshes on 401. A hand-rolled fetch silently loses all three.
    await capturePhoto("job-1", fakeBlob(), null);
    expect(postMock).toHaveBeenCalledTimes(1);
    expect(postMock.mock.calls[0][1]).toBeInstanceOf(FormData);
  });

  it("omits kind when the tech didn't pick one (server defaults it)", async () => {
    await capturePhoto("job-1", fakeBlob(), null);
    expect(postMock.mock.calls[0][1].get("kind")).toBeNull();
  });

  it("releases the blob once uploaded", async () => {
    const r = await capturePhoto("job-1", fakeBlob(), "after");
    const row = await db.photos.get(r.id);
    expect(row.status).toBe("synced");
    expect(row.blob).toBeNull();
  });
});

describe("offline capture — the reason this exists", () => {
  it("keeps the photo and reports it queued rather than uploaded", async () => {
    setOnline(false);
    const r = await capturePhoto("job-1", fakeBlob(), "during");

    expect(r.queued).toBe(true);
    expect(postMock).not.toHaveBeenCalled();
    const row = await db.photos.get(r.id);
    expect(row.status).toBe("pending");
    expect(row.blob).toBeTruthy(); // still on the phone, not lost
  });

  it("keeps the photo when the network drops mid-upload", async () => {
    postMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const r = await capturePhoto("job-1", fakeBlob(), "during");

    expect(r.queued).toBe(true);
    const row = await db.photos.get(r.id);
    expect(row.status).toBe("pending");
    expect(row.blob).toBeTruthy();
  });

  it("uploads what was stored once signal returns", async () => {
    setOnline(false);
    const r = await capturePhoto("job-1", fakeBlob(), "before");
    expect((await db.photos.get(r.id)).status).toBe("pending");

    setOnline(true);
    await drainPhotos();

    expect(postMock).toHaveBeenCalledTimes(1);
    expect((await db.photos.get(r.id)).status).toBe("synced");
  });

  it("does not try to drain while still offline", async () => {
    setOnline(false);
    await capturePhoto("job-1", fakeBlob(), null);
    await drainPhotos();
    expect(postMock).not.toHaveBeenCalled();
  });
});

describe("failure handling", () => {
  it("stops retrying a photo the server refuses (4xx)", async () => {
    // e.g. a 400 on a bad kind, or a 404 from the ownership gate. Retrying
    // forever would spin the queue on something that can never succeed.
    postMock.mockRejectedValue(httpError(400));
    const r = await capturePhoto("job-1", fakeBlob(), "bogus");

    const row = await db.photos.get(r.id);
    expect(row.status).toBe("failed");
    // The blob SURVIVES. The tech was told it was saved and has already driven
    // away from the door — a rejected upload is a reason to tell someone, not
    // to destroy the only copy.
    expect(row.blob).toBeTruthy();

    await drainPhotos();
    expect(postMock).toHaveBeenCalledTimes(1); // not retried
  });

  it("fails loud on a broken client instead of pretending to wait for signal", async () => {
    // The trap this exists to close: `createApiClient()` throwing (a missing
    // dependency, a coding mistake) carries no HTTP status. Filing that under
    // "transient, we'll retry" shows the tech "uploads when you have signal"
    // on full bars, forever — the exact silent shape of 0 photos across 205
    // jobs, one layer up.
    postMock.mockRejectedValue(new Error("getActivePinia() was called but there was no active Pinia"));
    const r = await capturePhoto("job-1", fakeBlob(), null);

    const row = await db.photos.get(r.id);
    expect(row.status).toBe("failed");
    expect(row.blob).toBeTruthy();
  });

  it("treats 401 as transient, not as a verdict on the photo", async () => {
    // The bug this exists to prevent: a photo captured at 9am and drained at
    // 5pm meets an expired token. Lumping 401 in with "4xx = a real answer"
    // marks it failed and stops — silently losing the tech's photo to nothing
    // more than token expiry. useApi refreshes and retries; if that fails, the
    // photo must WAIT, not die.
    postMock.mockRejectedValue(httpError(401));
    const r = await capturePhoto("job-1", fakeBlob(), null);

    const row = await db.photos.get(r.id);
    expect(row.status).toBe("pending");
    expect(row.blob).toBeTruthy();

    postMock.mockResolvedValue({ ok: true });
    await drainPhotos();
    expect((await db.photos.get(r.id)).status).toBe("synced");
  });

  it("does not upload the same photo twice when drains overlap", async () => {
    // `online` and `visibilitychange` routinely fire together on reconnect, so
    // two drains genuinely race over the same row.
    setOnline(false);
    await capturePhoto("job-1", fakeBlob(), null);
    setOnline(true);
    // Slow upload: both drains are in flight at once.
    postMock.mockImplementation(
      () => new Promise((res) => setTimeout(() => res({ ok: true }), 0)),
    );

    await Promise.all([drainPhotos(), drainPhotos()]);

    expect(postMock).toHaveBeenCalledTimes(1);
  });

  it("retries after a server error (5xx)", async () => {
    postMock.mockRejectedValue(httpError(503));
    const r = await capturePhoto("job-1", fakeBlob(), null);
    expect((await db.photos.get(r.id)).status).toBe("pending");

    postMock.mockResolvedValue({ ok: true });
    await drainPhotos();
    expect((await db.photos.get(r.id)).status).toBe("synced");
  });
});

describe("pending count", () => {
  it("counts what's still waiting for signal", async () => {
    const { pendingPhotos } = usePhotoQueue();
    setOnline(false);
    await capturePhoto("job-1", fakeBlob(), null);
    await capturePhoto("job-1", fakeBlob(), null);
    expect(pendingPhotos.value).toBe(2);

    setOnline(true);
    await drainPhotos();
    expect(pendingPhotos.value).toBe(0);
  });
});
