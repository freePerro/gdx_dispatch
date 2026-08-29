/**
 * DispatchView — the job drawer's "Close out" opens the closeout sheet (#526).
 *
 * This mounts the REAL DispatchView. Its predecessor (DispatchCompleteFlow.spec.js)
 * re-implemented quickStatusChange in a fake host and grepped the source for the
 * handler's text — and stayed green for the entire life of the repo while the
 * handler had no caller: the status select that once invoked it was replaced by
 * the read-only JobStateChip, so the board had NO way to complete a job. A test
 * that mounts the view and clicks the button cannot be fooled that way.
 *
 * Pinned:
 *  1. A live job's drawer offers "Close out"; clicking it opens the sheet for
 *     THAT job and sends no HTTP of its own (the sheet POSTs on submit).
 *  2. A completed / cancelled job's drawer does not offer it.
 *  3. When the sheet reports closed-out, the sheet AND the drawer close, and
 *     the board refetches.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const pushMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: patchMock, del: vi.fn() }),
}));

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DatePicker: { props: ["modelValue"], emits: ["update:modelValue"], template: '<input type="date" />' },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Avatar: { props: ["label"], template: "<span>{{ label }}</span>" },
  Button: { props: ["label"], emits: ["click"], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  Drawer: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  Select: { props: ["modelValue", "options"], emits: ["change"], template: "<select />" },
  SelectButton: { props: ["modelValue", "options"], template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  InputText: { props: ["modelValue"], template: "<input />" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  TechTimelineColumn: { template: "<div />" },
  TechEfficiencyPanel: { template: "<div />" },
  JobStateChip: { template: "<span />" },
  MobileJobCloseoutDialog: {
    props: ["visible", "jobId", "jobTitle", "jobType", "customerName"],
    emits: ["update:visible", "closed-out"],
    template: '<div data-testid="closeout-stub" :data-visible="String(visible)" :data-job="jobId" :data-title="jobTitle" />',
  },
};

const flushAll = async () => { await flushPromises(); await flushPromises(); };

// An intake-lane job (no tech, no date): its card is rendered by DispatchView
// itself, so the drawer is opened by a REAL card click through normalizeJob —
// not by handing the view a fixture.
function job(overrides = {}) {
  return {
    id: "job-9", status: "In Progress", lifecycle_stage: "in_progress",
    title: "Spring swap", customer_name: "Acme", address: "1 Main", job_type: "service",
    time_window: "Anytime", technician_id: null, assigned_to: null, scheduled_at: null,
    ...overrides,
  };
}

let boardJobs = [];
function serve(jobs) { boardJobs = jobs; }

async function mountBoard(theJob) {
  serve([theJob]);
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/technicians")) return Promise.resolve([{ id: "tech-1", user_id: "Mike", name: "Mike" }]);
    if (u.includes("/api/jobs")) return Promise.resolve(boardJobs);
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  const card = w.find(`[data-testid="unassigned-job-${theJob.id}"]`);
  if (card.exists()) {
    await card.trigger("click");
  } else {
    // A completed / cancelled job has no intake-lane card to click (the lane
    // filters terminal jobs out), so the drawer is opened the way a timeline
    // card would open it — with the board's own normalized object.
    const normalized = w.vm.$.setupState.jobs.find((j) => String(j.id) === String(theJob.id));
    w.vm.openJobDrawer(normalized || theJob);
  }
  await flushAll();
  return w;
}

describe("DispatchView — job drawer Close out (#526)", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    pushMock.mockReset();
  });

  it("a live job's drawer offers Close out; clicking it opens the sheet for that job and the BOARD sends nothing (the sheet POSTs on submit)", async () => {
    const w = await mountBoard(job());
    const btn = w.find('[data-testid="dispatch-job-closeout"]');
    expect(btn.exists()).toBe(true);
    expect(w.find('[data-testid="closeout-stub"]').attributes("data-visible")).toBe("false");

    postMock.mockClear(); patchMock.mockClear();
    await btn.trigger("click");
    await flushAll();

    const stub = w.find('[data-testid="closeout-stub"]');
    expect(stub.attributes("data-visible")).toBe("true");
    expect(stub.attributes("data-job")).toBe("job-9");
    expect(stub.attributes("data-title")).toBe("Spring swap");
    expect(postMock).not.toHaveBeenCalled();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("a completed job's drawer does not offer Close out", async () => {
    const w = await mountBoard(job({ status: "Complete", lifecycle_stage: "completed" }));
    expect(w.find('[data-testid="dispatch-job-open"]').exists()).toBe(true);
    expect(w.find('[data-testid="dispatch-job-closeout"]').exists()).toBe(false);
  });

  it("a cancelled job's drawer does not offer Close out", async () => {
    const w = await mountBoard(job({ status: "Cancelled", lifecycle_stage: "cancelled" }));
    expect(w.find('[data-testid="dispatch-job-closeout"]').exists()).toBe(false);
  });

  it("the drawer follows the board's refresh: a job the tech closed out meanwhile stops offering Close out", async () => {
    const w = await mountBoard(job());
    expect(w.find('[data-testid="dispatch-job-closeout"]').exists()).toBe(true);
    // The tech closes out from the truck; the next poll brings the new state.
    serve([job({ status: "Completed", lifecycle_stage: "completed" })]);
    await w.vm.fetchJobs();
    await flushAll();
    expect(w.find('[data-testid="dispatch-job-drawer"]').exists()).toBe(true);
    expect(w.find('[data-testid="dispatch-job-closeout"]').exists()).toBe(false);
  });

  it("the drawer closes when its job leaves the board", async () => {
    const w = await mountBoard(job());
    serve([]);
    await w.vm.fetchJobs();
    await flushAll();
    expect(w.find('[data-testid="dispatch-job-drawer"]').exists()).toBe(false);
  });

  it("closed-out closes the sheet AND the drawer, and refetches the board", async () => {
    const w = await mountBoard(job());
    await w.find('[data-testid="dispatch-job-closeout"]').trigger("click");
    await flushAll();
    const jobsCallsBefore = getMock.mock.calls.filter((c) => String(c[0]).includes("/api/jobs")).length;

    w.findComponent(stubs.MobileJobCloseoutDialog).vm.$emit("closed-out", { ok: true });
    await flushAll();

    expect(w.find('[data-testid="closeout-stub"]').attributes("data-visible")).toBe("false");
    expect(w.find('[data-testid="dispatch-job-drawer"]').exists()).toBe(false);
    const jobsCallsAfter = getMock.mock.calls.filter((c) => String(c[0]).includes("/api/jobs")).length;
    expect(jobsCallsAfter).toBeGreaterThan(jobsCallsBefore);
  });
});
