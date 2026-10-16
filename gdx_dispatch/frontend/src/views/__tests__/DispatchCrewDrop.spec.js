/**
 * DispatchView — a job with a crew of two or more, dropped in day or week view.
 *
 * Doug, 2026-10-15: one assigned_tech_id replaces the whole crew on the
 * server, and the day view checked a drop against the lead tech only, so
 * moving a two-tech job along the helper's column took the lead off it, and
 * dropping a crew job on any other tech replaced the crew with that one tech.
 *
 * Pinned:
 *  1. Day view: a time move in any crew member's column is a date-only PATCH,
 *     with or without a duration on the job.
 *  2. A crew job dropped on a tech outside the crew asks: swap one member
 *     (the column it came from first) or add the tech. Nothing is sent
 *     before the answer, and Cancel sends nothing.
 *  3. Swap sends the whole new crew; swapping the lead makes the new tech lead.
 *     Add keeps the lead and appends the tech. Week view asks the same.
 *  4. The Reassign dialog confirmed on a crew member leaves the crew alone.
 *  5. A one-tech job dropped on another tech is still a plain reassignment.
 *  6. Swap or Add on a crew job with no duration asks the duration question
 *     a one-tech drop asks, and sends the hours in the same crew PATCH; Skip
 *     sends the crew without hours; closing the question moves nothing.
 *  7. The queue card's assign dropdown on an undated crew job asks the same
 *     question, and dates the job to the board day as a one-tech pick does.
 *  8. Dropping a crew job into the New Jobs queue or a holding area takes
 *     every tech off it, so it asks first; a one-tech drop does not ask.
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
const confirmRequire = vi.fn();
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: confirmRequire }) }));
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
const OTHER = new Date().getDay() === 6 ? -1 : 1;
const OTHER_DAY = keyOf(at(OTHER, 0));
const wallOf = (iso) => { const d = new Date(iso); return [keyOf(d), d.getHours(), d.getMinutes()]; };

const CREW = {
  id: "job-1", title: "Double door install", job_type: "Install", status: "Scheduled",
  customer_id: "c1", customer_name: "Lee Park", assigned_to: "tech-1",
  assigned_tech_ids: ["tech-1", "tech-2"], technician_id: "tech-1", lead_tech_id: "tech-1",
  scheduled_at: at(0, 10, 30).toISOString(), holding_area_id: null,
  scheduled_duration_hours: 2, effective_duration_hours: 2,
};
const SOLO = {
  ...CREW, id: "job-4", customer_name: "Sam Diaz", assigned_to: "tech-1",
  assigned_tech_ids: ["tech-1"], scheduled_at: at(0, 14).toISOString(),
};

let boardRows = [];

async function mountBoard(viewMode = "day") {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/visits")) return Promise.resolve({ items: [], job_ids: [], timezone: null });
    if (u.includes("/api/dispatch/")) return Promise.resolve({ items: [] });
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
      { id: "tech-2", name: "Ana", active: true, effective_shift_start: "07:30", effective_shift_end: "16:00" },
      { id: "tech-3", name: "Raj", active: true, effective_shift_start: "08:00", effective_shift_end: "17:00" },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve([]);
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
const prompt = (w) => w.find('[data-testid="crew-change-prompt"]');
const jobPatches = () => patchMock.mock.calls.filter(([u]) => u === "/api/jobs/job-1");

// Pick the card up from one tech's column and drop it on another's at 1 PM.
async function dayDrop(w, job, fromTech, toTech, hour = 13) {
  timeline(w, fromTech).vm.$emit("job-drag-start", job, null);
  const done = timeline(w, toTech).vm.$emit("place", { jobId: job.id, techId: toTech, startISO: at(0, hour).toISOString() });
  await flushAll();
  return done;
}

describe("DispatchView — crew-safe drops", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    patchMock.mockResolvedValue({});
    granted = new Set(["jobs.read_all"]);
    boardRows = [CREW, SOLO];
  });

  it("moves a crew job's time along the helper's column without touching the crew", async () => {
    const NO_EST = { ...CREW, id: "job-3", scheduled_duration_hours: null, effective_duration_hours: null };
    boardRows = [CREW, NO_EST];
    const w = await mountBoard();
    await dayDrop(w, CREW, "tech-2", "tech-2");
    await dayDrop(w, NO_EST, "tech-1", "tech-1", 15);
    expect(w.find('[data-testid="dispatch-duration-prompt"]').exists()).toBe(false);
    expect(prompt(w).exists()).toBe(false);
    expect(patchMock.mock.calls.map(([u, b]) => [u, Object.keys(b)])).toEqual([
      ["/api/jobs/job-1", ["scheduled_at"]],
      ["/api/jobs/job-3", ["scheduled_at"]],
    ]);
    expect(wallOf(patchMock.mock.calls[0][1].scheduled_at).slice(1)).toEqual([13, 0]);
  });

  it("asks before changing a crew, offering the column it came from first", async () => {
    const w = await mountBoard();
    await dayDrop(w, CREW, "tech-2", "tech-3");
    expect(prompt(w).exists()).toBe(true);
    expect(patchMock).not.toHaveBeenCalled();
    const labels = prompt(w).findAll("button").map((b) => b.text());
    expect(labels).toEqual(["Swap Ana for Raj", "Swap Mike for Raj", "Add Raj to the crew", "Cancel"]);
    await w.find('[data-testid="crew-change-swap-tech-2"]').trigger("click");
    await flushAll();
    expect(jobPatches()).toHaveLength(1);
    const body = jobPatches()[0][1];
    expect(body).toMatchObject({ assigned_tech_ids: ["tech-1", "tech-3"], lead_tech_id: "tech-1" });
    expect(body).not.toHaveProperty("assigned_tech_id");
    expect(body).not.toHaveProperty("assigned_to");
    expect(wallOf(body.scheduled_at).slice(1)).toEqual([13, 0]);
  });

  it("makes the new tech lead when the lead is swapped out", async () => {
    const w = await mountBoard();
    await dayDrop(w, CREW, "tech-1", "tech-3");
    await w.find('[data-testid="crew-change-swap-tech-1"]').trigger("click");
    await flushAll();
    expect(jobPatches()[0][1]).toMatchObject({ assigned_tech_ids: ["tech-3", "tech-2"], lead_tech_id: "tech-3" });
  });

  it("moves nothing when the crew question is cancelled", async () => {
    const w = await mountBoard();
    await dayDrop(w, CREW, "tech-2", "tech-3");
    await w.find('[data-testid="crew-change-cancel"]').trigger("click");
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(prompt(w).exists()).toBe(false);
  });

  it("asks the same in week view, and Add keeps the crew and its lead", async () => {
    const w = await mountBoard("week");
    // Picked up from Ana's row through the template, so her swap comes first.
    await w.find(`[data-testid="week-cell-tech-2-${keyOf(at(0, 0))}"] [data-testid$="-job-1"]`).trigger("dragstart");
    await w.find(`[data-testid="week-cell-tech-3-${OTHER_DAY}"]`).trigger("drop");
    await flushAll();
    expect(prompt(w).exists()).toBe(true);
    expect(patchMock).not.toHaveBeenCalled();
    expect(prompt(w).findAll("button").map((b) => b.text())[0]).toBe("Swap Ana for Raj");
    await w.find('[data-testid="crew-change-add"]').trigger("click");
    await flushAll();
    const body = jobPatches()[0][1];
    expect(body).toMatchObject({ assigned_tech_ids: ["tech-1", "tech-2", "tech-3"], lead_tech_id: "tech-1" });
    expect(wallOf(body.scheduled_at)).toEqual([OTHER_DAY, 10, 30]);
  });

  it("leaves the crew alone when Reassign is confirmed on a crew member", async () => {
    const w = await mountBoard();
    const s = w.vm.$.setupState;
    s.openReassignDialog(CREW);
    expect(s.assignDialogTechId).toBe("tech-1");
    s.confirmAssign();
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("still reassigns a one-tech job without asking", async () => {
    const w = await mountBoard();
    timeline(w, "tech-1").vm.$emit("job-drag-start", SOLO, null);
    timeline(w, "tech-3").vm.$emit("place", { jobId: "job-4", techId: "tech-3", startISO: at(0, 15).toISOString() });
    await flushAll();
    expect(prompt(w).exists()).toBe(false);
    const call = patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-4");
    expect(call[1]).toMatchObject({ assigned_tech_id: "tech-3", technician_id: "tech-3" });
  });

  describe("the queue card's assign dropdown on an undated crew job", () => {
    const UNDATED = { ...CREW, id: "job-9", scheduled_at: null };
    const pick = (w, techId) => w.findAllComponents(stubs.Select)
      .find((c) => c.attributes("data-testid") === "assign-dropdown-job-9")
      .vm.$emit("change", { value: techId });

    it("asks before changing the crew, and Add dates it to the board day", async () => {
      boardRows = [UNDATED, SOLO];
      const w = await mountBoard();
      pick(w, "tech-3");
      await flushAll();
      expect(prompt(w).exists()).toBe(true);
      expect(patchMock).not.toHaveBeenCalled();
      await w.find('[data-testid="crew-change-add"]').trigger("click");
      await flushAll();
      const call = patchMock.mock.calls.find(([u]) => u === "/api/jobs/job-9");
      expect(call[1]).toMatchObject({ assigned_tech_ids: ["tech-1", "tech-2", "tech-3"], lead_tech_id: "tech-1" });
      expect(call[1]).not.toHaveProperty("assigned_tech_id");
      expect(wallOf(call[1].scheduled_at)).toEqual([keyOf(at(0, 0)), 0, 0]);
    });

    it("picking a crew member on a job with hours only dates it", async () => {
      boardRows = [UNDATED, SOLO];
      const w = await mountBoard();
      pick(w, "tech-2");
      await flushAll();
      expect(prompt(w).exists()).toBe(false);
      expect(patchMock.mock.calls.map(([u, b]) => [u, Object.keys(b)])).toEqual([["/api/jobs/job-9", ["scheduled_at"]]]);
    });

    it("picking a crew member on a job with no hours asks for them before dating it", async () => {
      boardRows = [{ ...UNDATED, scheduled_duration_hours: null, effective_duration_hours: null }, SOLO];
      const w = await mountBoard();
      pick(w, "tech-2");
      await flushAll();
      expect(w.find('[data-testid="dispatch-duration-prompt"]').exists()).toBe(true);
      expect(patchMock).not.toHaveBeenCalled();
      w.vm.$.setupState.durationPromptHours = 2;
      await flushAll();
      await w.find('[data-testid="dispatch-duration-prompt-save"]').trigger("click");
      await flushAll();
      expect(patchMock).toHaveBeenCalledTimes(1);
      expect(Object.keys(patchMock.mock.calls[0][1]).sort()).toEqual(["scheduled_at", "scheduled_duration_hours"]);
      expect(patchMock.mock.calls[0][1].scheduled_duration_hours).toBe(2);
    });

    it("clears the card's picker after a cancelled pick", async () => {
      boardRows = [UNDATED, SOLO];
      const w = await mountBoard();
      const before = w.vm.$.setupState.queuePickerKey;
      pick(w, "tech-3");
      await flushAll();
      await w.find('[data-testid="crew-change-cancel"]').trigger("click");
      await flushAll();
      expect(patchMock).not.toHaveBeenCalled();
      expect(w.vm.$.setupState.queuePickerKey).toBe(before + 1);
    });
  });

  describe("a crew job with no duration", () => {
    const NO_EST = { ...CREW, scheduled_duration_hours: null, effective_duration_hours: null };
    const durationPrompt = (w) => w.find('[data-testid="dispatch-duration-prompt"]');

    async function swapIn(w) {
      await dayDrop(w, NO_EST, "tech-2", "tech-3");
      await w.find('[data-testid="crew-change-swap-tech-2"]').trigger("click");
      await flushAll();
    }

    it("asks for the hours after Swap and sends them with the crew", async () => {
      boardRows = [NO_EST, SOLO];
      const w = await mountBoard();
      await swapIn(w);
      expect(durationPrompt(w).exists()).toBe(true);
      expect(patchMock).not.toHaveBeenCalled();
      w.vm.$.setupState.durationPromptHours = 3;
      await flushAll();
      await w.find('[data-testid="dispatch-duration-prompt-save"]').trigger("click");
      await flushAll();
      expect(patchMock).toHaveBeenCalledTimes(1);
      expect(jobPatches()[0][1]).toMatchObject({
        assigned_tech_ids: ["tech-1", "tech-3"], lead_tech_id: "tech-1", scheduled_duration_hours: 3,
      });
    });

    it("sends the crew without hours on Skip", async () => {
      boardRows = [NO_EST, SOLO];
      const w = await mountBoard();
      await swapIn(w);
      await w.find('[data-testid="dispatch-duration-prompt-skip"]').trigger("click");
      await flushAll();
      expect(patchMock).toHaveBeenCalledTimes(1);
      const body = jobPatches()[0][1];
      expect(body).toMatchObject({ assigned_tech_ids: ["tech-1", "tech-3"] });
      expect(body).not.toHaveProperty("scheduled_duration_hours");
    });

    it("moves nothing when the duration question is closed", async () => {
      boardRows = [NO_EST, SOLO];
      const w = await mountBoard();
      await swapIn(w);
      // The Dialog's close (X / Esc / mask) ends in @hide.
      w.vm.$.setupState.durationPromptOpen = false;
      w.vm.$.setupState.onDurationPromptHide();
      await flushAll();
      expect(patchMock).not.toHaveBeenCalled();
    });
  });
});

describe("dropping a crew job off the board", () => {
  const drop = { dataTransfer: { getData: () => "job-1" } };
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset(); confirmRequire.mockReset();
    patchMock.mockResolvedValue({});
    granted = new Set(["jobs.read_all"]);
  });

  it("into the New Jobs queue asks first, naming the crew, then clears it", async () => {
    boardRows = [CREW];
    confirmRequire.mockImplementation((o) => o.accept());
    const w = await mountBoard();
    patchMock.mockClear();
    await w.vm.$.setupState.moveToScheduleQueue(drop);
    await flushAll();
    expect(confirmRequire).toHaveBeenCalledWith(expect.objectContaining({
      header: "Remove every technician?",
      message: expect.stringContaining("Mike, Ana"),
    }));
    expect(jobPatches()).toEqual([["/api/jobs/job-1", expect.objectContaining({ assigned_to: null })]]);
  });

  it("into the New Jobs queue, saying no sends nothing", async () => {
    boardRows = [CREW];
    confirmRequire.mockImplementation((o) => o.reject());
    const w = await mountBoard();
    patchMock.mockClear();
    await w.vm.$.setupState.moveToScheduleQueue(drop);
    await flushAll();
    expect(confirmRequire).toHaveBeenCalledTimes(1);
    expect(jobPatches()).toEqual([]);
  });

  it("into a holding area, saying no sends nothing", async () => {
    boardRows = [CREW];
    confirmRequire.mockImplementation((o) => o.reject());
    const w = await mountBoard();
    patchMock.mockClear();
    await w.vm.$.setupState.moveToHoldingArea(drop, "ha-1");
    await flushAll();
    expect(confirmRequire).toHaveBeenCalledTimes(1);
    expect(jobPatches()).toEqual([]);
  });

  it("into a holding area, yes clears the crew on the card too, so it is not asked about again", async () => {
    boardRows = [CREW];
    confirmRequire.mockImplementation((o) => o.accept());
    patchMock.mockImplementation(() => new Promise(() => {})); // the board shows the drop before the server answers
    const w = await mountBoard();
    patchMock.mockClear();
    w.vm.$.setupState.moveToHoldingArea(drop, "ha-1");
    await flushAll();
    expect(jobPatches()).toEqual([["/api/jobs/job-1", { assigned_to: null, holding_area_id: "ha-1" }]]);
    const card = w.vm.$.setupState.jobs.find((j) => j.id === "job-1");
    expect(card.assigned_tech_ids).toEqual([]);
  });

  it("a one-tech job drops into the queue without asking", async () => {
    boardRows = [SOLO];
    const w = await mountBoard();
    patchMock.mockClear();
    await w.vm.$.setupState.moveToScheduleQueue({ dataTransfer: { getData: () => "job-4" } });
    await flushAll();
    expect(confirmRequire).not.toHaveBeenCalled();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-4", expect.objectContaining({ assigned_to: null }));
  });
});
