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
 *  9. The "To schedule" strip is drawn when empty and takes a drop: the job
 *     loses its tech AND its date, back to intake (Doug, 2026-10-16).
 * 10. Holding areas are on the week board: a parked job is in its lane, not
 *     on the "No tech" row, and a drop on a lane parks the job there.
 * 11. The strip refuses finished and under-way work, and a chip let go over
 *     the strip or lane it came from writes nothing (/audit, 2026-10-16: it
 *     took a self-assigned tech off an undated job).
 * 12. An undated job with no hours dropped in its own tech's row asks the
 *     hours question before dating it, and sends the hours in the same PATCH;
 *     closing the question moves nothing; the "No tech" row only dates it,
 *     as the day view exempts no-tech drops (GDXA-1261).
 * 13. Day or Week survives a reload (localStorage); a bad stored value or a
 *     storage that throws opens Day (GDXA-1261).
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
let areas = [];

async function mountBoard(view = "week") {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
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
const wallOf = (iso) => { const d = new Date(iso); return [keyOf(d), d.getHours(), d.getMinutes()]; };

describe("DispatchView — week board", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    patchMock.mockResolvedValue({});
    granted = new Set(["jobs.read_all"]);
    boardRows = [JOB, INTAKE]; visitItems = []; areas = [];
    window.localStorage.clear();
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

  describe("an undated job with no hours dropped in its own tech's row", () => {
    const SELF = {
      ...INTAKE, id: "job-5", assigned_to: "tech-1", assigned_tech_ids: ["tech-1"], technician_id: "tech-1",
      scheduled_duration_hours: null, effective_duration_hours: null,
    };
    const durationPrompt = (w) => w.find('[data-testid="dispatch-duration-prompt"]');
    async function dropSelf(w) {
      w.vm.onDragStart(SELF, null);
      const drop = cell(w, "tech-1", OTHER_DAY).trigger("drop");
      await flushAll();
      return drop;
    }

    it("asks for the hours and sends them with the date", async () => {
      boardRows = [JOB, SELF];
      const w = await mountBoard();
      const drop = await dropSelf(w);
      expect(durationPrompt(w).exists()).toBe(true);
      expect(patchMock).not.toHaveBeenCalled();
      w.vm.$.setupState.durationPromptHours = 3;
      await flushAll();
      await w.find('[data-testid="dispatch-duration-prompt-save"]').trigger("click");
      await drop; await flushAll();
      expect(patchMock).toHaveBeenCalledTimes(1);
      const [url, body] = patchMock.mock.calls[0];
      expect(url).toBe("/api/jobs/job-5");
      expect(Object.keys(body).sort()).toEqual(["scheduled_at", "scheduled_duration_hours"]);
      expect(body.scheduled_duration_hours).toBe(3);
      expect(wallOf(body.scheduled_at)).toEqual([OTHER_DAY, 8, 0]);
    });

    it("dates it without hours on Skip", async () => {
      boardRows = [JOB, SELF];
      const w = await mountBoard();
      const drop = await dropSelf(w);
      await w.find('[data-testid="dispatch-duration-prompt-skip"]').trigger("click");
      await drop; await flushAll();
      expect(patchMock.mock.calls.map(([u, b]) => [u, Object.keys(b)])).toEqual([["/api/jobs/job-5", ["scheduled_at"]]]);
    });

    it("moves nothing when the hours question is closed", async () => {
      boardRows = [JOB, SELF];
      const w = await mountBoard();
      const drop = await dropSelf(w);
      w.vm.$.setupState.durationPromptOpen = false;
      w.vm.$.setupState.onDurationPromptHide();
      await drop; await flushAll();
      expect(patchMock).not.toHaveBeenCalled();
    });

    it("on the No tech row only dates it: that row has no capacity to count", async () => {
      const LOOSE = { ...INTAKE, scheduled_duration_hours: null, effective_duration_hours: null };
      boardRows = [JOB, LOOSE];
      const w = await mountBoard();
      await dragOnto(w, LOOSE, "unassigned", OTHER_DAY);
      expect(durationPrompt(w).exists()).toBe(false);
      expect(patchMock.mock.calls.map(([u, b]) => [u, Object.keys(b)])).toEqual([["/api/jobs/job-2", ["scheduled_at"]]]);
    });
  });

  describe("view mode across reloads", () => {
    it("reopens in the mode last picked", async () => {
      const first = await mountBoard();
      expect(window.localStorage.getItem("gdx.dispatch.viewMode")).toBe("week");
      first.unmount();
      const second = await mountBoard(null);
      expect(second.vm.$.setupState.viewMode).toBe("week");
      expect(second.find('[data-testid="week-board"]').exists()).toBe(true);
      second.vm.$.setupState.viewMode = "day";
      await flushAll();
      second.unmount();
      expect((await mountBoard(null)).vm.$.setupState.viewMode).toBe("day");
    });

    it("opens Day on a stored value it does not know", async () => {
      window.localStorage.setItem("gdx.dispatch.viewMode", "month");
      expect((await mountBoard(null)).vm.$.setupState.viewMode).toBe("day");
    });

    it("opens Day when storage throws", async () => {
      // Only this key throws: the auth store reads storage too.
      const real = Storage.prototype.getItem;
      const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(function (k) {
        if (k === "gdx.dispatch.viewMode") throw new Error("denied");
        return real.call(this, k);
      });
      try {
        expect((await mountBoard(null)).vm.$.setupState.viewMode).toBe("day");
      } finally {
        spy.mockRestore();
      }
    });
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

  it("stops marking a slot chosen once the start time is typed over (GDXA-1268)", async () => {
    const w = await mountBoard();
    w.vm.onDragStart(INTAKE, null);
    const drop = cell(w, "tech-1", TODAY).trigger("drop");
    await flushAll();
    const slot0 = () => w.find('[data-testid="week-slot-0"]');
    expect(slot0().classes()).toContain("is-selected");
    w.vm.$.setupState.slotPromptTime = "09:15";
    await flushAll();
    expect(w.findAll(".week-slot-option.is-selected")).toHaveLength(0);
    expect(slot0().find(".pi-circle-fill").exists()).toBe(false);
    w.vm.$.setupState.slotPromptTime = "07:30";
    await flushAll();
    expect(slot0().classes()).toContain("is-selected");
    await w.find('[data-testid="week-slot-cancel"]').trigger("click");
    await drop; await flushAll();
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

  it("takes a job off the schedule when dropped on the To schedule strip, even an empty one", async () => {
    areas = [{ id: "area-ready", name: "Ready to Schedule", color: "#888888" }];
    boardRows = [JOB];
    const w = await mountBoard();
    const strip = w.find('[data-testid="week-queue"]');
    expect(strip.exists()).toBe(true);
    w.vm.onDragStart(JOB, null);
    await strip.trigger("drop");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1",
      { assigned_to: null, holding_area_id: "area-ready", scheduled_at: null },
    );
    expect(w.find('[data-testid="week-queue-job-1"]').exists()).toBe(true);
    expect(cell(w, "tech-1", TODAY).text()).not.toContain("Lee Park");
  });

  it("shows the holding areas, with a parked job in its lane and not on the No tech row", async () => {
    areas = [{ id: "area-parts", name: "Needs Parts", color: "#f59e0b" }];
    const PARKED = {
      ...INTAKE, id: "job-3", customer_name: "Kim Roe",
      scheduled_at: at(OTHER, 9).toISOString(), holding_area_id: "area-parts",
    };
    boardRows = [JOB, INTAKE, PARKED];
    const w = await mountBoard();
    const lane = w.find('[data-testid="week-area-area-parts"]');
    expect(lane.exists()).toBe(true);
    expect(lane.find('[data-testid="week-area-job-area-parts-job-3"]').exists()).toBe(true);
    expect(cell(w, "unassigned", OTHER_DAY).text()).not.toContain("Kim Roe");

    w.vm.onDragStart(JOB, null);
    await lane.trigger("drop");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_to: null, holding_area_id: "area-parts" });
  });

  it.each([
    // The jobs list sends stage labels, not enum values (walk, 2026-10-16).
    ["completed", { lifecycle_stage: "Completed", status: "Completed" }],
    ["in progress", { lifecycle_stage: "In Progress", status: "In Progress" }],
  ])("refuses to take a %s job off the schedule", async (_label, stage) => {
    const STARTED = { ...JOB, ...stage };
    boardRows = [STARTED];
    const w = await mountBoard();
    w.vm.onDragStart(STARTED, null);
    await w.find('[data-testid="week-queue"]').trigger("drop");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: "Nothing was moved" }));
  });

  it("writes nothing when a chip is let go over the strip it came from", async () => {
    const SELF = { ...INTAKE, id: "job-9", assigned_to: "tech-1", technician_id: "tech-1", assigned_tech_ids: ["tech-1"] };
    boardRows = [JOB, SELF];
    const w = await mountBoard();
    expect(w.find('[data-testid="week-queue-job-9"]').exists()).toBe(true);
    w.vm.onDragStart(SELF, null, null, "week-queue");
    await w.find('[data-testid="week-queue"]').trigger("drop");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("writes nothing when a chip is let go over the lane it came from", async () => {
    areas = [{ id: "area-parts", name: "Needs Parts", color: "#f59e0b" }];
    const PARKED = { ...INTAKE, id: "job-3", holding_area_id: "area-parts", scheduled_at: at(OTHER, 9).toISOString() };
    boardRows = [PARKED];
    const w = await mountBoard();
    w.vm.onDragStart(PARKED, null, null, "area:area-parts");
    await w.find('[data-testid="week-area-area-parts"]').trigger("drop");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("takes the tech off a parked job dragged from its tech's row onto its own lane", async () => {
    areas = [{ id: "area-parts", name: "Needs Parts", color: "#f59e0b" }];
    const PARKED_TECH = { ...JOB, id: "job-4", customer_name: "Ray Fox", holding_area_id: "area-parts" };
    boardRows = [PARKED_TECH];
    const w = await mountBoard();
    expect(cell(w, "tech-1", TODAY).text()).toContain("Ray Fox");
    expect(w.find('[data-testid="week-area-job-area-parts-job-4"]').exists()).toBe(true);
    w.vm.onDragStart(PARKED_TECH, null, "tech-1");
    await w.find('[data-testid="week-area-area-parts"]').trigger("drop");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-4", { assigned_to: null, holding_area_id: "area-parts" });
  });

  it("re-reads the board when the server refuses the unschedule", async () => {
    boardRows = [JOB];
    const w = await mountBoard();
    patchMock.mockRejectedValueOnce(new Error("409"));
    const jobReads = () => getMock.mock.calls.filter(([u]) => String(u).startsWith("/api/jobs")).length;
    const before = jobReads();
    w.vm.onDragStart(JOB, null);
    await w.find('[data-testid="week-queue"]').trigger("drop");
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(jobReads()).toBeGreaterThan(before);
    expect(cell(w, "tech-1", TODAY).text()).toContain("Lee Park");
  });

  it("knows where a chip was picked up from the chip's own dragstart", async () => {
    areas = [{ id: "area-parts", name: "Needs Parts", color: "#f59e0b" }];
    const PARKED = { ...INTAKE, id: "job-3", holding_area_id: "area-parts", scheduled_at: at(OTHER, 9).toISOString() };
    boardRows = [INTAKE, PARKED];
    const w = await mountBoard();
    const dataTransfer = { setData: () => {}, getData: () => "", effectAllowed: "" };
    const strip = w.find('[data-testid="week-queue"]');
    const lane = w.find('[data-testid="week-area-area-parts"]');

    await w.find('[data-testid="week-queue-job-2"]').trigger("dragstart", { dataTransfer });
    await strip.trigger("drop");
    await w.find('[data-testid="week-area-job-area-parts-job-3"]').trigger("dragstart", { dataTransfer });
    await lane.trigger("drop");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();

    // Picked up from the strip, let go over a lane: that is a real move.
    await w.find('[data-testid="week-queue-job-2"]').trigger("dragstart", { dataTransfer });
    await lane.trigger("drop");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-2", { assigned_to: null, holding_area_id: "area-parts" });
  });
});
