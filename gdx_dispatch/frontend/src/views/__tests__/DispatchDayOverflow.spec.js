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
