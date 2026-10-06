/**
 * DispatchView — visit cards (multi-day jobs plan §5.3, PR 2b).
 *
 * A job its job row cannot draw (more than one day, or a day moved off the
 * row's time or tech) comes from GET /api/dispatch/visits, one item per
 * visit, and the board draws those cards instead of the job row. A drop
 * moves that visit only (PATCH /api/jobs/{id}/visits/{visit_id}).
 *
 * Mounts the REAL DispatchView (same harness as DispatchPartialJobs.spec.js).
 *
 * Pinned:
 *  1. A visit card replaces its job row: drawn in the visit's tech column,
 *     with the visit's length, and the row is not drawn in the crew's column.
 *  2. A timeline drop is the visit route with the shop day, time and tech;
 *     the board re-reads both lists.
 *  3. A refusal toasts the server's sentence and still re-reads.
 *  4. A tray drop is the board day at 00:00.
 *  5. A CLOSED or ON SITE card cannot be dragged.
 *  6. A job its row draws keeps today's path (PATCH /api/jobs/{id}).
 *  7. A visit card dropped on a holding area or the New Jobs queue moves nothing.
 *  8. A holding lane lists a multi-day job once, at the job's hours.
 *  9. Release on a visit card writes the job and re-reads the visits —
 *     parked partial job included.
 * 10. New Jobs: an unassigned day of a crewed job shows even with the red
 *     lane on; a crew-less one is left to the lane. Its key is the card key.
 * 11. The drawer keeps describing the same day after a poll.
 * 12. An older read answering last does not overwrite a newer one.
 * 13. Without jobs.read_all the visits read is never made.
 * 14. A date change re-reads both lists; week view draws the card with its badge.
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
const atDays = (n, h = 9) => { const d = new Date(); d.setDate(d.getDate() + n); d.setHours(h, 0, 0, 0); return d; };
const keyOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const TODAY = keyOf(new Date());

// The job row: dated today at 9 for Ana (tech-2), but day 1 was moved to Mike.
const ROW = {
  id: "job-9", title: "Commercial 3-door install", job_type: "Install", status: "Scheduled",
  customer_id: "c1", customer_name: "Dana Ruiz", assigned_to: "tech-2", assigned_tech_ids: ["tech-2"],
  scheduled_at: atDays(0).toISOString(), holding_area_id: null, effective_duration_hours: 20,
};

function visit(over = {}) {
  const start = over.start || atDays(0);
  const hours = over.hours ?? 8;
  return {
    ...ROW, job_has_crew: true, day_count: 2, day_index: 1,
    visit_id: "v1", visit_tech_id: "tech-1", visit_tech_name: "Mike",
    visit_start: start.toISOString(),
    visit_end: new Date(start.getTime() + hours * 3600_000).toISOString(),
    visit_state: "open", visit_day: keyOf(start),
    ...over,
  };
}

let boardRows = [];
let visitItems = [];
let holdingAreas = [];
let partialRows = [];
let settings = {};

async function mountBoard() {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/visits")) return Promise.resolve({ items: visitItems, job_ids: [], timezone: null });
    if (u.includes("/api/dispatch/partial-jobs")) return Promise.resolve({ items: partialRows });
    if (u.includes("/api/dispatch/late-open")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch/scheduled-unassigned")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch-settings")) return Promise.resolve(settings);
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true }, { id: "tech-2", name: "Ana", active: true },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve(holdingAreas);
    if (u.includes("/api/jobs")) return Promise.resolve(boardRows);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  return w;
}

const timeline = (w, techId) => w.findAllComponents(TechTimelineColumn).find((c) => String(c.props("tech")?.id) === techId);
const visitReads = () => getMock.mock.calls.filter(([u]) => String(u).includes("/api/dispatch/visits"));
const cardOf = (w, key) => w.vm.boardJobs.find((j) => j.card_key === key);

describe("DispatchView — visit cards", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    granted = new Set(["jobs.read_all"]);
    boardRows = [ROW]; visitItems = [visit()]; holdingAreas = []; partialRows = []; settings = {};
  });

  it("draws the visit in its own tech's column instead of the job row", async () => {
    const w = await mountBoard();
    const mike = timeline(w, "tech-1").props("jobs");
    expect(mike.map((j) => j.card_key)).toEqual(["job-9:v1"]);
    expect(mike[0]).toMatchObject({ day_index: 1, day_count: 2, effective_duration_hours: 8, job_duration_hours: 20 });
    expect(timeline(w, "tech-2").props("jobs")).toEqual([]);
  });

  it("moves only that visit on a timeline drop, then re-reads both lists", async () => {
    const w = await mountBoard();
    const card = cardOf(w, "job-9:v1");
    w.vm.onDragStart(card, null);
    patchMock.mockResolvedValue({});
    const before = visitReads().length;
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-9", techId: "tech-2", startISO: atDays(0, 13).toISOString() });
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-9/visits/v1",
      { day: TODAY, start_time: "13:00", tech_id: "tech-2" },
      { suppressErrorToast: true },
    );
    expect(visitReads().length).toBe(before + 1);
  });

  it("says the server's refusal and still re-reads", async () => {
    const w = await mountBoard();
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    patchMock.mockRejectedValue(Object.assign(new Error("x"), {
      body: { code: "visit_in_past", detail: "That day has passed." },
    }));
    const before = visitReads().length;
    timeline(w, "tech-1").vm.$emit("place", { jobId: "job-9", techId: "tech-1", startISO: atDays(-1).toISOString() });
    await flushAll();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: "Visit not moved", detail: "That day has passed." }));
    expect(visitReads().length).toBe(before + 1);
  });

  it("puts a tray drop on the board day at 00:00", async () => {
    const w = await mountBoard();
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    patchMock.mockResolvedValue({});
    timeline(w, "tech-1").vm.$emit("place-tray", { jobId: "job-9", techId: "tech-1" });
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-9/visits/v1", { day: TODAY, start_time: "00:00" }, { suppressErrorToast: true },
    );
  });

  it("sends nothing for a drop that changes nothing", async () => {
    visitItems = [visit({ start: atDays(0, 0) })];
    const w = await mountBoard();
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    timeline(w, "tech-1").vm.$emit("place-tray", { jobId: "job-9", techId: "tech-1" });
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    timeline(w, "tech-1").vm.$emit("place", { jobId: "job-9", techId: "tech-1", startISO: atDays(0, 0).toISOString() });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("assigns an unassigned day from its New Jobs dropdown, with no schedule button", async () => {
    visitItems = [visit({ visit_tech_id: null, visit_tech_name: null })];
    // An ordinary undated job beside it, for its button count.
    boardRows = [ROW, { ...ROW, id: "job-5", assigned_to: null, assigned_tech_ids: [], scheduled_at: null }];
    const w = await mountBoard();
    const card = w.find('[data-testid="unassigned-job-job-9:v1"]');
    const plain = w.find('[data-testid="unassigned-job-job-5"]');
    expect(card.findAll("button").length).toBe(plain.findAll("button").length - 1);
    patchMock.mockResolvedValue({});
    await w.vm.$.setupState.assignVisitTech(cardOf(w, "job-9:v1"), "tech-2");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-9/visits/v1", { day: TODAY, start_time: "09:00", tech_id: "tech-2" }, { suppressErrorToast: true },
    );
  });

  it("keys a crew's two cards on one day apart", async () => {
    visitItems = [visit(), visit({ visit_id: "v2", visit_tech_id: "tech-2" })];
    const w = await mountBoard();
    expect(timeline(w, "tech-1").props("jobs")[0].card_key).toBe("job-9:v1");
    expect(timeline(w, "tech-2").props("jobs")[0].card_key).toBe("job-9:v2");
  });

  it.each(["closed", "on_site"])("will not drag a %s day", async (state) => {
    visitItems = [visit({ visit_state: state })];
    const w = await mountBoard();
    const ev = { preventDefault: vi.fn(), dataTransfer: { setData: vi.fn() } };
    w.vm.onDragStart(cardOf(w, "job-9:v1"), ev);
    expect(ev.preventDefault).toHaveBeenCalled();
    expect(w.vm.draggingJobId).toBeNull();
  });

  it("leaves a job its row draws on the job route", async () => {
    visitItems = [];
    const w = await mountBoard();
    const row = timeline(w, "tech-2").props("jobs")[0];
    expect(row.visit_id).toBeUndefined();
    w.vm.onDragStart(row, null);
    patchMock.mockResolvedValue({});
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-9", techId: "tech-2", startISO: atDays(0, 13).toISOString() });
    await flushAll();
    expect(patchMock.mock.calls[0][0]).toBe("/api/jobs/job-9");
  });

  it("moves nothing when a day is dropped on a holding area or the New Jobs queue", async () => {
    holdingAreas = [{ id: "ha-1", name: "Needs Parts" }];
    const w = await mountBoard();
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    await w.vm.moveToHoldingArea({ dataTransfer: { getData: () => "job-9" } }, "ha-1");
    w.vm.onDragStart(cardOf(w, "job-9:v1"), null);
    await w.vm.moveToScheduleQueue({ dataTransfer: { getData: () => "job-9" } });
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledTimes(2);
    expect(toastAdd.mock.calls[0][0]).toMatchObject({ summary: "Nothing was moved" });
  });

  it("lists a parked multi-day job once in its lane, at the job's hours", async () => {
    holdingAreas = [{ id: "ha-1", name: "Needs Parts" }];
    visitItems = [
      visit({ holding_area_id: "ha-1" }),
      visit({ holding_area_id: "ha-1", visit_id: "v2", visit_tech_id: "tech-2", visit_tech_name: "Ana" }),
    ];
    const w = await mountBoard();
    const lane = w.vm.getHoldingAreaJobs("ha-1");
    expect(lane).toHaveLength(1);
    expect(lane[0].effective_duration_hours).toBe(20);
  });

  it("releases a visit card by writing the job and re-reading the visits", async () => {
    holdingAreas = [{ id: "ha-1", name: "Needs Parts" }];
    visitItems = [visit({ holding_area_id: "ha-1" })];
    const w = await mountBoard();
    patchMock.mockResolvedValue({});
    const before = visitReads().length;
    await w.vm.releaseFromHoldingArea("job-9", cardOf(w, "job-9:v1"));
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-9", { holding_area_id: null });
    expect(visitReads().length).toBe(before + 1);
  });

  it("re-reads the visits after releasing a parked partial job's card", async () => {
    holdingAreas = [{ id: "ha-1", name: "Needs Parts" }];
    visitItems = [visit({ holding_area_id: "ha-1" })];
    partialRows = [{ ...ROW, holding_area_id: "ha-1", last_worked_day: TODAY, worked_by: [] }];
    const w = await mountBoard();
    patchMock.mockResolvedValue({});
    const before = visitReads().length;
    await w.vm.releaseFromHoldingArea("job-9", cardOf(w, "job-9:v1"));
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-9", { holding_area_id: null });
    expect(visitReads().length).toBe(before + 1);
  });

  it("queues an unassigned day of a crewed job even with the red lane on", async () => {
    settings = { dispatch_show_unassigned_lane: true };
    visitItems = [visit({ visit_tech_id: null, visit_tech_name: null })];
    const w = await mountBoard();
    expect(w.find('[data-testid="unassigned-job-job-9:v1"]').exists()).toBe(true);
    expect(w.find('[data-testid="day-badge-job-9:v1"]').text()).toContain("Day 1 of 2");
  });

  // Audit 2026-10-06: a finished job's visit keeps status "scheduled", so it
  // reads open; the server refuses to move it, so the card must not drag.
  it("does not let a finished job's open visit drag", async () => {
    visitItems = [visit(), visit({ id: "job-8", visit_id: "v8", lifecycle_stage: "completed", status: "Completed" })];
    boardRows = [ROW, { ...ROW, id: "job-8" }];
    const w = await mountBoard();
    expect(cardOf(w, "job-9:v1").visit_movable).toBe(true);
    expect(cardOf(w, "job-8:v8").visit_movable).toBe(false);
    const ev = { preventDefault: vi.fn(), dataTransfer: { setData: () => {} } };
    w.vm.onDragStart(cardOf(w, "job-8:v8"), ev);
    expect(ev.preventDefault).toHaveBeenCalled();
  });

  it("gives a one-day job drawn by visit no day label", async () => {
    visitItems = [visit({ visit_tech_id: null, visit_tech_name: null, day_count: 1 })];
    const w = await mountBoard();
    expect(w.find('[data-testid="unassigned-job-job-9:v1"]').exists()).toBe(true);
    expect(w.find('[data-testid="day-badge-job-9:v1"]').exists()).toBe(false);
  });

  it("leaves a crew-less job's unassigned day to the red lane", async () => {
    settings = { dispatch_show_unassigned_lane: true };
    visitItems = [visit({ visit_tech_id: null, visit_tech_name: null, job_has_crew: false })];
    const w = await mountBoard();
    expect(w.find('[data-testid="unassigned-job-job-9:v1"]').exists()).toBe(false);
  });

  it("keeps the drawer on the same day after a poll", async () => {
    visitItems = [visit(), visit({ visit_id: "v2", visit_tech_id: "tech-2", day_index: 2, visit_state: "closed" })];
    const w = await mountBoard();
    w.vm.openJobDrawer(cardOf(w, "job-9:v2"));
    await w.vm.fetchJobs({ keepOnError: true });
    await flushAll();
    expect(w.find('[data-testid="dispatch-job-drawer-day"]').text()).toContain("Day 2 of 2 · done for the day");
  });

  it("drops an older read that answers after a newer one", async () => {
    const w = await mountBoard();
    let releaseOld;
    const oldVisits = new Promise((r) => { releaseOld = r; });
    const base = getMock.getMockImplementation();
    getMock.mockImplementationOnce(base).mockImplementationOnce(() => oldVisits);
    const first = w.vm.fetchJobs();
    visitItems = [visit({ visit_id: "v9" })];
    await w.vm.fetchJobs();
    releaseOld({ items: [visit({ visit_id: "stale" })] });
    await first;
    await flushAll();
    expect(w.vm.boardJobs.map((j) => j.card_key)).toEqual(["job-9:v9"]);
  });

  it("never asks for visits without jobs.read_all", async () => {
    granted = new Set();
    const w = await mountBoard();
    expect(visitReads()).toHaveLength(0);
    expect(timeline(w, "tech-2").props("jobs").map((j) => j.id)).toEqual(["job-9"]);
  });

  it("re-reads both lists on a date change and draws the card in week view", async () => {
    const w = await mountBoard();
    const before = visitReads().length;
    w.vm.$.setupState.viewMode = "week";
    await flushAll();
    const last = visitReads().at(-1)[0];
    expect(visitReads().length).toBe(before + 1);
    expect(last).toMatch(/date_from=.*date_to=/);
    const card = w.find(`[data-testid="week-job-${TODAY}-job-9:v1"]`);
    expect(card.exists()).toBe(true);
    expect(card.text()).toContain("Day 1 of 2");
  });
});
