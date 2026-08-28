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
 *  5. [superseded 2026-08-25] This file used to pin "Time is READ-ONLY",
 *     because a Stop button would have closed the timer and switched off the
 *     guard #154 shipped (`_open_job_timers` filters clock_out IS NULL), so
 *     closeout would synthesize a SECOND row and an attested 2h job would bill
 *     5h. That was true of the endpoint as it stood: it banked wall-clock
 *     elapsed into `duration_minutes`, which IS payroll hours. Both clocks are
 *     now on the screen (Doug, 2026-07-17) and the endpoint was fixed first —
 *     a manual stop banks ZERO payable minutes, and closeout restates the
 *     tech's own stopped row rather than stacking a synthetic beside it. The
 *     money side is pinned in tests/test_closeout_labor_trail.py; what this
 *     file pins is the UI contract: the control reflects state, it is hidden
 *     on a read-only grant, and it never tells the tech the span is pay.
 */
// Must precede any import that pulls in Dexie (lib/offlineDb) — jsdom has no
// IndexedDB, so without the polyfill every queued action's background write
// rejects with MissingAPIError as an unhandled rejection (24 per run).
import "fake-indexeddb/auto";
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
const postMock = vi.fn();
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: vi.fn(), postQueued: postQueuedMock }),
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
    clocks: clocksPayload(),
  };
}

/** Server shape of the two clocks. Defaults to "nothing running". */
function clocksPayload(job = {}, day = {}) {
  return {
    day: { running: false, since: null, elapsed_minutes: 0, pays: true, ...day },
    job: { running: false, entry_id: null, since: null, elapsed_minutes: 0, pays: false, ...job },
  };
}

