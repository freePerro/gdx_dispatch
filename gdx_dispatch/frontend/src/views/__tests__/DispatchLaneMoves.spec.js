/**
 * DispatchView — holding lanes without a drag, and Undo (Doug, 2026-10-16:
 * the day board's lanes were "too far apart to grab and put on another
 * lane"). Mounts the REAL DispatchView with the app's leaf components stubbed.
 *
 * Contract pinned:
 *  1. Lanes are a compact strip on the day board: a card shows customer, job
 *     and hours, with no repeated lane tag.
 *  2. A card's Move to… lists every other lane, not its own, and Release.
 *  3. Move to… parks the job exactly as a drop does (tech off, date kept).
 *  4. A move or Release offers Undo; Undo writes back the lane, and the crew
 *     the move took off.
 *  5. A failed move offers no Undo.
 *  6. A New Jobs card has Move to… with the lanes and no Release.
 *  7. The drawer names the lane and carries Move to….
 *  8. The week board's lane chip shows the job's hours, not its date.
 *  9. A job the board has not loaded (the red lane's, dated another day)
 *     is not parked where no lane would show it, and the board says why;
 *     one day of a multi-day job is offered no lane move it would refuse;
 *     Undo after "To schedule" restores date and crew.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";
import { formatDurationHours } from "../../utils/hours";

const getMock = vi.fn();
const patchMock = vi.fn();
const toastAdd = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
// The office answers Remove to "Remove every technician?".
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: (o) => o.accept() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn(), patch: patchMock, del: vi.fn() }),
}));
vi.mock("../../composables/usePermission", async () => {
  const { ref } = await import("vue");
  return {
    usePermission: () => ({
      hasPermission: () => false,
      permissions: ref([]),
      permissionsLoaded: ref(true),
      reloadPermissions: () => Promise.resolve(),
    }),
  };
});

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
  TechTimelineColumn: { props: ["tech"], template: "<div />" },
  TechEfficiencyPanel: { template: "<div />" },
  JobStateChip: { template: "<span class='state-chip' />" },
  DataTable: { template: "<div />" },
  Column: { template: "<div />" },
  MobileJobCloseoutDialog: { template: "<div />" },
  // The board's one Move to… menu, drawn open: every item is a button.
  Menu: {
    props: ["model"],
    methods: { toggle() {} },
    template: `<div data-testid="lane-menu"><template v-for="g in model" :key="g.label || 'sep'">
      <button v-if="g.command" @click="g.command()">{{ g.label }}</button>
      <template v-for="i in g.items || []" :key="i.label">
        <button v-if="i.command" @click="i.command()">{{ i.label }}</button><span v-else>{{ i.label }}</span>
      </template>
    </template></div>`,
  },
};

const flushAll = async () => { await flushPromises(); await flushPromises(); };
const today9 = () => { const d = new Date(); d.setHours(9, 0, 0, 0); return d.toISOString(); };

const AREAS = [
  { id: "ha-doors", name: "Order Doors", color: "#3b82f6" },
  { id: "ha-wait", name: "Waiting on doors", color: "#f59e0b" },
  { id: "ha-rts", name: "Ready to Schedule", color: "#22c55e" },
];

const job = (over = {}) => ({
  id: "job-1", job_number: "JOB-1", title: "16x7 steel door", job_type: "Install", status: "Scheduled",
  customer_id: "c1", customer_name: "Dana Ruiz", scheduled_at: today9(), assigned_to: null,
  holding_area_id: "ha-doors", scheduled_duration_hours: 3, effective_duration_hours: 3, ...over,
});

let boardJobs = [];
let redLane = [];

async function mountBoard() {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true }, { id: "tech-2", name: "Ana", active: true },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve(AREAS);
    if (u.includes("/api/dispatch-settings")) return Promise.resolve({ dispatch_show_unassigned_lane: redLane.length > 0 });
    if (u.includes("/api/dispatch/scheduled-unassigned")) return Promise.resolve({ items: redLane });
    if (u.includes("/api/dispatch/")) return Promise.resolve({ items: [] });
    if (u.includes("/api/jobs")) return Promise.resolve(boardJobs);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  return w;
}

const menuLabels = (w) => w.get('[data-testid="lane-menu"]').findAll("button").map((b) => b.text());
async function pick(w, jobId, label) {
  await w.get(`[data-testid="lane-job-menu-${jobId}"]`).trigger("click");
  const item = w.get('[data-testid="lane-menu"]').findAll("button").find((b) => b.text() === label);
  expect(item, `no "${label}" in ${menuLabels(w)}`).toBeTruthy();
  await item.trigger("click");
  await flushAll();
}

describe("DispatchView — holding lanes without a drag", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    patchMock.mockResolvedValue({});
    boardJobs = [];
    redLane = [];
  });

  it("draws a lane card as customer, job and hours, in a strip above the board", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    const strip = w.get('[data-testid="lane-strip"]');
    const card = strip.get('[data-testid="lane-job-ha-doors-job-1"]');
    expect(card.text()).toContain("Dana Ruiz");
    expect(card.text()).toContain("16x7 steel door");
    expect(card.text()).toContain(formatDurationHours(3));
    expect(card.text()).not.toContain("Order Doors");
    // "Ready to Schedule" is the New Jobs queue, not a lane.
    expect(w.find('[data-testid="lane-ha-rts"]').exists()).toBe(false);
  });

  it("offers every other lane and Release, never the lane the job is in", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    await w.get('[data-testid="lane-job-menu-job-1"]').trigger("click");
    expect(menuLabels(w)).toEqual(["Waiting on doors", "Release from Order Doors"]);
  });

  it("Move to… parks the job the way a drop does: the tech comes off, the date stays", async () => {
    boardJobs = [job({ assigned_to: "tech-1" })];
    const w = await mountBoard();
    await pick(w, "job-1", "Waiting on doors");
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_to: null, holding_area_id: "ha-wait" });
    expect(w.find('[data-testid="lane-job-ha-wait-job-1"]').exists()).toBe(true);
    expect(w.find('[data-testid="lane-job-ha-doors-job-1"]').exists()).toBe(false);
  });

  it("Undo after a move puts back the lane and the tech the move took off", async () => {
    boardJobs = [job({ assigned_to: "tech-1" })];
    const w = await mountBoard();
    await pick(w, "job-1", "Waiting on doors");
    expect(w.get('[data-testid="board-undo"]').text()).toContain("Moved to Waiting on doors");
    patchMock.mockClear();
    await w.get('[data-testid="board-undo-button"]').trigger("click");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", {
      holding_area_id: "ha-doors", assigned_tech_ids: ["tech-1"], lead_tech_id: "tech-1",
    });
    expect(w.find('[data-testid="board-undo"]').exists()).toBe(false);
  });

  it("Release offers Undo, and Undo puts the job back in its lane", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    await pick(w, "job-1", "Release from Order Doors");
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { holding_area_id: null });
    expect(w.get('[data-testid="board-undo"]').text()).toContain("Released from Order Doors");
    patchMock.mockClear();
    await w.get('[data-testid="board-undo-button"]').trigger("click");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { holding_area_id: "ha-doors" });
  });

  it("offers no Undo when the move was refused", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    patchMock.mockRejectedValueOnce(new Error("refused"));
    await pick(w, "job-1", "Waiting on doors");
    expect(w.find('[data-testid="board-undo"]').exists()).toBe(false);
  });

  it("a New Jobs card can be parked from its menu, which has no Release", async () => {
    boardJobs = [job({ id: "job-2", holding_area_id: null, scheduled_at: null })];
    const w = await mountBoard();
    expect(w.find('[data-testid="unassigned-job-job-2"]').exists()).toBe(true);
    await w.get('[data-testid="lane-job-menu-job-2"]').trigger("click");
    expect(menuLabels(w)).toEqual(["Order Doors", "Waiting on doors"]);
    await pick(w, "job-2", "Order Doors");
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-2", { assigned_to: null, holding_area_id: "ha-doors" });
  });

  it("the drawer names the job's lane and moves it from there", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    await w.get('[data-testid="lane-job-ha-doors-job-1"]').trigger("click");
    await flushAll();
    expect(w.get('[data-testid="dispatch-job-drawer-lane"]').text()).toContain("Order Doors");
    await w.get('[data-testid="dispatch-job-move"]').trigger("click");
    expect(menuLabels(w)).toContain("Waiting on doors");
  });

  it("a red-lane job dated another day is not parked out of sight, and says why", async () => {
    // Tomorrow: the day's read brings it (the server widens a day by one
    // either side), but no lane on today's board lists it.
    const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1); tomorrow.setHours(9, 0, 0, 0);
    const red = job({ id: "job-9", holding_area_id: null, scheduled_at: tomorrow.toISOString() });
    redLane = [red];
    boardJobs = [red];
    const w = await mountBoard();
    await w.get('[data-testid="scheduled-unassigned-job-job-9"]').trigger("click");
    await flushAll();
    expect(w.find('[data-testid="dispatch-job-move"]').exists()).toBe(false);
    // A drop of it on a lane writes nothing and says why.
    await w.get('[data-testid="lane-ha-doors"]').trigger("drop", { dataTransfer: { getData: () => "job-9" } });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was moved", detail: expect.stringContaining("Open that day to park it"),
    }));
  });

  it("one day of a multi-day job offers no lane it would refuse, only Release", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    w.vm.$.setupState.openLaneMenu({}, { ...job(), visit_id: "v-1" });
    await flushAll();
    const buttons = w.get('[data-testid="lane-menu"]').findAll("button").map((b) => b.text());
    expect(buttons).toEqual(["Release from Order Doors"]);
    expect(w.get('[data-testid="lane-menu"]').text()).toContain("move it on its Visits card");
  });

  it("Undo after a drop on To schedule puts back the date, the crew and the lead", async () => {
    const at = today9();
    boardJobs = [job({
      holding_area_id: null, scheduled_at: at, assigned_to: "tech-1",
      assigned_tech_ids: ["tech-1", "tech-2"], lead_tech_id: "tech-2",
    })];
    const w = await mountBoard();
    await w.vm.$.setupState.moveToScheduleQueue({ dataTransfer: { getData: () => "job-1" } });
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_to: null, holding_area_id: "ha-rts", scheduled_at: null });
    expect(w.get('[data-testid="board-undo"]').text()).toContain("Taken off the schedule");
    patchMock.mockClear();
    await w.get('[data-testid="board-undo-button"]').trigger("click");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", {
      holding_area_id: null, assigned_tech_ids: ["tech-2", "tech-1"], lead_tech_id: "tech-2", scheduled_at: at,
    });
  });

  it("the week board's lane chip shows the job's hours, not its date", async () => {
    boardJobs = [job()];
    const w = await mountBoard();
    w.vm.$.setupState.viewMode = "week";
    await flushAll();
    const chip = w.get('[data-testid="lane-job-ha-doors-job-1"]');
    expect(chip.text()).toContain(formatDurationHours(3));
    expect(chip.text()).not.toMatch(/\d{1,2}\/\d{1,2}/);
  });
});
