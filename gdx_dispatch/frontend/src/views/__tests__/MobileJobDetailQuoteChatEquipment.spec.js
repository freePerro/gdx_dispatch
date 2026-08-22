/**
 * MobileJobDetailView — PR A of the one-job-card plan.
 *
 * Build/Show quote, Change order, Chat and Install & equipment existed ONLY on
 * Today's route card. A tech who reached a job any other way — the Jobs list, a
 * notification, an unscheduled "in the area" job — could not build a quote,
 * raise a change order, or message dispatch about that job at all.
 *
 * Pinned here:
 *  1. The new actions carry the SAME dispatch_status guards as Today's card, so
 *     one job offers one set of actions whichever way the tech reached it.
 *  2. readOnly (company-wide browsing) hides them — the tech has no claim on the
 *     job and every call would 404.
 *  3. NOTHING new is fetched at mount. test_mobile_job_cards mocks api.get with
 *     mockResolvedValueOnce — exactly once — so an extra mount-time GET resolves
 *     undefined and throws (July plan, trap #3). This is asserted
 *     counterfactually: the quote and equipment endpoints must NOT be called
 *     until the tech taps.
 *  4. Job context (priority, return visit, customer alerts, customer warnings)
 *     renders — the detail screen used to show strictly less about a job than
 *     the route card did.
 */
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
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: postQueuedMock }),
}));
vi.mock("../../composables/usePhotoQueue", () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
  }),
}));

