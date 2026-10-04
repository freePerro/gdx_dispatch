/**
 * The office job page's "Scheduled For" row on a job that never went on the
 * calendar.
 *
 * It read "Not yet scheduled" whatever the stage, so a finished job showed
 * "Complete" over "Not yet scheduled" — as if still waiting to be booked.
 * Prod 2026-10-03: 205 completed jobs have no scheduled_at; 21 of those carry
 * a completed_at, 184 carry neither.
 *
 * Pinned here:
 *  1. A scheduled date always wins.
 *  2. Complete + completed_at -> "Completed {date}".
 *  3. Complete or Cancelled with no dates -> "Not scheduled" (no "yet").
 *  4. An open job keeps "Not yet scheduled".
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

const sched = (w) => w.get('[data-testid="job-detail-scheduled-for"]').text();

describe("office job page — Scheduled For on an unscheduled job", () => {
  it("shows the scheduled date when there is one, even on a completed job", async () => {
    routeGet({ job: { status: "Complete", lifecycle_stage: "Complete", scheduled_at: "2026-09-14T15:00:00Z", completed_at: "2026-09-15T20:00:00Z" } });
    const w = await mountView();
    expect(sched(w)).not.toMatch(/scheduled|Completed/i);
    expect(sched(w)).toMatch(/2026/);
  });

  it("a completed job with no schedule shows when it was completed", async () => {
    routeGet({ job: { status: "Complete", lifecycle_stage: "Complete", scheduled_at: null, completed_at: "2026-09-15T20:00:00Z" } });
    const w = await mountView();
    expect(sched(w)).toMatch(/^Completed .*2026/);
  });

  it("a completed job with neither date says Not scheduled, never 'yet'", async () => {
    routeGet({ job: { status: "Complete", lifecycle_stage: "Complete", scheduled_at: null, completed_at: null } });
    const w = await mountView();
    expect(sched(w)).toBe("Not scheduled");
  });

  it("a cancelled job with no schedule says Not scheduled", async () => {
    routeGet({ job: { status: "Cancelled", lifecycle_stage: "Cancelled", scheduled_at: null } });
    const w = await mountView();
    expect(sched(w)).toBe("Not scheduled");
  });

  it("an open job keeps Not yet scheduled", async () => {
    routeGet({ job: { status: "Service Call", lifecycle_stage: "Service Call", scheduled_at: null, completed_at: null } });
    const w = await mountView();
    expect(sched(w)).toBe("Not yet scheduled");
  });
});
