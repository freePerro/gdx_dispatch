/**
 * DispatchView — "Partial Jobs — Need to Schedule" (multi-day jobs plan
 * §5.3a, D10-D12).
 *
 * A job a tech worked one day of, with nothing further booked, has a past
 * date and no visit coming — the day after, it was on no screen. This section
 * is where it waits, and dragging its card onto a tech books the next day.
 *
 * Mounts the REAL DispatchView (same harness as DispatchLateOpen.spec.js).
 *
 * Pinned:
 *  1. No rows → the section does not exist.
 *  2. A row shows the customer, the job, the last worked day and who worked it.
 *  3. A job parked in a holding area (not Ready to Schedule) is left out.
 *  4. A partial job never also shows in "New Jobs to Schedule".
 *  5. A drop on a timeline slot or the tray is PATCH /api/jobs/{id} with the
 *     tech AND the date — always the date, which an ordinary reassignment of a
 *     dated job leaves out — and no duration prompt.
 *  6. A drop the server answers 200 but books nothing for (E5: the job is
 *     still partial on re-read) warns; one that books says so.
 *  7. A user without jobs.read_all never calls the endpoint.
 *  8. A partial job parked in a holding area shows in that area's lane (the
 *     day's job list never loads it), and Release sends it back here.
 *  9. A drop on a day that has passed books nothing and says why.
 * 10. A drop at the job's own stored instant sends nothing: the server reads
 *     it as no date change and would swap the crew without booking a day.
 * 11. A drop on New Jobs to Schedule only un-parks a partial job.
 * 12. A drop on a tech with a closed visit of the job that day (closed_days)
 *     sends nothing: E5 books nothing but would still write the date and crew.
 * 13. A failed re-read after a drop claims neither outcome.
 * 14. A parked partial job counts its hours in the lane total and opens the
 *     drawer with the same fields a day-list job has (tech, window).
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";
import { formatDurationHours } from "../../utils/hours";

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
  props: ["tech"],
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
// Drops are judged against today, so the slots are relative to it.
const inDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); d.setHours(9, 0, 0, 0); return d.toISOString(); };
const LATER = inDays(2);

const PARTIAL = {
  id: "job-7", job_number: "JOB-2026-007", title: "Install 16x7 + opener", job_type: "Install",
  status: "In Progress", lifecycle_stage: "in_progress", scheduled_at: "2026-10-01T14:00:00+00:00",
  customer_id: "c1", customer_name: "Dana Ruiz", assigned_to: "tech-1", holding_area_id: null,
  is_return_visit: false, last_worked_day: "2026-10-01",
  worked_by: [{ tech_id: "tech-1", name: "Mike" }],
};

let partialRows = [];
let boardJobs = [];
let holdingAreas = [];

async function mountBoard() {
  getMock.mockImplementation((url) => {
    const u = String(url || "");
    if (u.includes("/api/dispatch/partial-jobs")) return Promise.resolve({ items: partialRows });
    if (u.includes("/api/dispatch/late-open")) return Promise.resolve({ items: [] });
    if (u.includes("/api/technicians")) return Promise.resolve([
      { id: "tech-1", name: "Mike", active: true }, { id: "tech-2", name: "Ana", active: true },
    ]);
    if (u.includes("/api/holding-areas")) return Promise.resolve(holdingAreas);
    if (u.includes("/api/jobs")) return Promise.resolve(boardJobs);
    return Promise.resolve([]);
  });
  const w = mount(DispatchView, { global: { stubs } });
  await flushAll();
  return w;
}

const partialCalls = () => getMock.mock.calls.filter(([u]) => String(u).includes("/api/dispatch/partial-jobs")).length;
const timeline = (w, techId) => w.findAllComponents(TechTimelineColumn).find((c) => String(c.props("tech")?.id) === techId);

describe("DispatchView — Partial Jobs", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset(); patchMock.mockReset(); toastAdd.mockReset();
    granted = new Set(["jobs.read_all"]);
    partialRows = []; boardJobs = []; holdingAreas = [];
  });

  it("does not render when no job is partial", async () => {
    const w = await mountBoard();
    expect(partialCalls()).toBeGreaterThan(0);
    expect(w.find('[data-testid="partial-jobs"]').exists()).toBe(false);
  });

  it("shows the customer, the job, the last worked day and who worked it", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    const card = w.find('[data-testid="partial-job-job-7"]');
    expect(card.exists()).toBe(true);
    expect(card.text()).toContain("Dana Ruiz");
    expect(card.text()).toContain("Install 16x7 + opener");
    const worked = w.find('[data-testid="partial-job-worked-job-7"]').text();
    expect(worked).toMatch(/Last worked .*10\/1 by Mike/);
    expect(card.attributes("draggable")).toBe("true");
  });

  it("shows a parked job in its holding area's lane instead, but keeps one in Ready to Schedule", async () => {
    holdingAreas = [{ id: "ha-parts", name: "Needs Parts" }, { id: "ha-rts", name: "Ready to Schedule" }];
    partialRows = [
      { ...PARTIAL, id: "job-parked", holding_area_id: "ha-parts" },
      { ...PARTIAL, id: "job-rts", holding_area_id: "ha-rts" },
    ];
    const w = await mountBoard();
    expect(w.find('[data-testid="partial-job-job-parked"]').exists()).toBe(false);
    const lane = w.find('[data-testid="holding-job-ha-parts-job-parked"]');
    expect(lane.exists()).toBe(true);
    expect(lane.text()).toContain("Dana Ruiz");
    expect(w.find('[data-testid="partial-job-job-rts"]').exists()).toBe(true);
  });

  it("a parked partial job counts its hours and opens a full drawer", async () => {
    holdingAreas = [{ id: "ha-parts", name: "Needs Parts" }];
    partialRows = [{ ...PARTIAL, holding_area_id: "ha-parts", scheduled_duration_hours: 3, effective_duration_hours: 3 }];
    const w = await mountBoard();
    const total = w.get('[data-testid="holding-area-total-ha-parts"]').text();
    expect(total).toContain(`${formatDurationHours(3)} queued`);
    expect(total).not.toContain("no-est");
    await w.get('[data-testid="holding-job-ha-parts-job-7"]').trigger("click");
    await flushAll();
    const drawer = w.get(".job-drawer-content").text();
    expect(drawer).toContain("Technician: Mike");
    expect(drawer).toContain("Window: Anytime");
  });

  it("Release on a parked partial job clears only its holding area", async () => {
    holdingAreas = [{ id: "ha-parts", name: "Needs Parts" }];
    partialRows = [{ ...PARTIAL, holding_area_id: "ha-parts" }];
    const w = await mountBoard();
    patchMock.mockImplementation(() => { partialRows = [PARTIAL]; return Promise.resolve({}); });
    const release = w.get('[data-testid="holding-job-ha-parts-job-7"]').findAll("button").find((b) => b.text() === "Release");
    await release.trigger("click");
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-7", { holding_area_id: null });
    expect(w.find('[data-testid="partial-job-job-7"]').exists()).toBe(true);
  });

  it("Release leaves the lane even when the job was last worked on the board day", async () => {
    holdingAreas = [{ id: "ha-parts", name: "Needs Parts" }];
    const today = { ...PARTIAL, scheduled_at: inDays(0), holding_area_id: "ha-parts" };
    partialRows = [today];
    boardJobs = [{ ...today, technician_id: "tech-1" }];
    const w = await mountBoard();
    patchMock.mockImplementation(() => { partialRows = [{ ...today, holding_area_id: null }]; return Promise.resolve({}); });
    const release = w.get('[data-testid="holding-job-ha-parts-job-7"]').findAll("button").find((b) => b.text() === "Release");
    await release.trigger("click");
    await flushAll();
    expect(w.find('[data-testid="holding-job-ha-parts-job-7"]').exists()).toBe(false);
  });

  it("a drop on a day that has passed books nothing and says why", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-7", techId: "tech-2", startISO: inDays(-1) });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was booked", detail: expect.stringContaining("has passed"),
    }));
    expect(w.find('[data-testid="partial-job-job-7"]').exists()).toBe(true);
  });

  it("never shows a partial job in New Jobs to Schedule too", async () => {
    partialRows = [{ ...PARTIAL, scheduled_at: null }];
    boardJobs = [{ ...PARTIAL, scheduled_at: null, technician_id: null, assigned_to: null }];
    const w = await mountBoard();
    expect(w.find('[data-testid="partial-job-job-7"]').exists()).toBe(true);
    expect(w.find('[data-testid="unassigned-job-job-7"]').exists()).toBe(false);
  });

  it("a timeline drop sends the tech and the slot, and says the day was booked", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    patchMock.mockImplementation(() => { partialRows = []; return Promise.resolve({}); });
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-7", techId: "tech-2", startISO: LATER });
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-7", {
      assigned_tech_id: "tech-2", assigned_to: "tech-2", scheduled_at: LATER,
    });
    expect(w.find('[data-testid="dialog"]').exists()).toBe(false); // no duration prompt
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: "success", summary: "Next day booked" }));
    expect(w.find('[data-testid="partial-jobs"]').exists()).toBe(false);
  });

  it("a tray drop sends the board's day even though the job already has a date", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    patchMock.mockResolvedValue({});
    timeline(w, "tech-1").vm.$emit("place-tray", { jobId: "job-7", techId: "tech-1" });
    await flushAll();
    const [, body] = patchMock.mock.calls[0];
    expect(body.assigned_tech_id).toBe("tech-1");
    const sent = new Date(body.scheduled_at);
    expect([sent.getHours(), sent.getMinutes()]).toEqual([0, 0]);
    expect(sent.toDateString()).toBe(new Date().toDateString());
  });

  it("a tray drop at the job's own date sends nothing, since the server would book nothing", async () => {
    const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
    partialRows = [{ ...PARTIAL, scheduled_at: midnight.toISOString() }];
    const w = await mountBoard();
    timeline(w, "tech-2").vm.$emit("place-tray", { jobId: "job-7", techId: "tech-2" });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was booked", detail: expect.stringContaining("already worked at that time"),
    }));
  });

  it("a drop on a tech with a closed visit that day sends nothing (E5 would book nothing)", async () => {
    // Mike closed a visit tomorrow early; the last worked day is the day
    // after, so a check on last_worked_day alone would let this through.
    const day = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
    partialRows = [{ ...PARTIAL, last_worked_day: day(2), closed_days: { [day(1)]: ["tech-1"], [day(2)]: ["tech-2"] } }];
    const at = new Date(); at.setDate(at.getDate() + 1); at.setHours(15, 0, 0, 0);
    const w = await mountBoard();
    timeline(w, "tech-1").vm.$emit("place", { jobId: "job-7", techId: "tech-1", startISO: at.toISOString() });
    await flushAll();
    expect(patchMock).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was booked", detail: expect.stringContaining("Mike already worked"),
    }));
    // Another tech that day is a real booking.
    patchMock.mockResolvedValue({});
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-7", techId: "tech-2", startISO: at.toISOString() });
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
  });

  it("a drop on New Jobs to Schedule only un-parks a partial job", async () => {
    holdingAreas = [{ id: "ha-parts", name: "Needs Parts" }, { id: "ha-rts", name: "Ready to Schedule" }];
    partialRows = [{ ...PARTIAL, holding_area_id: "ha-parts" }];
    const w = await mountBoard();
    patchMock.mockResolvedValue({});
    await w.get('[data-testid="unassigned-section"]').trigger("drop", { dataTransfer: { getData: () => "job-7" } });
    await flushAll();
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-7", { holding_area_id: "ha-rts" });
  });

  it("warns when the server booked nothing and the job is still partial", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    patchMock.mockResolvedValue({}); // 200, and the re-read still lists it (E5)
    timeline(w, "tech-1").vm.$emit("place", { jobId: "job-7", techId: "tech-1", startISO: LATER });
    await flushAll();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: "warn", summary: "Nothing was booked", detail: expect.stringContaining("No visit of Dana Ruiz was booked for Mike"),
    }));
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ severity: "success" }));
    expect(w.find('[data-testid="partial-job-job-7"]').exists()).toBe(true);
  });

  it("claims neither booked nor not booked when the re-read fails", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    patchMock.mockResolvedValue({});
    const base = getMock.getMockImplementation();
    getMock.mockImplementation((url) => (String(url).includes("/api/dispatch/partial-jobs")
      ? Promise.reject(new Error("offline")) : base(url)));
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-7", techId: "tech-2", startISO: LATER });
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: "info", summary: "Drop sent" }));
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: "Nothing was booked" }));
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: "Next day booked" }));
  });

  it("keeps the card and adds no toast of its own when the server refuses", async () => {
    partialRows = [PARTIAL];
    const w = await mountBoard();
    patchMock.mockRejectedValue(Object.assign(new Error("refused"), { body: { code: "crew_on_site", detail: "refused" } }));
    timeline(w, "tech-2").vm.$emit("place", { jobId: "job-7", techId: "tech-2", startISO: LATER });
    await flushAll();
    expect(toastAdd).not.toHaveBeenCalled(); // useApi shows the server's sentence
    expect(w.find('[data-testid="partial-job-job-7"]').exists()).toBe(true);
  });

  it("is never asked for without jobs.read_all", async () => {
    granted = new Set();
    partialRows = [PARTIAL];
    const w = await mountBoard();
    expect(partialCalls()).toBe(0);
    expect(w.find('[data-testid="partial-jobs"]').exists()).toBe(false);
  });
});