async function mountWith(overrides = {}, topLevel = {}) {
  const { default: View } = await import("../MobileJobDetailView.vue");
  getMock.mockImplementation(async () => ({ ...jobPayload(overrides), ...topLevel }));
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  postMock.mockResolvedValue({ ok: true });
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

  it("hides Bill / collect when the office marked the job not billable (055)", async () => {
    // The RFB dismiss verb: the office said this job never gets an invoice.
    // Ships as its own key — folding it into `billed` would make that field
    // lie (a not-billable job has NO invoice).
    const w = await mountWith({ dispatch_status: "done", billed: false, not_billable: true });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(false);
  });

  it("an old server that doesn't send not_billable must not hide Bill", async () => {
    const w = await mountWith({ dispatch_status: "done", billed: false, not_billable: undefined });
    expect(w.find('[data-testid="mjd-bill"]').exists()).toBe(true);
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
    expect(capturePhotoMock).toHaveBeenCalledWith("job-123", expect.any(File));
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

describe("view-only grants (2026-08-17 field report)", () => {
  // read_only/access_grant are TOP-LEVEL response fields, sibling to `job`.
  async function mountPayloadExtra(extra) {
    const { default: View } = await import("../MobileJobDetailView.vue");
    getMock.mockImplementation(async () => ({ ...jobPayload(), ...extra }));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    return w;
  }

  it("creator grant hides actions and says dispatch still owns the handoff", async () => {
    // The original failure: a tech opened the job he'd just created and got
    // a full action bar whose every tap answered "Could not save — job not
    // found". Creator grant is now honestly read-only with a reason.
    const w = await mountPayloadExtra({ read_only: true, access_grant: "creator" });
    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(false);
    const banner = w.find('[data-testid="mjd-readonly-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain("You created this job");
    expect(banner.text()).toContain("isn't assigned to you");
  });

  it("company grant shows the generic view-only banner", async () => {
    const w = await mountPayloadExtra({ read_only: true, access_grant: "company" });
    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(false);
    const banner = w.find('[data-testid="mjd-readonly-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain("View only");
  });

  it("an assigned (writable) job shows no banner and keeps its actions", async () => {
    const w = await mountWith({ dispatch_status: "assigned" });
    expect(w.find('[data-testid="mjd-readonly-banner"]').exists()).toBe(false);
    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(true);
  });

  // 2026-08-28 field report: the tech who created the job on site, with
  // "Assign to me" switched off, could not add photos — the Add-photo label
  // sat behind the same v-if as the clocks. The server now says which
  // writes a grant keeps; the screen follows it, not the read_only bit.
  it("creator grant keeps Add photo when the server says can_add_photos", async () => {
    const w = await mountPayloadExtra({ read_only: true, access_grant: "creator", can_add_photos: true });
    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-photo-add"]').exists()).toBe(true);
  });

  it("company grant hides Add photo when the server withholds it", async () => {
    const w = await mountPayloadExtra({ read_only: true, access_grant: "company", can_add_photos: false });
    expect(w.find('[data-testid="mjd-photo-add"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-claim"]').exists()).toBe(false);
  });

  it("creator grant offers Assign to me; the tap claims and reloads the writable screen", async () => {
    const w = await mountPayloadExtra({ read_only: true, access_grant: "creator", can_add_photos: true });
    const btn = w.find('[data-testid="mjd-claim"]');
    expect(btn.exists()).toBe(true);
    expect(btn.text()).toContain("Assign to me");

    postMock.mockResolvedValueOnce({ ok: true, assigned_to: "tech-1", access_grant: "assigned" });
    // After the claim the server answers with the assigned, writable shape.
    getMock.mockImplementation(async () => ({ ...jobPayload({ dispatch_status: "assigned" }), read_only: false, access_grant: "assigned" }));
    await btn.trigger("click");
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith("/api/mobile/jobs/job-123/claim");
    expect(w.find('[data-testid="mjd-readonly-banner"]').exists()).toBe(false);
    expect(w.find('[data-testid="mobile-job-detail-actions"]').exists()).toBe(true);
  });

  it("a 409 on claim says dispatch already assigned it, and the screen stays read-only", async () => {
    const w = await mountPayloadExtra({ read_only: true, access_grant: "creator", can_add_photos: true });
    postMock.mockRejectedValueOnce(Object.assign(new Error("Conflict"), { status: 409 }));
    await w.find('[data-testid="mjd-claim"]').trigger("click");
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "error",
      detail: expect.stringContaining("already assigned"),
    }));
    expect(w.find('[data-testid="mjd-readonly-banner"]').exists()).toBe(true);
  });
});

describe("both clocks — the tech must never guess which one pays", () => {
  it("labels the day clock as paid time and the job clock as not", async () => {
    const w = await mountWith({ dispatch_status: "on_site" });
    const day = w.find('[data-testid="mjd-day-clock"]');
    const job = w.find('[data-testid="mjd-job-clock"]');
    expect(day.exists()).toBe(true);
    expect(job.exists()).toBe(true);
    expect(day.text()).toContain("your paid time");
    // The one sentence that stops this feature doing harm.
    expect(job.text()).toContain("doesn't pay you");
  });

  it("renders Stop — not Start — when the timer is already running", async () => {
    // Arriving starts this timer server-side, so a Start button here would
    // 409 or double-start. The control reflects state.
    const w = await mountWith(
      { dispatch_status: "on_site" },
      { clocks: clocksPayload({ running: true, elapsed_minutes: 95, entry_id: "te-1" }) },
    );
    expect(w.find('[data-testid="mjd-job-clock-stop"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-job-clock-start"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-job-clock"]').text()).toContain("1h 35m");
  });

  it("renders Start when nothing is running on site", async () => {
    const w = await mountWith({ dispatch_status: "on_site" });
    expect(w.find('[data-testid="mjd-job-clock-start"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-job-clock-stop"]').exists()).toBe(false);
  });

  it("offers no job-clock control before the tech is under way", async () => {
    const w = await mountWith({ dispatch_status: "assigned" });
    expect(w.find('[data-testid="mjd-job-clock-start"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-job-clock-stop"]').exists()).toBe(false);
  });

  it("hides the control on a read-only grant instead of shipping a button that 404s", async () => {
    const w = await mountWith(
      { dispatch_status: "on_site" },
      {
        read_only: true,
        access_grant: "company",
        clocks: clocksPayload({ running: true, elapsed_minutes: 30 }),
      },
    );
    expect(w.find('[data-testid="mjd-job-clock"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-job-clock-stop"]').exists()).toBe(false);
  });

  it("stopping says what was recorded and that it is not pay", async () => {
    postMock.mockResolvedValue({ ok: true, elapsed_minutes: 192, recorded_minutes: 0, payable: false });
    const w = await mountWith(
      { dispatch_status: "on_site" },
      { clocks: clocksPayload({ running: true, elapsed_minutes: 192 }) },
    );
    await w.find('[data-testid="mjd-job-clock-stop"]').trigger("click");
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith("/api/mobile/jobs/job-123/clock-out");
    const toastArgs = toastAdd.mock.calls.at(-1)[0];
    expect(toastArgs.summary).toContain("3h 12m");
    // Never let the elapsed span read as hours earned.
    expect(toastArgs.detail).toContain("close-out");
  });

  it("treats a 409 on start as state drift, not an error the tech caused", async () => {
    // Arrival already opened the timer; the tech taps Start on a stale screen.
    postMock.mockRejectedValue(Object.assign(new Error("conflict"), { status: 409 }));
    const w = await mountWith({ dispatch_status: "on_site" });
    await w.find('[data-testid="mjd-job-clock-start"]').trigger("click");
    await flushPromises();

    const errors = toastAdd.mock.calls.filter((c) => c[0].severity === "error");
    expect(errors).toHaveLength(0);
  });

  it("survives a payload with no clocks key rather than throwing on mount", async () => {
    // An older server, or a cached response from before this shipped.
    const w = await mountWith({ dispatch_status: "on_site" }, { clocks: undefined });
    expect(w.find('[data-testid="mjd-job-clock"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-job-clock-stop"]').exists()).toBe(false);
  });

  it("renders the Time card for a closeout with no arrival stamp", async () => {
    // A job entered after the fact, or a dispatcher closing for a tech. The
    // attested hours are the point of the card, not the arrival — pinned here
    // by mounting rather than by a regex over the template source.
    const w = await mountWith({
      dispatch_status: "done",
      arrived_at: null,
      closeout: { hours_worked: 1.5, no_parts_used: true, parts_count: 0, notes: "spring swapped" },
    });
    expect(w.find('[data-testid="mobile-closeout-summary"]').exists()).toBe(true);
    expect(w.find('[data-testid="mobile-closeout-summary"]').text()).toContain("1.50");
    expect(w.find('[data-testid="mobile-closeout-notes"]').text()).toContain("spring swapped");
  });
});
