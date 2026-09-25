/**
 * The office job page's route to the customer record (GDXA-16).
 *
 * The job was the last desktop document page whose customer name was plain
 * text: `InvoiceDetailView:64` and `EstimateView:42` both wrap the name in a
 * `router-link` to `/customers/:id`, the job printed a `<p>`. The office could
 * see WHO the job was for and had no way to open them.
 *
 * Pinned here:
 *  1. The name is an anchor to /customers/{job.customer_id}.
 *  2. It survives a failed customer read — the id on the job is what routes,
 *     not the record the page happened to load.
 *  3. A lead with no customer renders "Unassigned" as TEXT. A dead anchor, or
 *     one labelled "Unassigned", is the failure this guard exists to stop.
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
function routeGet({ job = {}, customer = { id: "cust-1", name: "Acme Door Co" } } = {}) {
  getMock.mockImplementation(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/customers/") && !u.includes("/locations")) {
      if (!customer) throw Object.assign(new Error("denied"), { status: 404 });
      return customer;
    }
    if (u === "/api/jobs/job-1") {
      return { id: "job-1", title: "Spring replacement", status: "Scheduled", ...job };
    }
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

describe("office job page — the customer name routes to the customer", () => {
  it("links the name to /customers/:id and keeps the Edit affordance", async () => {
    routeGet({ job: { customer_id: "cust-1", customer_name: "Acme Door (stale)" } });
    const w = await mountView();

    const link = w.get('[data-testid="job-detail-customer-link"]');
    expect(link.element.tagName).toBe("A");
    expect(link.attributes("href")).toBe("/customers/cust-1");
    // The loaded record wins over the name denormalized onto the job.
    expect(link.text()).toBe("Acme Door Co");
    expect(w.find('[data-testid="job-detail-edit-customer"]').exists()).toBe(true);
  });

  it("still links when the customer read fails — the job's id is what routes", async () => {
    routeGet({
      job: { customer_id: "cust-7", customer_name: "Bridgeview Storage" },
      customer: null,
    });
    const w = await mountView();

    const link = w.get('[data-testid="job-detail-customer-link"]');
    expect(link.attributes("href")).toBe("/customers/cust-7");
    expect(link.text()).toBe("Bridgeview Storage");
    // Edit needs the loaded record; navigation does not. Unchanged by GDXA-16.
    expect(w.find('[data-testid="job-detail-edit-customer"]').exists()).toBe(false);
  });

  it("a lead with no customer renders plain text, never an anchor", async () => {
    routeGet({ job: { customer_id: null, customer_name: null }, customer: null });
    const w = await mountView();

    expect(w.find('[data-testid="job-detail-customer-link"]').exists()).toBe(false);
    const name = w.get('[data-testid="job-detail-customer-name"]');
    expect(name.element.tagName).toBe("SPAN");
    expect(name.text()).toBe("Unassigned");
  });

  it("a customer whose name never resolved is text, not an anchor reading 'Unassigned'", async () => {
    routeGet({ job: { customer_id: "cust-9", customer_name: "" }, customer: null });
    const w = await mountView();

    expect(w.find('[data-testid="job-detail-customer-link"]').exists()).toBe(false);
    expect(w.get('[data-testid="job-detail-customer-name"]').text()).toBe("Unassigned");
  });
});
