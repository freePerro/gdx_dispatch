/**
 * DispatchView — a job booked from a drop that runs past the end of the
 * dropped-on tech's shift (Doug, 2026-10-18: "if a job is more than the
 * available time slot in a day it should ask if there should be another
 * appointment the next day", and "maybe it's worth adding another tech").
 *
 * Pinned:
 *  1. A booking that fits what is left of the shift asks nothing more and
 *     sends no visit length.
 *  2. One that does not opens "This job runs past the end of the day" after
 *     "Schedule this job", naming the hours and what is left. Nothing is sent
 *     before the answer, and Cancel sends nothing.
 *  3. "Book N days" sends the job PATCH with day one's length to shift end,
 *     then books the rest on the tech's next workdays at shift start through
 *     POST /api/jobs/{id}/visits, for the whole crew.
 *  4. Adding a tech shares the man-hours; once it fits, Schedule sends the
 *     crew and the new hours, and books no other day.
 *  5. "Keep it all on" books the one day at full length.
 *  6. A refused later day leaves day one booked and says so.
 *  7. A job that already has a date gets the same question on every path
 *     that puts it in a tech's day (Doug, 2026-10-18): a move along its own
 *     tech's timeline, a reassign, a crew member's column, the tray, the
 *     week grid and the tech picker. Cancel writes nothing; a split sends
 *     day one's length, which the server sets on the visit it moves.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";

const getMock = vi.fn();
const patchMock = vi.fn();
const postMock = vi.fn();
const toastAdd = vi.fn();
let granted = new Set();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
const confirmRequire = vi.fn();
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: confirmRequire }) }));
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
const OTHER = new Date().getDay() === 6 ? -1 : 1;
const OTHER_DAY = keyOf(at(OTHER, 0));
const wallOf = (iso) => { const d = new Date(iso); return [keyOf(d), d.getHours(), d.getMinutes()]; };

const DAY_PATH = "/api/jobs/job-7";
const UNDATED = {
  id: "job-7", title: "Two-door install", job_type: "Install", status: "Service Call",
  customer_id: "c1", customer_name: "Lee Park", assigned_to: null,
  assigned_tech_ids: [], technician_id: null, lead_tech_id: null,
  scheduled_at: null, holding_area_id: null,
  scheduled_duration_hours: 14, effective_duration_hours: 14,
};

let boardRows = [];
let areaRows = [];
let partialRows = [];

async function mountBoard(viewMode = "day") {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/visits")) return Promise.resolve({ items: [], job_ids: [], timezone: null });
    if (u.includes("/api/dispatch/partial-jobs")) return Promise.resolve({ items: partialRows });
    if (u.includes("/api/dispatch/")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
      { id: "tech-2", name: "Ana", active: true, effective_shift_start: "07:30", effective_shift_end: "16:00" },
      { id: "tech-3", name: "Raj", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve(areaRows);
    if (u.includes("/api/jobs")) return Promise.resolve(boardRows);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  if (viewMode !== "day") {
    w.vm.$.setupState.viewMode = viewMode;
    await flushAll();
  }
  return w;
}

const timeline = (w, techId) => w.findAllComponents(TechTimelineColumn).find((c) => String(c.props("tech")?.id) === techId);
const overflow = (w) => w.find('[data-testid="day-overflow-prompt"]');
const jobPatches = () => patchMock.mock.calls.filter(([u]) => u === DAY_PATH);
const visitPosts = () => postMock.mock.calls.filter(([u]) => u === `${DAY_PATH}/visits`);

// The next Monday–Friday day after `offset` days from today, as "YYYY-MM-DD".
function nextWeekday(offset) {
  const d = at(offset, 0);
  do { d.setDate(d.getDate() + 1); } while (d.getDay() === 0 || d.getDay() === 6);
  return keyOf(d);
}

// Drop the undated job on Mike (08:00–17:00) at `hour` today and answer
// "Schedule this job" as it stands.
async function bookAt(w, hour) {
  timeline(w, "tech-1").vm.$emit("place", { jobId: "job-7", techId: "tech-1", startISO: at(0, hour).toISOString() });
  await flushAll();
  await w.find('[data-testid="schedule-drop-confirm"]').trigger("click");
  await flushAll();
}
const click = async (w, id) => { await w.find(`[data-testid="${id}"]`).trigger("click"); await flushAll(); };

describe("DispatchView — a booking longer than the tech's day", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    granted = new Set(["dispatch.write", "jobs.write"]);
    patchMock.mockReset().mockResolvedValue({});
    postMock.mockReset().mockResolvedValue({ items: [] });
    toastAdd.mockReset();
    areaRows = [];
    partialRows = [];
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("books a job that fits with no second question and no visit length", async () => {
    boardRows = [{ ...UNDATED, scheduled_duration_hours: 4, effective_duration_hours: 4 }];
    const w = await mountBoard();
    await bookAt(w, 8);
    expect(overflow(w).exists()).toBe(false);
    expect(jobPatches()).toHaveLength(1);
    expect(jobPatches()[0][1]).not.toHaveProperty("visit_duration_minutes");
    expect(visitPosts()).toHaveLength(0);
  });

  it("asks before writing anything, and Cancel writes nothing", async () => {
    boardRows = [UNDATED];
    const w = await mountBoard();
    await bookAt(w, 8);
    expect(overflow(w).exists()).toBe(true);
    expect(overflow(w).text()).toContain("needs 14 h");
    expect(overflow(w).text()).toContain("Mike has 9 h left");
    expect(patchMock).not.toHaveBeenCalled();
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalled();
  });

  it("measures from the drop time: 4 h at 2 PM does not fit a 5 PM shift end", async () => {
    boardRows = [{ ...UNDATED, scheduled_duration_hours: 4, effective_duration_hours: 4 }];
    const w = await mountBoard();
    await bookAt(w, 14);
    expect(overflow(w).text()).toContain("Mike has 3 h left");
  });

  it("a drop at shift end offers no split and says why", async () => {
    boardRows = [UNDATED];
    const w = await mountBoard();
    await bookAt(w, 17);
    expect(overflow(w).text()).toContain("Mike has 0 h left");
    expect(w.find('[data-testid="day-overflow-split"]').exists()).toBe(false);
    expect(w.find('[data-testid="day-overflow-no-split"]').text()).toContain("at or after the end of Mike's shift");
  });

  it("Book 2 days: day one to shift end, the rest on Mike's next workday at shift start", async () => {
    boardRows = [UNDATED];
    const w = await mountBoard();
    await bookAt(w, 8);
    await click(w, "day-overflow-split");
    expect(jobPatches()).toHaveLength(1);
    expect(jobPatches()[0][1]).toMatchObject({
      assigned_tech_ids: ["tech-1"], lead_tech_id: "tech-1", visit_duration_minutes: 540,
    });
    expect(jobPatches()[0][1]).not.toHaveProperty("scheduled_duration_hours");
    expect(visitPosts()).toHaveLength(1);
    expect(visitPosts()[0][1]).toEqual({
      days: [nextWeekday(0)], start_time: "08:00", duration_minutes: 300, tech_ids: ["tech-1"],
    });
  });

  it("adding a tech shares the hours; once it fits, Schedule books one day for both", async () => {
    boardRows = [UNDATED];
    const w = await mountBoard();
    await bookAt(w, 8);
    w.findAllComponents(stubs.MultiSelect)
      .find((c) => c.attributes("data-testid") === "day-overflow-techs")
      .vm.$emit("update:modelValue", ["tech-1", "tech-3"]);
    await flushAll();
    expect(overflow(w).text()).toContain("about 7 h");
    await click(w, "day-overflow-confirm");
    expect(jobPatches()[0][1]).toMatchObject({
      assigned_tech_ids: ["tech-1", "tech-3"], lead_tech_id: "tech-1", scheduled_duration_hours: 7,
    });
    expect(jobPatches()[0][1]).not.toHaveProperty("visit_duration_minutes");
    expect(visitPosts()).toHaveLength(0);
  });

  it("Keep it all on the day books one visit at full length", async () => {
    boardRows = [UNDATED];
    const w = await mountBoard();
    await bookAt(w, 8);
    await click(w, "day-overflow-one-day");
    expect(jobPatches()).toHaveLength(1);
    expect(jobPatches()[0][1]).not.toHaveProperty("visit_duration_minutes");
    expect(visitPosts()).toHaveLength(0);
  });

  it("a refused later day keeps day one booked and says so", async () => {
    boardRows = [UNDATED];
    postMock.mockRejectedValue(Object.assign(new Error("A technician already has a visit of this job."), {
      body: { code: "double_booked", detail: "A technician already has a visit of this job." },
    }));
    const w = await mountBoard();
    await bookAt(w, 8);
    await click(w, "day-overflow-split");
    expect(jobPatches()).toHaveLength(1);
    const warn = toastAdd.mock.calls.map(([t]) => t).find((t) => t.severity === "warn");
    expect(warn.summary).toContain("Only part of Lee Park");
    expect(warn.detail).toContain("Visits card");
  });

  it("books no later day when the job PATCH fails", async () => {
    boardRows = [UNDATED];
    patchMock.mockRejectedValue(new Error("nope"));
    const w = await mountBoard();
    await bookAt(w, 8);
    await click(w, "day-overflow-split");
    expect(visitPosts()).toHaveLength(0);
  });
});

const DATED = {
  ...UNDATED, status: "Scheduled", lifecycle_stage: "scheduled",
  assigned_to: "tech-1", technician_id: "tech-1", assigned_tech_ids: ["tech-1"], lead_tech_id: "tech-1",
  scheduled_at: at(0, 8).toISOString(), scheduled_duration_hours: 8, effective_duration_hours: 8,
};
const place = async (w, techId, h, m = 0) => {
  timeline(w, techId).vm.$emit("place", { jobId: "job-7", techId, startISO: at(0, h, m).toISOString() });
  await flushAll();
};

describe("DispatchView — a dated job put in a tech's day", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    granted = new Set(["dispatch.write", "jobs.write"]);
    patchMock.mockReset().mockResolvedValue({});
    postMock.mockReset().mockResolvedValue({ items: [] });
    toastAdd.mockReset();
    areaRows = [];
    partialRows = [];
  });
  afterEach(() => { vi.restoreAllMocks(); });

  it("an 8 h block moved to 3:45 PM on its own tech asks first, and Cancel writes nothing", async () => {
    boardRows = [DATED];
    const w = await mountBoard();
    await place(w, "tech-1", 15, 45);
    expect(overflow(w).exists()).toBe(true);
    expect(overflow(w).text()).toContain("needs 8 h");
    expect(patchMock).not.toHaveBeenCalled();
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalled();
  });

  it("the split moves the block with day one's length and books the rest", async () => {
    boardRows = [DATED];
    const w = await mountBoard();
    await place(w, "tech-1", 15, 45);
    await click(w, "day-overflow-split");
    expect(jobPatches()).toHaveLength(1);
    const body = jobPatches()[0][1];
    expect(wallOf(body.scheduled_at)).toEqual([keyOf(at(0, 0)), 15, 45]);
    expect(body.visit_duration_minutes).toBe(75);
    expect(body).not.toHaveProperty("scheduled_duration_hours");
    expect(body).not.toHaveProperty("assigned_tech_ids");
    expect(visitPosts()).toEqual([[`${DAY_PATH}/visits`, {
      days: [nextWeekday(0)], start_time: "08:00", duration_minutes: 405, tech_ids: ["tech-1"],
    }, { suppressErrorToast: true }]]);
  });

  it("a move that still fits asks nothing and sends no visit length", async () => {
    boardRows = [DATED];
    const w = await mountBoard();
    await place(w, "tech-1", 9);
    expect(overflow(w).exists()).toBe(false);
    expect(jobPatches()).toHaveLength(1);
    expect(jobPatches()[0][1]).not.toHaveProperty("visit_duration_minutes");
  });

  it("a reassign to another tech is measured against that tech's shift", async () => {
    boardRows = [DATED];
    const w = await mountBoard();
    await place(w, "tech-2", 15);
    expect(overflow(w).text()).toContain("Ana has 1 h left");
    expect(patchMock).not.toHaveBeenCalled();
    await click(w, "day-overflow-split");
    expect(jobPatches()).toHaveLength(1);
    expect(jobPatches()[0][1]).toMatchObject({ assigned_tech_id: "tech-2", visit_duration_minutes: 60 });
    expect(visitPosts()[0][1]).toMatchObject({ start_time: "07:30", duration_minutes: 420, tech_ids: ["tech-2"] });
  });

  it("a crew job moved along a crew member's column asks, and books the rest for the whole crew", async () => {
    boardRows = [{ ...DATED, assigned_tech_ids: ["tech-1", "tech-3"] }];
    const w = await mountBoard();
    await place(w, "tech-3", 15, 45);
    expect(overflow(w).exists()).toBe(true);
    expect(patchMock).not.toHaveBeenCalled();
    await click(w, "day-overflow-split");
    expect(jobPatches()[0][1]).toMatchObject({ visit_duration_minutes: 75 });
    expect(jobPatches()[0][1]).not.toHaveProperty("assigned_tech_ids");
    expect(visitPosts()[0][1].tech_ids).toEqual(["tech-1", "tech-3"]);
  });

  it("the tray measures the whole shift", async () => {
    boardRows = [{ ...DATED, scheduled_duration_hours: 10, effective_duration_hours: 10 }];
    const w = await mountBoard();
    timeline(w, "tech-2").vm.$emit("place-tray", { jobId: "job-7", techId: "tech-2" });
    await flushAll();
    expect(overflow(w).text()).toContain("Ana has 8.5 h left");
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("a week-grid move on its own tech's row asks before re-dating", async () => {
    boardRows = [{ ...DATED, scheduled_duration_hours: 10, effective_duration_hours: 10 }];
    const w = await mountBoard();
    const event = { dataTransfer: { getData: () => "job-7" } };
    w.vm.$.setupState.onWeekCellDrop(event, "tech-1", OTHER_DAY);
    await flushAll();
    expect(overflow(w).text()).toContain("Mike has 9 h left");
    expect(patchMock).not.toHaveBeenCalled();
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("a tech picked from the dropdown keeps the job's time and is measured from it", async () => {
    boardRows = [{ ...DATED, scheduled_at: at(0, 15).toISOString() }];
    const w = await mountBoard();
    w.vm.$.setupState.assignFromPicker("job-7", "tech-2");
    await flushAll();
    expect(overflow(w).text()).toContain("Ana has 1 h left");
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("re-picking the tech a job already has asks nothing", async () => {
    boardRows = [{ ...DATED, scheduled_at: at(0, 15).toISOString() }];
    const w = await mountBoard();
    w.vm.$.setupState.assignFromPicker("job-7", "tech-1");
    await flushAll();
    expect(overflow(w).exists()).toBe(false);
  });

  it("hours typed in the duration prompt are measured before either PATCH", async () => {
    boardRows = [{ ...DATED, scheduled_duration_hours: null, effective_duration_hours: null }];
    const w = await mountBoard();
    await place(w, "tech-2", 15);
    const state = w.vm.$.setupState;
    expect(state.durationPromptOpen).toBe(true);
    state.durationPromptHours = "4";
    state.confirmDurationPrompt();
    await flushAll();
    expect(overflow(w).text()).toContain("Ana has 1 h left");
    await click(w, "day-overflow-cancel");
    expect(patchMock).not.toHaveBeenCalled();
  });

  it.each([["more than a day's", "30", 16], ["under 15 minutes of", "0.1", 16]])(
    "%s hours kept on one day save the hours and leave the visit its length", async (_, typed, hour) => {
    boardRows = [{ ...DATED, scheduled_duration_hours: null, effective_duration_hours: null }];
    const w = await mountBoard();
    await place(w, "tech-2", hour);
    const state = w.vm.$.setupState;
    state.durationPromptHours = typed;
    state.confirmDurationPrompt();
    await flushAll();
    await click(w, "day-overflow-one-day");
    const bodies = jobPatches().map(([, b]) => b);
    expect(bodies).toContainEqual(expect.objectContaining({ scheduled_duration_hours: Number(typed) }));
    expect(bodies).toContainEqual(expect.objectContaining({ assigned_tech_id: "tech-2" }));
    for (const b of bodies) expect(b).not.toHaveProperty("visit_duration_minutes");
  });

  it("a job under way is not asked: its crew is not the dialog's to change", async () => {
    boardRows = [{ ...DATED, lifecycle_stage: "in_progress" }];
    const w = await mountBoard();
    await place(w, "tech-1", 15, 45);
    expect(overflow(w).exists()).toBe(false);
    expect(jobPatches()).toHaveLength(1);
  });
});
