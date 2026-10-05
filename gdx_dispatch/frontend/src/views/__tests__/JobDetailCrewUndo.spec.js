/**
 * Job Detail crew row — Undo arrival (multi-day jobs plan §5.2a).
 *
 * A tech who has tapped in (`arrived_at` on the assignment row) gets an Undo
 * arrival action for dispatch managers; it opens UndoArrivalDialog in job
 * mode for that tech (GET/POST /api/jobs/{id}/undo-arrival).
 * Harness copied from JobDetailStagePaths.spec.js.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-1" }, query: {}, path: "/jobs/job-1" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    del: vi.fn(),
    request: vi.fn(),
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(), confirmDestructive: vi.fn() }),
}));
let role = "admin";
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role }, hasPermission: () => true }),
}));

/**
 * @param job         what GET /api/jobs/job-1 returns
 * @param customer    what GET /api/customers/{id} returns; null = the read fails
 */
function routeGet({ crew = [], job = {}, customer = { id: "cust-1", name: "Acme Door Co" } } = {}) {
  getMock.mockImplementation(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/customers/") && !u.includes("/locations")) {
      if (!customer) throw Object.assign(new Error("denied"), { status: 404 });
      return customer;
    }
    if (u === "/api/jobs/job-1/assignments") return crew;
    if (u.startsWith("/api/technicians")) return [{ id: "t1", name: "Mike" }, { id: "t2", name: "Sam" }];
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
        DataTable: true, Column: true,
        Dialog: { props: ['visible', 'header'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        MobileJobCloseoutDialog: { props: ['visible', 'jobId'], template: '<div data-testid="closeout-sheet" :data-open="String(visible)" :data-job="jobId" />' }, Select: true, InputText: true,
        Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' }, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true,
        JobStateOverrideDialog: { props: ['modelValue'], template: '<div data-testid="reopen-dialog" :data-open="String(modelValue)" />' }, CatalogPickerDialog: true,
        DoorSpecList: true, PhoneInput: true, EmailTimeline: true, InputNumber: true,
        AuthedImage: true,
        UndoArrivalDialog: { props: ['modelValue', 'mode', 'jobId', 'techId', 'techName'], template: '<div v-if="modelValue" data-testid="crew-undo-dialog">{{ mode }}:{{ jobId }}:{{ techId }}:{{ techName }}</div>' },
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  role = "admin";
});

const CREW = [
  { id: "a1", tech_id: "t1", is_lead: true, arrived_at: "2026-10-05T13:14:00Z" },
  { id: "a2", tech_id: "t2", is_lead: false, arrived_at: null },
];
const OPEN = { status: "Scheduled", lifecycle_stage: "Scheduled", lifecycle_stage_raw: "scheduled", customer_id: "cust-1" };

describe("job page crew row — Undo arrival", () => {
  it("shows for a tech who tapped in, and opens the job-mode dialog for them", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    expect(w.find('[data-testid="assignment-undo-arrival-t2"]').exists()).toBe(false);
    await w.get('[data-testid="assignment-undo-arrival-t1"]').trigger("click");
    expect(w.get('[data-testid="crew-undo-dialog"]').text()).toBe("job:job-1:t1:Mike");
  });

  it("is not offered to a role the server would refuse", async () => {
    role = "technician";
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    expect(w.find('[data-testid="assignment-t1"]').exists()).toBe(true);
    expect(w.find('[data-testid="assignment-undo-arrival-t1"]').exists()).toBe(false);
  });
});
