/**
 * GDXA-227: the CALLBACK chip on a job whose parent is completed but has no
 * completed_at. The server cannot run the 90-day test, so it says
 * callback_undetermined; the page shows "CALLBACK?" instead of nothing.
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

const tid = (w, id) => w.find(`[data-testid="${id}"]`);

describe("office job page — callback chip", () => {
  it("shows CALLBACK? when the parent's completion date is missing", async () => {
    routeGet({ job: { is_callback: false, callback_undetermined: true } });
    const w = await mountView();
    expect(tid(w, "job-detail-callback-undetermined").exists()).toBe(true);
    expect(tid(w, "job-detail-callback").exists()).toBe(false);
  });

  it("a dated callback shows CALLBACK only", async () => {
    routeGet({ job: { is_callback: true, callback_undetermined: false } });
    const w = await mountView();
    expect(tid(w, "job-detail-callback").exists()).toBe(true);
    expect(tid(w, "job-detail-callback-undetermined").exists()).toBe(false);
  });

  it("an ordinary job shows neither", async () => {
    routeGet({ job: { is_callback: false, callback_undetermined: false } });
    const w = await mountView();
    expect(tid(w, "job-detail-callback").exists()).toBe(false);
    expect(tid(w, "job-detail-callback-undetermined").exists()).toBe(false);
  });
});