const stubs = {
  Button: {
    props: ["label", "icon", "loading", "severity", "text", "rounded", "outlined"],
    // emits MUST be declared: without it onClick stays in $attrs, a
    // `v-bind="$attrs"` stub binds it natively AND re-emits, and every tap
    // fires the parent handler twice. That is a stub artifact, not product
    // behaviour — but it silently doubles any call-count assertion.
    emits: ["click"],
    template: '<button @click="$emit(\'click\')">{{ label }}</button>',
  },
  Tag: { props: ["value", "severity"], template: "<span class='tag'>{{ value }}</span>" },
  MobileJobCloseoutDialog: { props: ["visible", "jobId"], template: "<div />" },
  MobileInvoiceDialog: { props: ["visible", "job"], template: "<div />" },
  MobileQuoteBuilderDialog: {
    props: ["visible", "job"],
    template: '<div data-testid="stub-quote-builder" v-if="visible" />',
  },
  MobileCustomerQuoteDialog: {
    props: ["visible", "quote"],
    template: '<div data-testid="stub-customer-quote" v-if="visible" />',
  },
  MobileChangeOrderDialog: {
    props: ["visible", "jobId", "jobTitle", "customerId", "customerName"],
    template: '<div data-testid="stub-change-order" v-if="visible" />',
  },
  MobileChatDialog: {
    props: ["visible", "job"],
    template: '<div data-testid="stub-chat" v-if="visible" />',
  },
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

/** Route GETs by URL so the quote/equipment calls are observable. */
function routeGets({ job = {}, quotes = [], equipment = [] } = {}) {
  getMock.mockImplementation(async (url) => {
    if (String(url).includes("/quote")) return { quotes };
    if (String(url).includes("/equipment")) return equipment;
    return jobPayload(job);
  });
}

async function mountWith(opts = {}) {
  const { default: View } = await import("../MobileJobDetailView.vue");
  routeGets(opts);
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

const urlsFetched = () => getMock.mock.calls.map((c) => String(c[0]));

beforeEach(() => {
  vi.clearAllMocks();
  postQueuedMock.mockResolvedValue({ ok: true });
});

describe("the new actions carry Today's guards", () => {
  it("does not offer a quote before the tech is en route", async () => {
    const w = await mountWith({ job: { dispatch_status: "assigned" } });
    expect(w.find('[data-testid="mjd-quote"]').exists()).toBe(false);
  });

  it.each(["en_route", "on_site", "done"])("offers a quote when %s", async (st) => {
    const w = await mountWith({ job: { dispatch_status: st } });
    expect(w.find('[data-testid="mjd-quote"]').exists()).toBe(true);
  });

  it.each(["assigned", "en_route"])("does not offer a change order when %s", async (st) => {
    const w = await mountWith({ job: { dispatch_status: st } });
    expect(w.find('[data-testid="mjd-change-order"]').exists()).toBe(false);
  });

  it.each(["on_site", "done"])("offers a change order when %s", async (st) => {
    const w = await mountWith({ job: { dispatch_status: st } });
    expect(w.find('[data-testid="mjd-change-order"]').exists()).toBe(true);
  });

  it("offers chat at any status — dispatch is reachable all day", async () => {
    const w = await mountWith({ job: { dispatch_status: "assigned" } });
    expect(w.find('[data-testid="mjd-chat"]').exists()).toBe(true);
  });
});

describe("view-only browsing hides every dispatch action", () => {
  it("hides quote, change order and chat when the job is not the tech's", async () => {
    const { default: View } = await import("../MobileJobDetailView.vue");
    getMock.mockImplementation(async () => ({
      ...jobPayload({ dispatch_status: "on_site" }),
      read_only: true,
    }));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="mjd-readonly-banner"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-quote"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-change-order"]').exists()).toBe(false);
    expect(w.find('[data-testid="mjd-chat"]').exists()).toBe(false);
  });
});

describe("nothing new is fetched until the tech asks (trap #3)", () => {
  it("does NOT call the quote or equipment endpoints at mount", async () => {
    await mountWith({ job: { dispatch_status: "on_site" } });
    expect(urlsFetched().some((u) => u.includes("/quote"))).toBe(false);
    expect(urlsFetched().some((u) => u.includes("/equipment"))).toBe(false);
  });

  it("fetches quotes only on the first tap, and not again on the second", async () => {
    const w = await mountWith({ job: { dispatch_status: "on_site" }, quotes: [] });
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    const first = urlsFetched().filter((u) => u.includes("/quote")).length;
    expect(first).toBe(1);
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(urlsFetched().filter((u) => u.includes("/quote")).length).toBe(1);
  });

  it("fetches equipment only when the section is expanded", async () => {
    const w = await mountWith({ job: { dispatch_status: "on_site" } });
    expect(urlsFetched().some((u) => u.includes("/equipment"))).toBe(false);
    await w.find('[data-testid="mjd-equipment-toggle"]').trigger("click");
    await flushPromises();
    expect(urlsFetched().some((u) => u.includes("/equipment"))).toBe(true);
  });
});

describe("the quote action does the right thing with what it finds", () => {
  it("opens the builder when the job has no quote yet", async () => {
    const w = await mountWith({ job: { dispatch_status: "on_site" }, quotes: [] });
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="stub-quote-builder"]').exists()).toBe(true);
    expect(w.find('[data-testid="stub-customer-quote"]').exists()).toBe(false);
  });

  it("presents the live quote to the customer when one exists", async () => {
    const w = await mountWith({
      job: { dispatch_status: "on_site" },
      quotes: [{ id: "q1", status: "sent" }],
    });
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="stub-customer-quote"]').exists()).toBe(true);
    expect(w.find('[data-testid="stub-quote-builder"]').exists()).toBe(false);
  });

  it("does not treat a declined quote as live — it offers to build a new one", async () => {
    const w = await mountWith({
      job: { dispatch_status: "on_site" },
      quotes: [{ id: "q1", status: "declined" }],
    });
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="stub-quote-builder"]').exists()).toBe(true);
  });

  it("retries on the next tap when the quote lookup failed", async () => {
    const { default: View } = await import("../MobileJobDetailView.vue");
    let quoteCalls = 0;
    getMock.mockImplementation(async (url) => {
      if (String(url).includes("/quote")) {
        quoteCalls += 1;
        throw new Error("offline");
      }
      return jobPayload({ dispatch_status: "on_site" });
    });
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(quoteCalls).toBe(1);
    expect(w.find('[data-testid="stub-quote-builder"]').exists()).toBe(false);
    // "no quote" and "couldn't ask" are different answers — the tech is stood
    // in front of the customer, so the next tap must ask again.
    await w.find('[data-testid="mjd-quote"]').trigger("click");
    await flushPromises();
    expect(quoteCalls).toBe(2);
  });
});

