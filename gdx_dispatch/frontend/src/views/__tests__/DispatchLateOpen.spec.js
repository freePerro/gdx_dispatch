/**
 * DispatchView — "Past their date, not closed out" card (2026-09-27).
 *
 * The board fetches only undated jobs and the dates in view, so an open job
 * whose scheduled day had passed was on no screen at all — prod had six, the
 * oldest scheduled 2026-05-13. This card is where they show.
 *
 * Mounts the REAL DispatchView (same harness as DispatchDrawerCloseout.spec.js)
 * and drives the card through its own endpoint mock.
 *
 * Pinned:
 *  1. No rows → the card does not exist (it must never become wallpaper).
 *  2. Rows → one line per job with customer, tech and how late it is.
 *  3. "Close out" opens the board's closeout sheet for THAT job, sending
 *     nothing itself; "Open job" routes to the job page.
 *  4. A user without jobs.read_all never calls the endpoint.
 *  5. Closing the sheet reloads the list, so a closed-out job drops off.
 *  6. Hide folds the rows (the board's drag targets must stay above the
 *     fold) without hiding the header or count.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { h } from "vue";
import DispatchView from "../DispatchView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const pushMock = vi.fn();
let granted = new Set();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: patchMock, del: vi.fn() }),
}));
vi.mock("../../composables/usePermission", async () => {
  const { ref } = await import("vue");
  return {
    usePermission: () => ({
      hasPermission: (k) => granted.has(k),
      permissions: ref([]),
      permissionsLoaded: ref(true),
      reloadPermissions: () => Promise.resolve(),
    }),
  };
});

// DataTable/Column are stubbed to render each row through the Column body
// slots, so the real cell templates (and their buttons) are exercised.
const Column = { props: ["header"], template: "<div />" };
const DataTable = {
  props: ["value"],
  render() {
    const cols = this.$slots.default?.() || [];
    return h("div", (this.value || []).map((row) =>
      h("div", { class: "row" },
        cols.map((col) => h("span", col.children?.body ? col.children.body({ data: row }) : [])))));
  },
};

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
  DataTable,
  Column,
  MobileJobCloseoutDialog: {
    props: ["visible", "jobId", "jobTitle", "jobType", "customerName"],
    emits: ["update:visible", "closed-out"],
    template: '<div data-testid="closeout-stub" :data-visible="String(visible)" :data-job="jobId" :data-title="jobTitle" />',
  },
};

const flushAll = async () => { await flushPromises(); await flushPromises(); };

const VOELTZ = {
  id: "job-31", job_number: "JOB-2026-031", title: "Replace broken spring", job_type: "Service Call",
  status: "Scheduled", lifecycle_stage: "scheduled", scheduled_at: "2026-06-02T14:00:00+00:00",
  days_late: 117, customer_id: "c1", customer_name: "Paula Vance", assigned_to: "tech-1",
  tech_name: "Mike", is_return_visit: false,
};

let lateRows = [];
async function mountBoard() {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/late-open")) return Promise.resolve({ items: lateRows });
    if (u.includes("/api/technicians")) return Promise.resolve([{ id: "tech-1", user_id: "Mike", name: "Mike" }]);
    if (u.includes("/api/jobs")) return Promise.resolve([]);
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  return w;
}

const lateCalls = () => getMock.mock.calls.filter(([u]) => String(u).includes("/api/dispatch/late-open")).length;

describe("DispatchView — past their date, not closed out", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); postMock.mockReset(); patchMock.mockReset(); pushMock.mockReset();
    granted = new Set(["jobs.read_all"]);
    lateRows = [];
  });

  it("does not render at all when nothing is late", async () => {
    const w = await mountBoard();
    expect(lateCalls()).toBeGreaterThan(0);
    expect(w.find('[data-testid="late-open-jobs"]').exists()).toBe(false);
  });

  it("lists a late job with its customer, tech and how late it is", async () => {
    lateRows = [VOELTZ];
    const w = await mountBoard();
    const card = w.find('[data-testid="late-open-jobs"]');
    expect(card.exists()).toBe(true);
    expect(card.text()).toContain("Past their date, not closed out");
    expect(card.text()).toContain("Paula Vance");
    expect(card.text()).toContain("Replace broken spring");
    expect(card.text()).toContain("Mike");
    expect(w.find('[data-testid="late-open-days-job-31"]').text()).toContain("117 days ago");
  });

  it("Close out opens the board's closeout sheet for that job and sends nothing itself", async () => {
    lateRows = [VOELTZ];
    const w = await mountBoard();
    postMock.mockClear(); patchMock.mockClear();
    await w.find('[data-testid="late-open-closeout-job-31"]').trigger("click");
    await flushAll();
    const stub = w.find('[data-testid="closeout-stub"]');
    expect(stub.attributes("data-visible")).toBe("true");
    expect(stub.attributes("data-job")).toBe("job-31");
    expect(stub.attributes("data-title")).toBe("Replace broken spring");
    expect(postMock).not.toHaveBeenCalled();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("Open job routes to the job page", async () => {
    lateRows = [VOELTZ];
    const w = await mountBoard();
    await w.find('[data-testid="late-open-open-job-31"]').trigger("click");
    expect(pushMock).toHaveBeenCalledWith("/jobs/job-31");
  });

  it("closing the sheet reloads the list, so a closed-out job drops off", async () => {
    lateRows = [VOELTZ];
    const w = await mountBoard();
    await w.find('[data-testid="late-open-closeout-job-31"]').trigger("click");
    await flushAll();
    lateRows = [];
    w.findComponent(stubs.MobileJobCloseoutDialog).vm.$emit("closed-out");
    await flushAll();
    expect(w.find('[data-testid="late-open-jobs"]').exists()).toBe(false);
  });

  it("Hide folds the rows away but keeps the header and count; Show brings them back", async () => {
    lateRows = [VOELTZ];
    const w = await mountBoard();
    const toggle = w.find('[data-testid="late-open-toggle"]');
    expect(toggle.text()).toBe("Hide");
    await toggle.trigger("click");
    expect(w.find('[data-testid="late-open-closeout-job-31"]').exists()).toBe(false);
    expect(w.find('[data-testid="late-open-jobs"]').text()).toContain("Past their date, not closed out");
    expect(w.find('[data-testid="late-open-toggle"]').text()).toBe("Show");
    await w.find('[data-testid="late-open-toggle"]').trigger("click");
    expect(w.find('[data-testid="late-open-closeout-job-31"]').exists()).toBe(true);
  });

  it("never calls the endpoint for a user without jobs.read_all", async () => {
    granted = new Set();
    lateRows = [VOELTZ];
    const w = await mountBoard();
    expect(lateCalls()).toBe(0);
    expect(w.find('[data-testid="late-open-jobs"]').exists()).toBe(false);
  });
});
