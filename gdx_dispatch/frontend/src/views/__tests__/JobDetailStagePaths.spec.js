/**
 * Job page stage changes go through the paths that own them
 * (job-stage-paths plan §4.2).
 *
 * "Complete Job" and the stage strip used to PATCH {status}: no completed_at,
 * no parts/hours gates, no webhook, and a finished job could be moved anywhere
 * with no reason. Now Complete opens the closeout sheet, a finished job's strip
 * opens the Re-open dialog, and "Close without work" posts its own verb. The
 * server refuses the old PATCHes (409), so each assertion below also checks
 * that no status PATCH was sent.
 *
 * Fixtures are the real get_job shape: `lifecycle_stage` is the display label
 * and `lifecycle_stage_raw` the stored enum.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const toastAdd = vi.fn();
const confirmMock = vi.fn();

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
  useDestructiveConfirm: () => ({ confirmAsync: confirmMock, confirmDestructive: vi.fn() }),
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
        DataTable: true, Column: true,
        Dialog: { props: ['visible', 'header'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        MobileJobCloseoutDialog: { props: ['visible', 'jobId'], template: '<div data-testid="closeout-sheet" :data-open="String(visible)" :data-job="jobId" />' }, Select: true, InputText: true,
        Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' }, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true,
        JobStateOverrideDialog: { props: ['modelValue'], template: '<div data-testid="reopen-dialog" :data-open="String(modelValue)" />' }, CatalogPickerDialog: true,
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


const statusPatches = () => patchMock.mock.calls.filter(([, body]) => body && ("status" in body || "lifecycle_stage" in body));
const OPEN = { status: "Scheduled", lifecycle_stage: "Scheduled", lifecycle_stage_raw: "scheduled", customer_id: "cust-1" };
const DONE = { status: "Complete", lifecycle_stage: "Complete", lifecycle_stage_raw: "completed", customer_id: "cust-1" };
const CANCELLED = { status: "Cancelled", lifecycle_stage: "Cancelled", lifecycle_stage_raw: "cancelled", customer_id: "cust-1" };

describe("job page — completing goes through the closeout sheet", () => {
  it("Complete Job opens the closeout sheet and sends no status PATCH", async () => {
    routeGet({ job: OPEN });
    const w = await mountView();
    expect(w.get('[data-testid="closeout-sheet"]').attributes("data-open")).toBe("false");
    await w.get('[data-testid="job-detail-complete"]').trigger("click");
    expect(w.get('[data-testid="closeout-sheet"]').attributes("data-open")).toBe("true");
    expect(w.get('[data-testid="closeout-sheet"]').attributes("data-job")).toBe("job-1");
    expect(statusPatches()).toHaveLength(0);
  });

  it("the strip's Complete opens the closeout sheet too", async () => {
    routeGet({ job: OPEN });
    const w = await mountView();
    await w.get('[data-testid="job-detail-stage-complete"]').trigger("click");
    expect(w.get('[data-testid="closeout-sheet"]').attributes("data-open")).toBe("true");
    expect(statusPatches()).toHaveLength(0);
  });

  it("an open-to-open strip move still PATCHes", async () => {
    routeGet({ job: OPEN });
    patchMock.mockResolvedValue({});
    const w = await mountView();
    await w.get('[data-testid="job-detail-stage-in-progress"]').trigger("click");
    await flushPromises();
    expect(statusPatches()).toEqual([["/api/jobs/job-1", { status: "In Progress" }, expect.anything()]]);
  });
});

describe("job page — a finished job moves only through Re-open", () => {
  for (const [name, job] of [["completed", DONE], ["cancelled", CANCELLED]]) {
    it(`a ${name} job's strip opens the Re-open dialog and sends nothing`, async () => {
      routeGet({ job });
      const w = await mountView();
      await w.get('[data-testid="job-detail-stage-scheduled"]').trigger("click");
      expect(w.get('[data-testid="reopen-dialog"]').attributes("data-open")).toBe("true");
      expect(statusPatches()).toHaveLength(0);
    });

    it(`a ${name} job offers neither Complete Job nor Close without work`, async () => {
      routeGet({ job });
      const w = await mountView();
      expect(w.find('[data-testid="job-detail-complete"]').exists()).toBe(false);
      expect(w.find('[data-testid="job-detail-close-without-work"]').exists()).toBe(false);
    });
  }
});

describe("job page — Close without work", () => {
  it("posts the reason to /close-without-work", async () => {
    routeGet({ job: OPEN });
    postMock.mockResolvedValue({ ok: true });
    const w = await mountView();
    await w.get('[data-testid="job-detail-close-without-work"]').trigger("click");
    const submit = w.get('[data-testid="close-without-work-submit"]');
    expect(submit.attributes("disabled")).toBeDefined();
    await w.get('[data-testid="close-without-work-reason"]').setValue("duplicate of JOB-12");
    await w.get('[data-testid="close-without-work-submit"]').trigger("click");
    await flushPromises();
    const call = postMock.mock.calls.find(([u]) => String(u).endsWith("/close-without-work"));
    expect(call?.[0]).toBe("/api/jobs/job-1/close-without-work");
    expect(call?.[1]).toEqual({ reason: "duplicate of JOB-12" });
    expect(call?.[2]).toMatchObject({ suppressErrorToast: true });
    expect(statusPatches()).toHaveLength(0);
  });

  // Multi-day jobs PR 3, plan §5.4a "The office's way out".
  function routeDayLog(log) {
    const base = getMock.getMockImplementation();
    getMock.mockImplementation(async (url) => {
      if (String(url) === "/api/jobs/job-1/day-log") return log;
      return base(url);
    });
  }

  it("says how many hours are already logged on earlier days", async () => {
    routeGet({ job: OPEN });
    routeDayLog({ rows: [{ id: "r1", date: "2026-10-05", person_name: "Ann", hours: 8 }], logged_hours_total: 8 });
    const w = await mountView();
    await w.get('[data-testid="job-detail-close-without-work"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="close-without-work-logged"]').text())
      .toBe("Already logged: 8 h on earlier days (still billed)");
  });

  it("shows nothing about logged hours on a job with none", async () => {
    routeGet({ job: OPEN });
    const w = await mountView();
    await w.get('[data-testid="job-detail-close-without-work"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="close-without-work-logged"]').exists()).toBe(false);
  });

  it("an earlier_day_open refusal shows inline with a way into the sheet", async () => {
    routeGet({ job: OPEN });
    postMock.mockRejectedValue(Object.assign(new Error("open"), {
      status: 409,
      body: { detail: "An earlier day is open", code: "earlier_day_open", date: "2026-10-05" },
    }));
    const w = await mountView();
    await w.get('[data-testid="job-detail-close-without-work"]').trigger("click");
    await w.get('[data-testid="close-without-work-reason"]').setValue("customer cancelled the rest");
    await w.get('[data-testid="close-without-work-submit"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="close-without-work-earlier-day"]').text())
      .toContain("Monday, Oct 5 is still open. Close that day first");
    expect(toastAdd).not.toHaveBeenCalled();
    await w.get('[data-testid="close-without-work-open-sheet"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-testid="close-without-work-dialog"]').exists()).toBe(false);
    expect(w.get('[data-testid="closeout-sheet"]').attributes("data-open")).toBe("true");
  });

  it("any other refusal is toasted and keeps the dialog open", async () => {
    routeGet({ job: OPEN });
    postMock.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403, body: { detail: "Forbidden" } }));
    const w = await mountView();
    await w.get('[data-testid="job-detail-close-without-work"]').trigger("click");
    await w.get('[data-testid="close-without-work-reason"]').setValue("duplicate");
    await w.get('[data-testid="close-without-work-submit"]').trigger("click");
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: "error", detail: "Forbidden" }));
    expect(w.find('[data-testid="close-without-work-dialog"]').exists()).toBe(true);
  });
});

describe("job page — Cancel job (GDXA-375)", () => {
  async function submitCancel(w, reason = "customer went elsewhere") {
    await w.get('[data-testid="job-detail-cancel"]').trigger("click");
    const submit = w.get('[data-testid="cancel-job-submit"]');
    expect(submit.attributes("disabled")).toBeDefined();
    await w.get('[data-testid="cancel-job-reason"]').setValue(reason);
    await w.get('[data-testid="cancel-job-submit"]').trigger("click");
    await flushPromises();
  }

  it("confirms, then posts the reason to /cancel and never PATCHes the stage", async () => {
    routeGet({ job: OPEN });
    confirmMock.mockResolvedValue(true);
    postMock.mockResolvedValue({ ok: true });
    const w = await mountView();
    await submitCancel(w);
    expect(confirmMock).toHaveBeenCalledTimes(1);
    const call = postMock.mock.calls.find(([u]) => String(u).endsWith("/cancel"));
    expect(call?.[0]).toBe("/api/jobs/job-1/cancel");
    expect(call?.[1]).toEqual({ reason: "customer went elsewhere" });
    expect(statusPatches()).toHaveLength(0);
    expect(w.find('[data-testid="cancel-job-dialog"]').exists()).toBe(false);
  });

  it("declining the confirm sends nothing and keeps the reason", async () => {
    routeGet({ job: OPEN });
    confirmMock.mockResolvedValue(false);
    const w = await mountView();
    await submitCancel(w);
    expect(postMock.mock.calls.find(([u]) => String(u).endsWith("/cancel"))).toBeUndefined();
    expect(w.get('[data-testid="cancel-job-reason"]').element.value).toBe("customer went elsewhere");
  });

  it("a refusal is toasted and keeps the dialog open", async () => {
    routeGet({ job: OPEN });
    confirmMock.mockResolvedValue(true);
    postMock.mockRejectedValue(Object.assign(new Error("conflict"), {
      status: 409, body: { detail: "An arrival needs an answer first" },
    }));
    const w = await mountView();
    await submitCancel(w);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: "warn", detail: "An arrival needs an answer first" }));
    expect(w.find('[data-testid="cancel-job-dialog"]').exists()).toBe(true);
    expect(w.find('[data-testid="cancel-job-appointments"]').exists()).toBe(false);
  });

  it("a refusal whose way out is Appointments links there", async () => {
    routeGet({ job: OPEN });
    confirmMock.mockResolvedValue(true);
    postMock.mockRejectedValue(Object.assign(new Error("conflict"), {
      status: 409,
      body: { code: "needs_answer", question: "status_only_arrival", detail: "An arrival needs an answer first" },
    }));
    const w = await mountView();
    await submitCancel(w);
    expect(w.find('[data-testid="cancel-job-appointments"]').exists()).toBe(true);
  });

  for (const [name, job] of [["completed", DONE], ["cancelled", CANCELLED]]) {
    it(`a ${name} job offers no Cancel job`, async () => {
      routeGet({ job });
      const w = await mountView();
      expect(w.find('[data-testid="job-detail-cancel"]').exists()).toBe(false);
    });
  }

  it("a cancelled job shows its recorded cancel", async () => {
    routeGet({ job: { ...CANCELLED, cancelled_at: "2026-10-09T12:00:00Z", cancel_reason: "customer went elsewhere" } });
    const w = await mountView();
    expect(w.find('[data-testid="job-detail-cancelled"]').exists()).toBe(true);
  });
});

describe("job page — Daily log card", () => {
  it("lists the day rows by day and hides when there are none", async () => {
    routeGet({ job: OPEN });
    const base = getMock.getMockImplementation();
    getMock.mockImplementation(async (url) => (String(url) === "/api/jobs/job-1/day-log"
      ? {
        rows: [
          { id: "r2", date: "2026-10-06", person_name: "Bob", hours: 7.5, note: "Track up", closed_by: "Ann" },
          { id: "r1", date: "2026-10-05", person_name: "Added helper", hours: 4, note: null, closed_by: "Ann" },
        ],
        logged_hours_total: 11.5,
      }
      : base(url)));
    const w = await mountView();
    const card = w.get('[data-testid="job-daily-log"]');
    expect(card.get('[data-testid="job-daily-log-total"]').text()).toBe("11.5 h logged");
    const r2 = card.get('[data-testid="job-daily-log-row-r2"]').text();
    expect(r2).toContain("Bob");
    expect(r2).toContain("7.5 h");
    expect(r2).toContain("closed by Ann");
    expect(r2).toContain("Track up");
    expect(card.get('[data-testid="job-daily-log-day-2026-10-05"]').text()).toContain("Added helper");
  });

  it("is hidden on a job with no day rows", async () => {
    routeGet({ job: OPEN });
    const w = await mountView();
    expect(w.find('[data-testid="job-daily-log"]').exists()).toBe(false);
  });
});