describe("offline: the tech in a dead zone still advances the job", () => {
  // The bug this pins: postQueued QUEUES the write and resolves {queued:true},
  // then the follow-up refresh() throws because there is no signal. The screen
  // used to let that throw reach the outer catch, so the tech saw "Saved
  // offline" AND "Could not save" and the button never changed. Today's card
  // flipped and worked. Same job, two answers, depending on which screen you
  // opened it from.
  it("flips to en route on a queued write even when the refetch fails", async () => {
    const { default: View } = await import("../MobileJobDetailView.vue");
    let loaded = false;
    getMock.mockImplementation(async () => {
      if (loaded) throw new Error("offline");   // every refetch fails
      loaded = true;
      return jobPayload({ dispatch_status: "assigned" });
    });
    postQueuedMock.mockResolvedValue({ queued: true });
    const w = mount(View, { global: { stubs } });
    await flushPromises();

    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();

    // The action the tech now expects to see is the NEXT one.
    expect(w.find('[data-testid="mjd-arrived"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-en-route"]').exists()).toBe(false);

    const summaries = toastAdd.mock.calls.map((c) => c[0].summary);
    expect(summaries).toContain("Saved offline");
    // The contradiction is the defect, not the offline-ness.
    expect(summaries).not.toContain("Could not save");
  });

  it("advances to on site when arrival is queued", async () => {
    const { default: View } = await import("../MobileJobDetailView.vue");
    let loaded = false;
    getMock.mockImplementation(async () => {
      if (loaded) throw new Error("offline");
      loaded = true;
      return jobPayload({ dispatch_status: "en_route" });
    });
    postQueuedMock.mockResolvedValue({ queued: true });
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    await w.find('[data-testid="mjd-arrived"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="mjd-complete"]').exists()).toBe(true);
  });

  it("does NOT advance when the server actually refuses", async () => {
    // The counterweight. A queued write is a recorded intent; a 4xx is a real
    // answer, and the card must not claim progress the server rejected.
    const { default: View } = await import("../MobileJobDetailView.vue");
    getMock.mockImplementation(async () => jobPayload({ dispatch_status: "assigned" }));
    postQueuedMock.mockRejectedValue(new Error("409 already en route"));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    await w.find('[data-testid="mjd-en-route"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="mjd-en-route"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-arrived"]').exists()).toBe(false);
    expect(toastAdd.mock.calls.map((c) => c[0].summary)).toContain("Could not save");
  });
});

describe("job context the route card always had and this screen never did", () => {
  it("shows priority, return visit and customer alerts", async () => {
    const w = await mountWith({
      job: {
        dispatch_status: "assigned",
        priority: "Emergency",
        is_return_visit: true,
        alerts: ["dog_warning", "gate_code"],
      },
    });
    const ctx = w.find('[data-testid="mjd-job-context"]');
    expect(ctx.exists()).toBe(true);
    expect(ctx.text()).toContain("Emergency");
    expect(w.find('[data-testid="mjd-return-visit"]').exists()).toBe(true);
    // Underscores are a storage detail, not something a tech should read.
    expect(ctx.text()).toContain("dog warning");
    expect(ctx.text()).toContain("gate code");
  });

  it("stays out of the way on an ordinary job", async () => {
    const w = await mountWith({
      job: { dispatch_status: "assigned", priority: "Normal", is_return_visit: false, alerts: [] },
    });
    expect(w.find('[data-testid="mjd-job-context"]').exists()).toBe(false);
  });

  it("surfaces the customer warning note", async () => {
    const w = await mountWith({
      job: {
        dispatch_status: "assigned",
        customer: { id: "c1", name: "Acme", notes: "Beware of dog" },
      },
    });
    expect(w.find('[data-testid="mjd-customer-notes"]').text()).toContain("Beware of dog");
  });
});
