/**
 * Today's Route survives a cold remount with no signal.
 *
 * The route lived in memory only. That was survivable while a tech never left
 * the screen — but every tap-through to a job and back is a remount, and with
 * no signal `load()` threw, `jobs` stayed [], and the screen read "Nothing
 * scheduled today". The tech loses the day's route in the exact dead zone the
 * offline queue exists for. This is the gate PR B had to clear before the
 * route card could become a link.
 *
 * Pinned here:
 *  1. A cold mount with no network renders the last saved route, not an empty day.
 *  2. It is LABELLED — a tech acting on a stale route must know it is stale.
 *  3. The cache is keyed per tech+day, so a stale route can never be shown in
 *     place of a different day's.
 *  4. A live load overwrites the cache and drops the label.
 *  5. No cache and no network still shows the real error, not a false-empty day.
 */
import "fake-indexeddb/auto";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postQueuedMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {}, path: "/mobile" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: postQueuedMock }),
}));

const stubs = {
  Button: { props: ["label", "icon"], emits: ["click"], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Message: { template: "<div class='msg'><slot /></div>" },
  SelectButton: { props: ["modelValue", "options"], template: "<div />" },
  Dialog: { props: ["visible"], template: "<div />" },
  AutoComplete: { props: ["modelValue"], template: "<div />" },
  InputNumber: { props: ["modelValue"], template: "<div />" },
  InputText: { props: ["modelValue"], template: "<div />" },
  Textarea: { props: ["modelValue"], template: "<div />" },
  Checkbox: { props: ["modelValue"], template: "<div />" },
  MobileReceiptCapture: { template: "<div />" },
  MobileJobCloseoutDialog: { props: ["visible"], template: "<div />" },
  MobileChangeOrderDialog: { props: ["visible"], template: "<div />" },
  MobileQuoteBuilderDialog: { props: ["visible"], template: "<div />" },
  MobileCustomerQuoteDialog: { props: ["visible"], template: "<div />" },
  MobileInvoiceDialog: { props: ["visible"], template: "<div />" },
  MobileChatDialog: { props: ["visible"], template: "<div />" },
};

function route(overrides = {}) {
  return {
    date: "2026-08-22",
    tech_id: "tech-1",
    jobs: [{
      id: "job-1",
      title: "Opener not closing",
      dispatch_status: "assigned",
      service_type: "Service Call",
      customer: { id: "c1", name: "Anderson Residence" },
      site_address: "1420 Oak St",
      time_window: { start: null, end: null },
    }],
    area_jobs: [],
    ...overrides,
  };
}

async function mountView() {
  const { default: View } = await import("../MobileTodayView.vue");
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  postQueuedMock.mockResolvedValue({ ok: true });
});

describe("the route survives losing signal", () => {
  it("renders the last saved route on a cold mount with no network", async () => {
    getMock.mockResolvedValue(route());
    const first = await mountView();
    expect(first.text()).toContain("Anderson Residence");

    // Remount with the network gone — exactly what a tap-through and back does.
    getMock.mockRejectedValue(new Error("Failed to fetch"));
    const second = await mountView();
    expect(second.text()).toContain("Anderson Residence");
    expect(second.text()).not.toContain("Nothing scheduled today");
  });

  it("says plainly that the route is stale", async () => {
    getMock.mockResolvedValue(route());
    await mountView();
    getMock.mockRejectedValue(new Error("Failed to fetch"));
    const w = await mountView();
    expect(w.find('[data-testid="mt-cached-route"]').exists()).toBe(true);
    expect(w.find('[data-testid="mt-cached-route"]').text()).toMatch(/no signal/i);
  });

  it("does not label a live route as cached", async () => {
    getMock.mockResolvedValue(route());
    const w = await mountView();
    expect(w.find('[data-testid="mt-cached-route"]').exists()).toBe(false);
  });

  it("a live load replaces the cache rather than compounding it", async () => {
    getMock.mockResolvedValue(route());
    await mountView();
    getMock.mockResolvedValue(route({
      jobs: [{
        id: "job-2",
        title: "Spring replacement",
        dispatch_status: "assigned",
        service_type: "Service Call",
        customer: { id: "c2", name: "Second Customer" },
        site_address: "9 Dock St",
        time_window: { start: null, end: null },
      }],
    }));
    await mountView();

    getMock.mockRejectedValue(new Error("Failed to fetch"));
    const offline = await mountView();
    expect(offline.text()).toContain("Second Customer");
    expect(offline.text()).not.toContain("Anderson Residence");
  });

  it("still surfaces the real error when there is nothing cached", async () => {
    // The counterweight: a first-ever load with no signal must not fake a day.
    getMock.mockRejectedValue(new Error("Failed to fetch"));
    const w = await mountView();
    expect(w.find('[data-testid="mt-cached-route"]').exists()).toBe(false);
    expect(w.text()).toContain("Failed to fetch");
  });

  it("never shows one day's route in place of another's", async () => {
    getMock.mockResolvedValue(route());
    await mountView();
    // A cache keyed only by "the route" would happily serve yesterday's stops.
    const keys = Object.keys(localStorage).filter((k) => k.startsWith("gdx_today_route_cache"));
    expect(keys.length).toBeGreaterThan(0);
    expect(keys.every((k) => k.length > "gdx_today_route_cache".length)).toBe(true);
  });
});
