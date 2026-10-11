/**
 * The office job page shows and map-links the jobsite's TOWN (GDXA-502).
 *
 * A customer location keeps city/state/zip in their own columns. The page's
 * client-side chain read only `address`, so the line and the Google Maps link
 * said "88 Pine Ridge Rd" with no town. GET /api/jobs/{id} now carries
 * site_full_address from core/job_site.py — the one composition — and the
 * page prefers it.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-1" }, query: {}, path: "/jobs/job-1" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
    request: vi.fn(),
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(), confirmDestructive: vi.fn() }),
}));
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role: "admin" }, hasPermission: () => true }),
}));

/**
 * @param job         what GET /api/jobs/job-1 returns
 * @param customer    what GET /api/customers/{id} returns; null = the read fails
 */
function routeGet({ job = {}, customer = { id: "cust-1", name: "Acme Door Co" }, locations = [] } = {}) {
  getMock.mockImplementation(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/customers/") && !u.includes("/locations")) {
      if (!customer) throw Object.assign(new Error("denied"), { status: 404 });
      return customer;
    }
    if (u === "/api/jobs/job-1") {
      return { id: "job-1", title: "Spring replacement", status: "Scheduled", ...job };
    }
    if (u === "/api/customers/cust-1/locations") return locations;
    return [];
  });
}

async function mountView() {
  const { default: View } = await import("../JobDetailView.vue");
  const w = mount(View, {
    global: {
      stubs: {
        // Renders `to` as an href so the assertion reads the real binding —
        // a `true` stub would swallow it and pass for any destination.
        RouterLink: {
          props: ["to"],
          template: '<a :href="to" :data-testid="$attrs[\'data-testid\']"><slot /></a>',
          inheritAttrs: false,
        },
        Button: { props: ["label"], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
        DataTable: true, Column: true, Dialog: true, Select: true, InputText: true,
        Textarea: true, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true, JobStateOverrideDialog: true, CatalogPickerDialog: true,
        DoorSpecList: true, PhoneInput: true, EmailTimeline: true, InputNumber: true,
        AuthedImage: true,
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
});

const FULL = "88 Pine Ridge Rd, Anytown, MN 55555";
const LOC = { id: "loc-1", label: "Home", address: "88 Pine Ridge Rd", city: "Anytown", state: "MN", zip: "55555" };

describe("office job page — the jobsite address carries the town", () => {
  it("shows and map-links the server's full address", async () => {
    routeGet({
      job: { customer_id: "cust-1", location_id: "loc-1", site_address: LOC.address, site_full_address: FULL },
      locations: [LOC],
    });
    const w = await mountView();
    const line = w.get('[data-testid="job-customer-address"]');
    expect(line.text()).toContain(FULL);
    expect(line.get("a").attributes("href")).toBe(`https://maps.google.com/?q=${encodeURIComponent(FULL)}`);
  });

  it("a missing site stays missing — no address line, the warning shows", async () => {
    routeGet({
      job: { customer_id: "cust-1", location_id: "loc-1", site_address: null, site_full_address: null },
      locations: [{ ...LOC, address: null }],
    });
    const w = await mountView();
    expect(w.find('[data-testid="job-customer-address"]').exists()).toBe(false);
    expect(w.text()).toContain("no address on file");
  });

  it("a payload without the field keeps the old street-only chain", async () => {
    routeGet({ job: { customer_id: "cust-1", location_id: "loc-1" }, locations: [LOC] });
    const w = await mountView();
    expect(w.get('[data-testid="job-customer-address"]').text()).toContain("88 Pine Ridge Rd");
  });
});
