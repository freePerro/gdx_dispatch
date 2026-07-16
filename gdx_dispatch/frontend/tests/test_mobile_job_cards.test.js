// 2026-07-16 tech-mobile job-access fix — frontend coverage.
//
// 1. MobileJobsView cards are LINKS into the job detail view (they used to
//    be display-only: a tech had no path from the list to any job info).
// 2. MobileJobDetailView renders the /api/mobile/job/{id} payload
//    (customer contact as tap-to-call / tap-to-navigate links) and shows
//    a not-found state on 404.
// 3. MobileTodayView requests the tech's LOCAL day (date+tz params) and
//    renders the "when you're in the area" section from area_jobs.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();

const routerPush = vi.fn();
const routerBack = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: routerPush, back: routerBack, replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-123" }, query: {}, path: "/mobile/jobs/job-123" }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock }),
}));

vi.mock("../src/composables/usePermission", () => ({
  usePermission: () => ({ hasPermission: () => true }),
}));

// MobileTodayView pulls in heavy composables — stub them all.
vi.mock("../src/composables/useGpsBreadcrumb", () => ({
  useGpsBreadcrumb: () => ({ start: vi.fn(), stop: vi.fn() }),
}));
vi.mock("../src/composables/useOfflineSync", () => ({
  useOfflineSync: () => ({
    isOnline: { value: true }, pendingCount: { value: 0 },
    syncing: { value: false }, syncNow: vi.fn(),
  }),
  queueAction: vi.fn(),
}));
vi.mock("../src/composables/useMobileTour", () => ({
  useMobileTour: () => ({ start: vi.fn() }),
}));
vi.mock("../src/composables/usePushSubscription", () => ({
  isPushSupported: () => false,
  getCurrentPermission: () => "denied",
  subscribeToPush: vi.fn(),
}));
vi.mock("../src/composables/usePartsSeenCutoff", () => ({
  markJobSeen: vi.fn(),
  countUnseenForJob: vi.fn().mockResolvedValue(0),
}));

const RouterLinkStub = {
  props: ["to"],
  template: '<a :href="typeof to === \'string\' ? to : \'#\'" :data-to="typeof to === \'string\' ? to : \'\'"><slot /></a>',
};

const stubs = {
  RouterLink: RouterLinkStub,
  "router-link": RouterLinkStub,
  Button: {
    props: ["label"],
    emits: ["click"],
    template: "<button @click=\"$emit('click')\">{{ label }}<slot /></button>",
  },
  SelectButton: { template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Message: { template: "<div><slot /></div>" },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  MobileJobNewDialog: { template: "<div />" },
  MobileQuoteBuilderDialog: { template: "<div />" },
  MobileCustomerQuoteDialog: { template: "<div />" },
  MobileInvoiceDialog: { template: "<div />" },
  MobileChatDialog: { template: "<div />" },
  MobileChangeOrderDialog: { template: "<div />" },
  MobileJobCloseoutDialog: { template: "<div />" },
  InputText: { template: "<input />" },
  InputNumber: { template: "<input />" },
  Textarea: { template: "<textarea />" },
  AutoComplete: { template: "<input />" },
  Select: { template: "<select />" },
};

beforeEach(() => {
  setActivePinia(createPinia());
  getMock.mockReset();
  routerPush.mockReset();
  routerBack.mockReset();
});

describe("MobileJobsView card links", () => {
  it("renders each job card as a link to its detail route", async () => {
    const { default: MobileJobsView } = await import("../src/views/MobileJobsView.vue");
    getMock.mockResolvedValueOnce({
      count: 1,
      jobs: [{
        id: "job-1", title: "Spring replacement", dispatch_status: "assigned",
        customer_name: "Acme", customer_address: "123 Main St",
        scheduled_at: "2026-07-16T15:00:00+00:00",
      }],
    });
    const w = mount(MobileJobsView, { global: { stubs } });
    await flushPromises();
    const card = w.find('[data-testid="mobile-job-card-job-1"]');
    expect(card.exists()).toBe(true);
    expect(card.attributes("data-to")).toBe("/mobile/jobs/job-1");
    expect(card.text()).toContain("Acme");
    expect(card.text()).toContain("123 Main St");
  });
});

describe("MobileJobDetailView", () => {
  it("renders customer contact as tel/navigation links", async () => {
    const { default: MobileJobDetailView } = await import("../src/views/MobileJobDetailView.vue");
    getMock.mockResolvedValueOnce({
      job: {
        id: "job-123", title: "Opener install", description: "8ft door",
        dispatch_status: "assigned", scheduled_at: "2026-07-16T15:00:00+00:00",
      },
      customer: {
        id: "c1", name: "Acme", phone: "5551234567",
        email: "a@example.com", address: "123 Main St",
      },
      notes: [{ id: "n1", note: "Gate code 4321", created_at: "2026-07-15T12:00:00+00:00" }],
      photos: [],
    });
    const w = mount(MobileJobDetailView, { global: { stubs } });
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith("/api/mobile/job/job-123");
    expect(w.find('[data-testid="mobile-job-detail-customer"]').text()).toBe("Acme");
    expect(w.find('[data-testid="mobile-job-detail-phone"]').attributes("href")).toBe("tel:5551234567");
    const nav = w.find('[data-testid="mobile-job-detail-address"]').attributes("href");
    expect(nav).toContain("google.com/maps");
    expect(nav).toContain(encodeURIComponent("123 Main St"));
    expect(w.text()).toContain("Gate code 4321");
  });

  it("shows a not-found state on 404 (ownership gate)", async () => {
    const { default: MobileJobDetailView } = await import("../src/views/MobileJobDetailView.vue");
    const err = new Error("job not found");
    err.status = 404;
    getMock.mockRejectedValueOnce(err);
    const w = mount(MobileJobDetailView, { global: { stubs } });
    await flushPromises();
    expect(w.text()).toContain("Job not found");
  });
});

describe("MobileTodayView local day + area jobs", () => {
  it("requests the local date + tz and renders the area section", async () => {
    const { default: MobileTodayView } = await import("../src/views/MobileTodayView.vue");
    getMock.mockImplementation((url) => {
      if (url.startsWith("/api/mobile/today")) {
        return Promise.resolve({
          date: "2026-07-16", tech_id: "tech-1", count: 0, jobs: [],
          area_jobs: [{
            id: "job-area-1", title: "Adjust track",
            customer: { name: "Acme", address: "123 Main St" },
          }],
          area_count: 1,
        });
      }
      return Promise.resolve({});
    });
    const w = mount(MobileTodayView, { global: { stubs } });
    await flushPromises();

    const todayCall = getMock.mock.calls.find(([u]) => u.startsWith("/api/mobile/today"));
    expect(todayCall).toBeTruthy();
    const qs = new URLSearchParams(todayCall[0].split("?")[1]);
    expect(qs.get("date")).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(qs.get("tz")).toBeTruthy();

    const section = w.find('[data-testid="mobile-area-jobs"]');
    expect(section.exists()).toBe(true);
    const card = w.find('[data-testid="mobile-area-job-job-area-1"]');
    expect(card.exists()).toBe(true);
    expect(card.attributes("data-to")).toBe("/mobile/jobs/job-area-1");
    expect(card.text()).toContain("Acme");
  });
});
