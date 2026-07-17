/**
 * MobileJobDetailView — the tech can actually work the job.
 *
 * Doug: "Michael can click on a job card but that is it… you should be able to
 * clock in and out and add parts etc. A tech's workflow needs to be able to
 * complete and send it to billing or be able to show the bill to the customer
 * to collect payment."
 *
 * This view was created 2026-07-16 (PR #153) purely so tapping a card did
 * something — Back and Retry were its only buttons. It is the path the tech
 * takes, and the ONLY path to a job not scheduled today. Live counts say the
 * workflow was unreachable: 184 completed jobs, 4 closeouts, 0 photos.
 *
 * Pinned here:
 *  1. Status actions appear only for the state they belong to.
 *  2. "Bill / collect" is NOT offered on a stale or already-billed job — this
 *     screen opens ANY job, so Today's date-blind status guard would offer to
 *     re-invoice a job that was paid months ago.
 *  3. Actions are offline-queued (the tech is in driveways and dead zones) and
 *     a queued write says so rather than claiming success.
 *  4. State is re-read from the server after an action, never guessed — Today
 *     flips dispatch_status before checking the result and never rolls it back,
 *     so its card can read "en route" while an error toast fires.
 *  5. Time is READ-ONLY. Arrive starts the job clock, Complete ends it (PR
 *     #154). A Stop button would close the timer and switch off the very guard
 *     #154 shipped (`_open_job_timers` filters clock_out IS NULL), so closeout
 *     would then synthesize a SECOND row: an attested 2h job bills 5h.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { ref } from "vue";

const getMock = vi.fn();
const postQueuedMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-123" }, query: {}, path: "/mobile/jobs/job-123" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: postQueuedMock }),
}));

const capturePhotoMock = vi.fn();
// A real ref, not { value } — the template relies on Vue auto-unwrapping it.
const pendingPhotosRef = ref(0);
vi.mock("../../composables/usePhotoQueue", () => ({
  usePhotoQueue: () => ({
    pendingPhotos: pendingPhotosRef,
    uploadingPhotos: ref(false),
    capturePhoto: capturePhotoMock,
    drainPhotos: vi.fn(),
  }),
}));

const stubs = {
  Button: {
    props: ["label", "icon", "loading", "severity", "text", "rounded", "outlined"],
    template: '<button v-bind="$attrs" @click="$emit(\'click\')">{{ label }}</button>',
  },
  MobileJobCloseoutDialog: { props: ["visible", "jobId"], template: "<div />" },
  MobileInvoiceDialog: { props: ["visible", "job"], template: "<div />" },
};

function jobPayload(overrides = {}) {
  return {
    job: {
      id: "job-123",
      title: "Spring replacement",
      dispatch_status: "assigned",
      navigation_link: "https://maps.google.com/?q=123+Main+St",
      customer: { id: "c1", name: "Acme", phone: "5551234567", address: "123 Main St" },
      ...overrides,
    },
    notes: [],
    photos: [],
  };
}

// The view fetches the job AND the tech-mobile settings on mount; route by URL
// so a settings call can't consume the job's mock (the old spec used
// mockResolvedValueOnce and any extra GET would have broken it).
function routeGet(overrides = {}, settings = {}) {
  getMock.mockImplementation(async (url) => {
    if (String(url).includes("tech-mobile-settings")) return { settings };
    return jobPayload(overrides);
  });
}

async function mountWith(overrides = {}, settings = {}) {
  const { default: View } = await import("../MobileJobDetailView.vue");
  routeGet(overrides, settings);
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  postQueuedMock.mockResolvedValue({ ok: true });
  capturePhotoMock.mockResolvedValue({ queued: false, id: "p1" });
  pendingPhotosRef.value = 0;
});

describe("status actions", () => {
  it("offers On my way on a fresh job", async () => {
    const w = await mountWith({ dispatch_status: "assigned" });
    expect(w.find('[data-testid="mjd-en-route"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-arrived"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-complete"]').exists()).toBe(false);
  });

  it("offers I'm here once en route", async () => {
    const w = await mountWith({ dispatch_status: "en_route" });
    expect(w.find('[data-testid="mjd-arrived"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-en-route"]').exists()).toBe(false);
  });

  it("offers Complete once on site", async () => {
    const w = await mountWith({ dispatch_status: "on_site" });
    expect(w.find('[data-testid="mjd-complete"]').exists()).toBe(true);
  });

  it("navigates using the server-built link", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => {});
    const w = await mountWith();
    await w.find('[data-testid="mjd-navigate"]').trigger("click");
    expect(open).toHaveBeenCalledWith("https://maps.google.com/?q=123+Main+St", "_blank", "noopener");
    open.mockRestore();
  });
});

describe("billing guard — this screen opens ANY job, not just today's", () => {
  it("offers Bill / collect on a finished job that was never invoiced", async () => {
    const w = await mountWith({ dispatch_status: "done", billed: false });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(true);
  });

  it("does NOT offer to re-bill a job that already reached an invoice", async () => {
    // Today's guard is dispatch_status-only — safe there because Today is only
    // today. Here it would invite re-invoicing a job that was paid in April.
    const w = await mountWith({ dispatch_status: "done", billed: true });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(false);
  });

  it("still offers to bill an OLD unbilled job — that's the backlog, not a mistake", async () => {
    // Age is not the hazard; a second invoice is. An unbilled April job is
    // exactly the work the office is chasing.
    const w = await mountWith({
      dispatch_status: "done",
      completed_at: "2026-04-27T14:00:00+00:00",
      billed: false,
    });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(true);
  });

  it("does not offer Bill / collect on a job still in progress", async () => {
    const w = await mountWith({ dispatch_status: "on_site", billed: false });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(false);
  });

  it("hides Bill / collect when the server did not say whether it's billed", async () => {
    // Absent must not read as "not billed" — that is exactly how the dead
    // billing_status column made every reader count paid jobs as unbilled.
    // Unknown fails safe: a second invoice is the mistake that costs money.
    const w = await mountWith({ dispatch_status: "done", billed: undefined });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(false);
  });
});

describe("offline behaviour", () => {
  it("queues En route rather than posting it", async () => {
    const w = await mountWith({ dispatch_status: "assigned" });
    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();
    expect(postQueuedMock).toHaveBeenCalledWith(
      "/api/mobile/jobs/job-123/en-route",
      {},
      expect.objectContaining({ actionType: "job.en_route", resourceId: "job-123" }),
    );
  });

  it("tells the tech when a write was only saved offline", async () => {
    postQueuedMock.mockResolvedValue({ queued: true });
    const w = await mountWith({ dispatch_status: "assigned" });
    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: "warn", summary: "Saved offline" }),
    );
  });

  it("re-reads state from the server instead of guessing it", async () => {
    const w = await mountWith({ dispatch_status: "assigned" });
    getMock.mockClear();
    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith("/api/mobile/job/job-123");
  });

  it("keeps the job on screen when the post-action refetch fails", async () => {
    // The dead-zone case, and the whole point of queueing. The write lands
    // locally, then the refetch has no network. If that refetch is allowed to
    // set the error state, `error` out-ranks `job` in the template and the tech
    // is told "Saved offline" and then watches the job vanish — the write
    // succeeded and the screen broke anyway.
    const w = await mountWith({ dispatch_status: "assigned" });
    postQueuedMock.mockResolvedValue({ queued: true });
    getMock.mockRejectedValue(new Error("Failed to fetch"));

    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();

    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(true);
    expect(w.text()).not.toContain("Failed to fetch");
    expect(w.find('[data-testid="mobile-job-detail-customer"]').text()).toBe("Acme");
  });

  it("still surfaces a real load failure on first paint", async () => {
    // The guard above must not swallow the case it was never about: opening a
    // job that genuinely will not load.
    const { default: View } = await import("../MobileJobDetailView.vue");
    getMock.mockRejectedValue(Object.assign(new Error("nope"), { status: 404 }));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    expect(w.text()).toContain("Job not found");
  });

  it("does not claim success when the write fails", async () => {
    postQueuedMock.mockRejectedValue(new Error("boom"));
    const w = await mountWith({ dispatch_status: "assigned" });
    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: "error" }),
    );
  });
});

describe("photo capture", () => {
  function pick(w, files) {
    const input = w.find('[data-testid="mjd-photo-add"] input[type="file"]');
    Object.defineProperty(input.element, "files", { value: files, configurable: true });
    return input.trigger("change");
  }
  const file = () => new File(["x"], "door.jpg", { type: "image/jpeg" });

  it("offers a camera control on a job with no photos", async () => {
    const w = await mountWith();
    const input = w.find('[data-testid="mjd-photo-add"] input[type="file"]');
    expect(input.exists()).toBe(true);
    expect(input.attributes("accept")).toBe("image/*");
    // NO `capture` attribute, deliberately: Android honours it by forcing a
    // single shot straight to the lens, which kills `multiple` AND locks the
    // tech out of the gallery — so a photo taken before the app was open could
    // never be attached. Bare accept="image/*" offers Camera or Files.
    expect(input.attributes("capture")).toBeUndefined();
    expect(input.attributes("multiple")).toBeDefined();
  });

  it("stores the photo through the offline queue", async () => {
    const w = await mountWith();
    await pick(w, [file()]);
    await flushPromises();
    expect(capturePhotoMock).toHaveBeenCalledWith("job-123", expect.any(File), null);
  });

  it("says 'saved on your phone' when there's no signal — never 'uploaded'", async () => {
    capturePhotoMock.mockResolvedValue({ queued: true, id: "p1" });
    const w = await mountWith();
    await pick(w, [file()]);
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: "warn", summary: "Saved on your phone" }),
    );
  });

  it("refetches after upload — the 201 carries no url to render", async () => {
    const w = await mountWith();
    getMock.mockClear();
    await pick(w, [file()]);
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith("/api/mobile/job/job-123");
  });

  it("shows how many photos are still waiting for signal", async () => {
    pendingPhotosRef.value = 3;
    const w = await mountWith();
    expect(w.find('[data-testid="mjd-photo-pending"]').text()).toContain("3 waiting for signal");
  });

  it("hides the slot picker when the tenant leaves tagging optional", async () => {
    const w = await mountWith({}, { "tech_mobile.photo_slot_tagging": "optional" });
    expect(w.find('[data-testid="mjd-photo-kinds"]').exists()).toBe(false);
  });

  it("requires a slot when the tenant demands one, rather than eating the 400", async () => {
    const w = await mountWith({}, { "tech_mobile.photo_slot_tagging": "required" });
    expect(w.find('[data-testid="mjd-photo-kinds"]').exists()).toBe(true);

    await pick(w, [file()]);
    await flushPromises();
    expect(capturePhotoMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: "warn" }),
    );
  });

  it("sends the chosen slot", async () => {
    const w = await mountWith({}, { "tech_mobile.photo_slot_tagging": "required" });
    await w.find('[data-testid="mjd-photo-kind-after"]').trigger("click");
    await pick(w, [file()]);
    await flushPromises();
    expect(capturePhotoMock).toHaveBeenCalledWith("job-123", expect.any(File), "after");
  });
});

describe("time is shown, never edited", () => {
  it("shows the job clock is running once arrived", async () => {
    const w = await mountWith({
      dispatch_status: "on_site",
      arrived_at: "2026-07-17T09:14:00+00:00",
    });
    const timer = w.find('[data-testid="mobile-job-detail-timer"]');
    expect(timer.exists()).toBe(true);
    expect(timer.text()).toMatch(/Tracking since you arrived/);
  });

  it("offers NO stop/start control — that would switch off #154's guard", async () => {
    const w = await mountWith({
      dispatch_status: "on_site",
      arrived_at: "2026-07-17T09:14:00+00:00",
    });
    const labels = w.findAll("button").map((b) => b.text().toLowerCase());
    expect(labels.some((l) => l.includes("stop"))).toBe(false);
    expect(labels.some((l) => l.includes("clock"))).toBe(false);
  });

  it("says plainly that the job clock is not what pays the tech", async () => {
    const w = await mountWith({
      dispatch_status: "on_site",
      arrived_at: "2026-07-17T09:14:00+00:00",
    });
    expect(w.text()).toContain("paid hours come from the day clock");
  });

  it("never implies a duration from arrival and close-out stamps", async () => {
    // Caught on a real phone: a job arrived at in May and closed out in July
    // rendered "Tracked May 19 → Jul 16" — two months, for work the tech
    // attested at 1.5 hours. The stamps don't bound the work, so they must not
    // be joined with an arrow and called tracked time.
    const w = await mountWith({
      dispatch_status: "done",
      billed: false,
      arrived_at: "2026-05-19T20:19:00+00:00",
      completed_at: "2026-07-16T20:16:00+00:00",
    });
    const timer = w.find('[data-testid="mobile-job-detail-timer"]');
    expect(timer.exists()).toBe(true);
    expect(timer.text()).not.toMatch(/Tracked.*→/);
    expect(timer.text()).toContain("Arrived");
    expect(timer.text()).toContain("closed out");
  });
});
