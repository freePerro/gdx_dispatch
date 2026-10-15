/**
 * DispatchView — week board (techs down the side, days across the top).
 *
 * Doug, 2026-10-15: the week view took no drops and its arrows stepped one
 * day, so the "week" slid a column at a time. Mounts the REAL DispatchView
 * (same harness as DispatchVisitCards.spec.js).
 *
 * Pinned:
 *  1. Week view: the arrows step seven days; day view: one.
 *  2. A job card lands in its tech's row on its day; undated intake is in the
 *     "To schedule" strip, not on a day.
 *  3. A drop on another tech's day keeps the card's time and sends that tech.
 *  4. A drop in a row whose tech is already on the job is a date-only PATCH —
 *     for every crew member's row, with or without a duration, so a crew is
 *     never cut down to one tech by a date move (/audit, 2026-10-15).
 *  5. A job with no time dropped on a tech's empty day starts at that tech's
 *     shift start; on a day with work it asks where it goes among the day's
 *     jobs (Doug, 2026-10-15), and Cancel moves nothing.
 *  6. A visit card dropped on a cell is the visit route with that day and its time.
 *  7. The "No tech" row refuses a job that has a tech, and moves nothing.
 *  8. Clicking a day header opens that day in day view.
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

async function mountBoard() {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/visits")) return Promise.resolve({ items: visitItems, job_ids: [], timezone: null });
    if (u.includes("/api/dispatch/")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
      { id: "tech-2", name: "Ana", active: true, effective_shift_start: "07:30", effective_shift_end: "16:00" },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve([]);
    if (u.includes("/api/jobs")) return Promise.resolve(boardRows);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  w.vm.$.setupState.viewMode = "week";
  await flushAll();
  return w;
}

const cell = (w, row, day) => w.find(`[data-testid="week-cell-${row}-${day}"]`);
async function dragOnto(w, job, row, day) {
  w.vm.onDragStart(job, null);
  await cell(w, row, day).trigger("drop");
  await flushAll();
}
const wallOf = (iso) => { const d = new Date(iso); return [keyOf(d), d.getHours(), d.getMinutes()]; };

describe("DispatchView — week board", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    patchMock.mockResolvedValue({});
    granted = new Set(["jobs.read_all"]);
    boardRows = [JOB, INTAKE]; visitItems = [];
  });

  it("steps a week at a time in week view and a day in day view", async () => {
    const w = await mountBoard();
    const start = new Date(w.vm.$.setupState.selectedDate);
    await w.find('[data-testid="dispatch-next-day-btn"]').trigger("click");
    const next = new Date(w.vm.$.setupState.selectedDate);
    expect(Math.round((next - start) / 86_400_000)).toBe(7);
    await w.find('[data-testid="dispatch-prev-day-btn"]').trigger("click");
    w.vm.$.setupState.viewMode = "day";
    await flushAll();
    await w.find('[data-testid="dispatch-next-day-btn"]').trigger("click");
    expect(Math.round((new Date(w.vm.$.setupState.selectedDate) - start) / 86_400_000)).toBe(1);
  });

  it("draws a job in its tech's row on its day, and undated intake in the strip", async () => {
    const w = await mountBoard();
    expect(cell(w, "tech-1", TODAY).find(`[data-testid="week-job-${TODAY}-job-1"]`).exists()).toBe(true);
    expect(cell(w, "tech-1", TODAY).text()).toContain("10:30a");
    expect(cell(w, "tech-2", TODAY).text()).not.toContain("Lee Park");
    expect(w.find('[data-testid="week-queue-job-2"]').exists()).toBe(true);
    expect(w.find('[data-testid="week-board"]').text()).not.toContain("Sam Diaz");
  });

  it("moves a job to another tech's day at the same time", async () => {
    const w = await mountBoard();
    await dragOnto(w, JOB, "tech-2", OTHER_DAY);
    const call = patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-1");
    expect(call[1]).toMatchObject({ assigned_tech_id: "tech-2", technician_id: "tech-2" });
    expect(wallOf(call[1].scheduled_at)).toEqual([OTHER_DAY, 10, 30]);
  });

  it("re-dates only when the tech is unchanged", async () => {
    const w = await mountBoard();
    await dragOnto(w, JOB, "tech-1", OTHER_DAY);
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [url, body] = patchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/job-1");
    expect(Object.keys(body)).toEqual(["scheduled_at"]);
    expect(wallOf(body.scheduled_at)).toEqual([OTHER_DAY, 10, 30]);
  });

  it("re-dates a two-tech job from either crew row without touching the crew", async () => {
    const CREW = { ...JOB, assigned_tech_ids: ["tech-1", "tech-2"] };
    const NO_EST = { ...CREW, id: "job-3", scheduled_duration_hours: null, effective_duration_hours: null };
    boardRows = [CREW, NO_EST, INTAKE];
    const w = await mountBoard();
    expect(cell(w, "tech-2", TODAY).text()).toContain("Lee Park");
    await dragOnto(w, CREW, "tech-2", OTHER_DAY);
    await dragOnto(w, NO_EST, "tech-1", OTHER_DAY);
    expect(w.find('[data-testid="dispatch-duration-prompt"]').exists()).toBe(false);
    expect(patchMock.mock.calls.map(([u, b]) => [u, Object.keys(b)])).toEqual([
      ["/api/jobs/job-1", ["scheduled_at"]],
      ["/api/jobs/job-3", ["scheduled_at"]],
    ]);
    expect(wallOf(patchMock.mock.calls[0][1].scheduled_at)).toEqual([OTHER_DAY, 10, 30]);
  });

  it("starts a job with no time at the tech's shift start on an empty day", async () => {
    const w = await mountBoard();
    await dragOnto(w, INTAKE, "tech-2", OTHER_DAY);
    expect(w.find('[data-testid="week-slot-prompt"]').exists()).toBe(false);
    const call = patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-2");
    expect(call[1]).toMatchObject({ assigned_tech_id: "tech-2" });
    expect(wallOf(call[1].scheduled_at)).toEqual([OTHER_DAY, 7, 30]);
  });

  it("asks where a job with no time goes on a day that has work", async () => {
    const w = await mountBoard();
    w.vm.onDragStart(INTAKE, null);
    const drop = cell(w, "tech-1", TODAY).trigger("drop");
    await flushAll();
    const prompt = w.find('[data-testid="week-slot-prompt"]');
    expect(prompt.exists()).toBe(true);
    // Before 10:30 (the 3h job fits from 7:30), Lee Park's 10:30–12:30, after it.
    expect(prompt.text()).toContain("Put it here — starts 7:30a");
    expect(prompt.text()).toContain("10:30a–12:30p");
    expect(prompt.text()).toContain("Put it here — starts 12:30p");
    expect(patchMock).not.toHaveBeenCalled();
    await w.find('[data-testid="week-slot-1"]').trigger("click");
    expect(w.vm.$.setupState.slotPromptTime).toBe("12:30");
    await w.find('[data-testid="week-slot-confirm"]').trigger("click");
    await drop; await flushAll();
    const call = patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-2");
    expect(call[1]).toMatchObject({ assigned_tech_id: "tech-1" });
    expect(wallOf(call[1].scheduled_at)).toEqual([TODAY, 12, 30]);
  });

  it("moves nothing when the placement question is cancelled", async () => {
    const w = await mountBoard();
    w.vm.onDragStart(INTAKE, null);
    const drop = cell(w, "tech-1", TODAY).trigger("drop");
    await flushAll();
    await w.find('[data-testid="week-slot-cancel"]').trigger("click");
    await drop; await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(w.find('[data-testid="week-slot-prompt"]').exists()).toBe(false);
  });

  it("moves a visit card by the visit route, keeping its time", async () => {
    const start = at(0, 8);
    visitItems = [{
      ...JOB, job_has_crew: true, day_count: 2, day_index: 1,
      visit_id: "v1", visit_tech_id: "tech-1", visit_tech_name: "Mike",
      visit_start: start.toISOString(), visit_end: at(0, 16).toISOString(),
      visit_state: "open", visit_day: TODAY,
    }];
    const w = await mountBoard();
    const card = w.vm.boardJobs.find((j) => j.card_key === "job-1:v1");
    expect(card).toBeTruthy();
    await dragOnto(w, card, "tech-2", OTHER_DAY);
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1/visits/v1",
      { day: OTHER_DAY, start_time: "08:00", tech_id: "tech-2" },
      { suppressErrorToast: true },
    );
  });

  it("refuses a job with a tech on the No tech row", async () => {
    const w = await mountBoard();
    await dragOnto(w, JOB, "unassigned", OTHER_DAY);
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: "Nothing was moved" }));
  });

  it("opens a day from its header", async () => {
    const w = await mountBoard();
    await w.find(`[data-testid="week-day-head-${OTHER_DAY}"]`).trigger("click");
    expect(w.vm.$.setupState.viewMode).toBe("day");
    expect(keyOf(new Date(w.vm.$.setupState.selectedDate))).toBe(OTHER_DAY);
  });
});
