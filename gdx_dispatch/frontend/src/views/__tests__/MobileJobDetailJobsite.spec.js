/**
 * MobileJobDetailView — the Jobsite row (PR 1, jobsite-address plan).
 *
 * The address on this screen is the JOBSITE (server-resolved site_address,
 * core/job_site.py), not blindly the customer record. Pinned here:
 *  1. A bound site renders its label chip + address, and the customer's own
 *     address appears as a clearly-secondary line when it differs.
 *  2. A bound site with NO address says so ("ask dispatch") — it must never
 *     silently render the customer HQ as if it were the site (D2).
 *  3. Same address → no redundant secondary line.
 *  4. Access notes (gate codes) render when the site carries them.
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
      navigation_link: "https://maps.google.com/?q=9+Dock+St",
      site_label: null,
      site_address: null,
      site_address_missing: false,
      site_access_notes: null,
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
  postQueuedMock.mockResolvedValue({ ok: true });
});

describe("jobsite row", () => {
  it("renders the bound site's label + address, customer address secondary", async () => {
    const w = await mountWith({
      site_label: "Warehouse 3",
      site_address: "9 Dock St",
    });
    const row = w.find('[data-testid="mobile-job-detail-address"]');
    expect(row.exists()).toBe(true);
    expect(row.text()).toContain("9 Dock St");
    expect(w.find('[data-testid="mjd-site-label"]').text()).toBe("Warehouse 3");
    const secondary = w.find('[data-testid="mjd-customer-address-secondary"]');
    expect(secondary.exists()).toBe(true);
    expect(secondary.text()).toContain("100 Billing Rd");
  });

  it("bound site with no address says ask-dispatch, never shows HQ as the site", async () => {
    const w = await mountWith({
      site_label: "Warehouse 3",
      site_address: null,
      site_address_missing: true,
      navigation_link: null,
    });
    expect(w.find('[data-testid="mobile-job-detail-address"]').exists()).toBe(false);
    const missing = w.find('[data-testid="mjd-site-address-missing"]');
    expect(missing.exists()).toBe(true);
    expect(missing.text()).toContain("ask dispatch");
    expect(missing.text()).not.toContain("100 Billing Rd");
    // The customer address is still findable — but only as the secondary row.
    expect(w.find('[data-testid="mjd-customer-address-secondary"]').text())
      .toContain("100 Billing Rd");
  });

  it("no secondary line when the site IS the customer address", async () => {
    const w = await mountWith({ site_address: "100 Billing Rd" });
    expect(w.find('[data-testid="mobile-job-detail-address"]').text())
      .toContain("100 Billing Rd");
    expect(w.find('[data-testid="mjd-customer-address-secondary"]').exists()).toBe(false);
  });

  it("renders site access notes when present", async () => {
    const w = await mountWith({
      site_address: "9 Dock St",
      site_access_notes: "gate 4411",
    });
    expect(w.find('[data-testid="mjd-site-access-notes"]').text()).toContain("gate 4411");
  });
});


describe("fix-address sheet (PR 4)", () => {
  it("routes 'fix it' by the source the display came from", async () => {
    const w = await mountWith({
      site_label: "Warehouse 3", site_address: "9 Dock St", site_source: "location",
    });
    await w.find('[data-testid="mjd-fix-address"]').trigger("click");
    const label = w.find('[data-testid="mjd-fix-address-form"]').text();
    expect(label).toContain("updates this site for all its jobs");
  });

  it("customer-source jobs offer to fix the customer's address", async () => {
    const w = await mountWith({ site_address: "100 Billing Rd", site_source: "customer" });
    await w.find('[data-testid="mjd-fix-address"]').trigger("click");
    expect(w.find('[data-testid="mjd-fix-address-form"]').text())
      .toContain("Fix the customer's address");
  });

  it("submits the queued PATCH with expected_address (stale-replay guard)", async () => {
    patchQueuedMock.mockResolvedValue({ ok: true });
    const w = await mountWith({ site_address: "9 Dock St", site_source: "location" });
    await w.find('[data-testid="mjd-fix-address"]').trigger("click");
    const input = w.find('[data-testid="mjd-fix-address-input"]');
    input.element.value = "11 Dock St";
    await input.trigger("input");
    await w.find('[data-testid="mjd-fix-address-save"]').trigger("click");
    expect(patchQueuedMock).toHaveBeenCalledWith(
      "/api/mobile/jobs/job-123/site",
      expect.objectContaining({
        address: "11 Dock St",
        apply_to: "source",
        expected_address: "9 Dock St",
      }),
      expect.objectContaining({ actionType: "job.site_fix" }),
    );
  });

  it("hidden entirely on read-only grants", async () => {
    // read_only is a TOP-LEVEL payload flag (readOnly.value = r?.read_only),
    // not a job field — drive it through the real payload shape.
    const { default: View } = await import("../MobileJobDetailView.vue");
    getMock.mockImplementation(async () => ({
      ...jobPayload({ site_address: "9 Dock St", site_source: "location" }),
      read_only: true,
      access_grant: "company",
    }));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="mjd-readonly-banner"]').exists()).toBe(true);
    expect(w.find('[data-testid="mjd-fix-address"]').exists()).toBe(false);
  });
});
