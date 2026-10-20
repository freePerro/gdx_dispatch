/**
 * MobileJobDetailView — the stage a finished job is in (GDXA-1942).
 *
 * The header read dispatch_status only, and an undated job always showed
 * "No date — do when in the area". A completed or cancelled job is over: the
 * nudge goes, and the pill names the stage (a cancel never touches
 * dispatch_status). /api/mobile/job/{id} sends lifecycle_stage as the raw
 * enum; the labels /api/jobs sends ("Complete", "Cancelled") read the same.
 */
import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises, RouterLinkStub } from "@vue/test-utils";
import { ref } from "vue";

const getMock = vi.fn();
const postQueuedMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-123" }, query: {}, path: "/mobile/jobs/job-123" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
const patchQueuedMock = vi.fn();
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: postQueuedMock, patchQueued: patchQueuedMock }),
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
  // The customer name on these screens is a router-link to /mobile/customers/:id.
  // These specs mock vue-router wholesale, so RouterLink is not registered.
  RouterLink: RouterLinkStub,
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
      title: "Install",
      dispatch_status: "assigned",
      scheduled_at: null,
      customer: { id: "c1", name: "Acme", phone: "5551234567", address: "100 Billing Rd" },
      ...overrides,
    },
    notes: [],
    photos: [],
  };
}

async function mountWith(overrides = {}) {
  const { default: View } = await import("../MobileJobDetailView.vue");
  getMock.mockImplementation(async () => jobPayload(overrides));
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("finished job header", () => {
  it.each([
    ["completed", "done", "Completed"],
    ["Complete", "done", "Completed"],
    ["cancelled", "assigned", "Cancelled"],
    ["Canceled", "en_route", "Cancelled"],
  ])("stage %s (dispatch %s): no 'do when in the area', pill reads %s", async (stage, dispatch, label) => {
    const w = await mountWith({ lifecycle_stage: stage, dispatch_status: dispatch });
    expect(w.find('[data-testid="mobile-job-detail-no-date"]').exists()).toBe(false);
    expect(w.text()).not.toContain("do when in the area");
    expect(w.find('[data-testid="mobile-job-detail-status"]').text()).toBe(label);
  });

  it.each(["in_progress", "In Progress", "scheduled", "service_call", undefined])(
    "open stage %s keeps the undated nudge and the dispatch pill",
    async (stage) => {
      const w = await mountWith({ lifecycle_stage: stage, dispatch_status: "en_route" });
      expect(w.find('[data-testid="mobile-job-detail-no-date"]').text()).toContain("do when in the area");
      expect(w.find('[data-testid="mobile-job-detail-status"]').text()).toBe("En route");
    },
  );

  it("a dated finished job still shows its date", async () => {
    const w = await mountWith({ lifecycle_stage: "completed", dispatch_status: "done", scheduled_at: "2026-10-01T15:00:00Z" });
    expect(w.find(".detail-meta").exists()).toBe(true);
    expect(w.find('[data-testid="mobile-job-detail-no-date"]').exists()).toBe(false);
  });
});
