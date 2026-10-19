/**
 * DispatchView — approved time off blocks the board (GDXA-1829, parent GDXA-1775).
 *
 * Doug, 2026-10-19: technicians ask for time off on the existing Time off path;
 * approved time off blocks the dispatch board's day and week views. The board
 * reads GET /api/dispatch/time-off (approved TimeOffRequest rows only). Mounts
 * the REAL DispatchView (same harness as DispatchWeekBoard.spec.js).
 *
 * Pinned:
 *  1. The time-off read asks for the window the board shows, and only for a
 *     user who may read the board (jobs.read_all, as the visits read).
 *  2. Week view: a tech's day off shows a "Time off" block in that cell, and a
 *     drop there moves nothing and says why; the same tech's other day still
 *     takes the drop.
 *  3. Day view: the tech's column shows the "Time off" block and "Time off"
 *     for capacity; a drop on the timeline or the tray moves nothing; another
 *     tech's column is untouched.
 *  4. The queue card's tech picker and the visit dropdown refuse it as well.
 *  5. A failed time-off read warns once; it does not pass for "nobody is off".
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";

const getMock = vi.fn();
const patchMock = vi.fn();
const toastAdd = vi.fn();
let granted = new Set();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn(), patch: patchMock, del: vi.fn() }),
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

const TechTimelineColumn = {
  name: "TechTimelineColumn",
  props: ["tech", "jobs"],
  emits: ["place", "place-tray"],
  template: "<div data-testid='timeline-stub' />",
};

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DatePicker: { props: ["modelValue"], template: '<input type="date" />' },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Avatar: { props: ["label"], template: "<span>{{ label }}</span>" },
  Button: { props: ["label"], emits: ["click"], template: '<button @click="$emit(\'click\', $event)">{{ label }}</button>' },
  Dialog: { props: ["visible"], template: "<div v-if='visible' data-testid='dialog'><slot /><slot name='footer' /></div>" },
  Drawer: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  Select: { props: ["modelValue", "options"], template: "<select />" },
  SelectButton: { props: ["modelValue", "options"], template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  InputText: { props: ["modelValue"], template: "<input />" },
  MultiSelect: { props: ["modelValue", "options"], emits: ["update:modelValue"], template: "<div />" },
  TechTimelineColumn,
  TechEfficiencyPanel: { template: "<div />" },
  JobStateChip: { template: "<span />" },
  DataTable: { template: "<div />" },
  Column: { template: "<div />" },
  MobileJobCloseoutDialog: { template: "<div />" },
};

const flushAll = async () => { await flushPromises(); await flushPromises(); };
const pad = (n) => String(n).padStart(2, "0");
const keyOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const at = (offset, h, m = 0) => { const d = new Date(); d.setDate(d.getDate() + offset); d.setHours(h, m, 0, 0); return d; };
// Another day in the SAME Sunday-started week as today.
const OTHER = new Date().getDay() === 6 ? -1 : 1;
const TODAY = keyOf(at(0, 0));
const OTHER_DAY = keyOf(at(OTHER, 0));

const JOB = {
  id: "job-1", title: "Spring replace", job_type: "Repair", status: "Scheduled",
  customer_id: "c1", customer_name: "Lee Park", assigned_to: "tech-1", assigned_tech_ids: ["tech-1"],
  technician_id: "tech-1",
  scheduled_at: at(0, 10, 30).toISOString(), holding_area_id: null,
  scheduled_duration_hours: 2, effective_duration_hours: 2,
};
const INTAKE = {
  id: "job-2", title: "New opener", job_type: "Install", status: "New",
  customer_id: "c2", customer_name: "Sam Diaz", assigned_tech_ids: [], scheduled_at: null,
  holding_area_id: null, scheduled_duration_hours: 3, effective_duration_hours: 3,
};

let boardRows = [];
let visitItems = [];
let areas = [];
let timeOff = [];

async function mountBoard(view = "week") {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/time-off")) return Promise.resolve({ items: timeOff });
    if (u.includes("/api/dispatch/visits")) return Promise.resolve({ items: visitItems, job_ids: [], timezone: null });
    if (u.includes("/api/dispatch/")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
      { id: "tech-2", name: "Ana", active: true, effective_shift_start: "07:30", effective_shift_end: "16:00" },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve(areas);
    if (u.includes("/api/jobs")) return Promise.resolve(boardRows);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  if (view) {
    w.vm.$.setupState.viewMode = view;
    await flushAll();
  }
  return w;
}

const cell = (w, row, day) => w.find(`[data-testid="week-cell-${row}-${day}"]`);
async function dragOnto(w, job, row, day) {
  w.vm.onDragStart(job, null);
  await cell(w, row, day).trigger("drop");
  await flushAll();
}

const OFF = (techId, start, end = start) => ({
  technician_id: techId, request_id: `req-${techId}`, entry_type: "vacation", label: "Vacation",
  start_date: start, end_date: end,
});

describe("DispatchView — approved time off", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    patchMock.mockResolvedValue({});
    granted = new Set(["jobs.read_all"]);
    boardRows = [JOB, INTAKE]; visitItems = []; areas = []; timeOff = [];
    window.localStorage.clear();
  });

  const timeOffCalls = () => getMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/api/dispatch/time-off"));

  it("reads time off for the window on screen, and only with jobs.read_all", async () => {
    await mountBoard("week");
    const last = timeOffCalls().at(-1);
    expect(last).toMatch(/\/api\/dispatch\/time-off\?date_from=\d{4}-\d{2}-\d{2}&date_to=\d{4}-\d{2}-\d{2}$/);
    const visits = getMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes("/api/dispatch/visits")).at(-1);
    expect(last.split("?")[1]).toBe(visits.split("?")[1]);

    getMock.mockReset();
    granted = new Set();
    await mountBoard("week");
    expect(timeOffCalls()).toEqual([]);
  });

  it("week view: blocks the tech's day off and refuses a drop there", async () => {
    timeOff = [OFF("tech-2", OTHER_DAY)];
    const w = await mountBoard("week");
    const offCell = cell(w, "tech-2", OTHER_DAY);
    expect(offCell.classes()).toContain("is-time-off");
    expect(offCell.find(`[data-testid="week-time-off-tech-2-${OTHER_DAY}"]`).text()).toBe("Time off · Vacation");
    expect(cell(w, "tech-2", TODAY).find('[data-testid^="week-time-off"]').exists()).toBe(false);
    expect(cell(w, "tech-1", OTHER_DAY).find('[data-testid^="week-time-off"]').exists()).toBe(false);

    await dragOnto(w, JOB, "tech-2", OTHER_DAY);
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was moved", detail: expect.stringContaining("Ana has approved time off (Vacation)"),
    }));

    await dragOnto(w, JOB, "tech-2", TODAY);
    expect(patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-1")[1]).toMatchObject({ technician_id: "tech-2" });
  });

  it("day view: shows the block and refuses timeline and tray drops", async () => {
    timeOff = [OFF("tech-2", TODAY, OTHER_DAY)];
    const w = await mountBoard("day");
    const block = w.find('[data-testid="tech-time-off-tech-2"]');
    expect(block.exists()).toBe(true);
    expect(block.text()).toContain("Time off · Vacation");
    expect(w.find('[data-testid="tech-capacity-tech-2"]').text()).toBe("Time off");
    expect(w.find('[data-testid="tech-column-tech-2"]').classes()).toContain("tech-column--time-off");
    expect(w.find('[data-testid="tech-time-off-tech-1"]').exists()).toBe(false);
    expect(w.find('[data-testid="tech-capacity-tech-1"]').text()).not.toContain("Time off");

    const timeline = w.findAllComponents(TechTimelineColumn).find((c) => c.props("tech").id === "tech-2");
    w.vm.onDragStart(JOB, null, "tech-1");
    timeline.vm.$emit("place", { jobId: "job-1", techId: "tech-2", startISO: at(0, 13).toISOString() });
    await flushAll();
    w.vm.onDragStart(INTAKE, null);
    timeline.vm.$emit("place-tray", { jobId: "job-2", techId: "tech-2" });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledTimes(2);
  });

  it("the queue card's tech picker and the visit dropdown refuse a day off too", async () => {
    timeOff = [OFF("tech-2", TODAY)];
    const w = await mountBoard("day");
    await w.vm.assignFromPicker("job-1", "tech-2");
    await w.vm.assignFromPicker("job-2", "tech-2");
    await w.vm.assignVisitTech({ job_id: "job-1", scheduled_at: JOB.scheduled_at }, "tech-2");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledTimes(3);
    expect(toastAdd).toHaveBeenLastCalledWith(expect.objectContaining({ summary: "Nothing was moved" }));
  });

  it("a failed time-off read says so once instead of showing nobody off", async () => {
    const w = await mountBoard("day");
    getMock.mockImplementation((url) => (String(url).includes("/api/dispatch/time-off")
      ? Promise.reject(new Error("boom"))
      : Promise.resolve(String(url).includes("/api/jobs") ? boardRows : { items: [] })));
    await w.vm.fetchJobs();
    await w.vm.fetchJobs();
    await flushAll();
    const warned = toastAdd.mock.calls.filter(([t]) => t.summary === "Time off could not be loaded");
    expect(warned).toHaveLength(1);
  });
});
