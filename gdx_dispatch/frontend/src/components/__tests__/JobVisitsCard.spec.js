/**
 * JobVisitsCard — the job page's Visits card (multi-day jobs plan §5.3a).
 *
 * Pinned:
 *  1. Rows read "Day k of n" in the server's order; a cancelled visit reads
 *     "Cancelled", and times are on the shop's clock (the list's timezone).
 *  2. Actions follow the visit's state and only exist for a dispatch role.
 *  3. Add day(s) sends shop-local days, the time, the length in minutes and
 *     the crew by default; a range sends from/to/skip_weekends.
 *  4. Move sends the day, time, length and tech; Remove asks first.
 *  5. A refused write keeps the dialog open with the server's sentence.
 *  6. Every write emits `changed` so the page re-reads the job date.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
const confirmAsync = vi.fn();

vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock, del: delMock }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));

const model = (tag, extra = "") => ({
  props: ["modelValue"], emits: ["update:modelValue"],
  template: `<${tag} v-bind="$attrs" ${extra} />`,
});

const STUBS = {
  Dialog: { props: ["visible", "header"], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Button: {
    props: ["label", "disabled", "loading"], emits: ["click"],
    template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Tag: { props: ["value", "severity"], template: '<span v-bind="$attrs">{{ value }}</span>' },
  ProgressSpinner: { template: "<span />" },
  SelectButton: model("div"),
  DatePicker: model("div"),
  InputText: {
    props: ["modelValue"], emits: ["update:modelValue"],
    template: '<input v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  InputNumber: model("div"),
  MultiSelect: { props: ["modelValue", "options"], template: '<div v-bind="$attrs" />' },
  Select: { props: ["modelValue", "options"], template: '<div v-bind="$attrs" />' },
  Checkbox: model("div"),
  UndoArrivalDialog: { props: ["modelValue", "appointment", "mode"], template: '<div v-if="modelValue" data-testid="undo-stub" />' },
};

const TZ = "America/Chicago";
// 2026-11-02 08:00 Chicago = 14:00Z. Days 2 and 3 follow.
const V = (id, day, state, extra = {}) => ({
  id, tech_id: "t1", tech_name: "Mike", day, state, status: "scheduled",
  start_at: `${day}T14:00:00+00:00`, end_at: `${day}T22:00:00+00:00`, arrived_at: null, ...extra,
});
const LIST = {
  timezone: TZ, day_count: 3,
  items: [
    V("v1", "2026-11-02", "closed", { day_index: 1, status: "completed", arrived_at: "2026-11-02T14:05:00+00:00" }),
    V("v2", "2026-11-03", "on_site", { day_index: 2, status: "arrived", arrived_at: "2026-11-03T14:02:00+00:00" }),
    V("vx", "2026-11-03", "cancelled", { day_index: null, status: "cancelled", tech_id: "t2", tech_name: "Ana" }),
    V("v3", "2026-11-04", "open", { day_index: 3 }),
  ],
};
const TECHS = [{ id: "t1", name: "Mike" }, { id: "t2", name: "Ana" }];

async function mountCard(props = {}) {
  const { default: Card } = await import("../JobVisitsCard.vue");
  const w = mount(Card, {
    props: { jobId: "j1", technicians: TECHS, crewTechIds: ["t1"], canEdit: true, ...props },
    global: { stubs: STUBS },
  });
  await flushPromises();
  return w;
}

const row = (w, id) => w.get(`[data-testid="visit-row-${id}"]`);
const has = (w, sel) => w.find(`[data-testid="${sel}"]`).exists();

beforeEach(() => {
  for (const m of [getMock, postMock, patchMock, delMock, confirmAsync]) m.mockReset();
  getMock.mockResolvedValue(LIST);
});

describe("JobVisitsCard", () => {
  it("numbers the work days and reads the times on the shop's clock", async () => {
    const w = await mountCard();
    expect(getMock).toHaveBeenCalledWith("/api/jobs/j1/visits", { suppressErrorToast: true });
    expect(row(w, "v1").text()).toContain("Day 1 of 3");
    expect(row(w, "v1").text()).toMatch(/8:00\s?AM – 4:00\s?PM/);
    expect(row(w, "vx").text()).toContain("Cancelled");
    expect(row(w, "vx").text()).not.toContain("Day ");
    expect(row(w, "v3").text()).toContain("Day 3 of 3");
    expect(row(w, "v3").text()).toContain("Mike");
  });

  it("offers each action only in the state it applies to", async () => {
    const w = await mountCard();
    expect([has(w, "visit-move-v3"), has(w, "visit-remove-v3"), has(w, "visit-complete-v3")]).toEqual([true, true, false]);
    expect([has(w, "visit-move-v2"), has(w, "visit-complete-v2"), has(w, "visit-undo-v2")]).toEqual([false, true, true]);
    expect([has(w, "visit-move-v1"), has(w, "visit-complete-v1"), has(w, "visit-undo-v1")]).toEqual([false, false, true]);
    expect([has(w, "visit-move-vx"), has(w, "visit-undo-vx")]).toEqual([false, false]);
  });

  it("shows no action at all to someone who cannot book", async () => {
    const w = await mountCard({ canEdit: false });
    expect(has(w, "visits-add")).toBe(false);
    expect(w.findAll('[data-testid^="visit-move-"], [data-testid^="visit-remove-"], [data-testid^="visit-complete-"], [data-testid^="visit-undo-"]')).toHaveLength(0);
  });

  it("says so when nothing is booked", async () => {
    getMock.mockResolvedValue({ items: [], day_count: 0, timezone: TZ });
    const w = await mountCard();
    expect(has(w, "visits-empty")).toBe(true);
  });

  it("adds picked days with the crew, the last visit's time and length", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visits-add"]').trigger("click");
    const vm = w.vm.$.setupState;
    expect(vm.add.startTime).toBe("08:00");
    expect(vm.add.hours).toBe(8);
    expect(vm.add.techIds).toEqual(["t1"]);
    vm.add.days = [new Date(2026, 10, 6), new Date(2026, 10, 5)];
    postMock.mockResolvedValue({ ...LIST, day_count: 5 });
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-add-submit"]').trigger("click");
    await flushPromises();
    expect(postMock).toHaveBeenCalledWith("/api/jobs/j1/visits", {
      start_time: "08:00", duration_minutes: 480, tech_ids: ["t1"], days: ["2026-11-05", "2026-11-06"],
    }, expect.objectContaining({ suppressErrorToast: true }));
    expect(has(w, "visits-add-dialog")).toBe(false);
    expect(w.emitted("changed")).toHaveLength(1);
  });

  it("adds a range, skipping weekends by default", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visits-add"]').trigger("click");
    const vm = w.vm.$.setupState;
    vm.add.mode = "range";
    vm.add.range = [new Date(2026, 10, 6), new Date(2026, 10, 10)];
    vm.add.hours = 4.5;
    postMock.mockResolvedValue(LIST);
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-add-submit"]').trigger("click");
    await flushPromises();
    const [, body] = postMock.mock.calls[0];
    expect(body.range).toEqual({ from: "2026-11-06", to: "2026-11-10", skip_weekends: true });
    expect(body.duration_minutes).toBe(270);
    expect(body.days).toBeUndefined();
  });

  it("keeps the dialog open with the server's sentence when refused", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visits-add"]').trigger("click");
    w.vm.$.setupState.add.days = [new Date(2026, 10, 3)];
    postMock.mockRejectedValue(Object.assign(new Error("A technician already has a visit of this job on 2026-11-03."),
      { body: { code: "double_booked" } }));
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-add-submit"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="visits-add-error"]').text()).toContain("already has a visit");
    expect(has(w, "visits-add-dialog")).toBe(true);
    expect(w.emitted("changed")).toBeUndefined();
  });

  it("moves an open visit with its day, time, length and tech", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visit-move-v3"]').trigger("click");
    const vm = w.vm.$.setupState;
    expect(vm.move.day.getDate()).toBe(4);
    expect([vm.move.startTime, vm.move.hours, vm.move.techId]).toEqual(["08:00", 8, "t1"]);
    vm.move.day = new Date(2026, 10, 9);
    vm.move.startTime = "07:30";
    vm.move.techId = "t2";
    patchMock.mockResolvedValue(LIST);
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-move-submit"]').trigger("click");
    await flushPromises();
    // The length was not touched, so it is not sent: the server keeps it.
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/j1/visits/v3", {
      day: "2026-11-09", start_time: "07:30", tech_id: "t2",
    }, expect.objectContaining({ suppressErrorToast: true }));
    expect(w.emitted("changed")).toHaveLength(1);
  });

  it("sends a length only when it was changed", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visit-move-v3"]').trigger("click");
    const vm = w.vm.$.setupState;
    vm.move.hours = 6;
    patchMock.mockResolvedValue(LIST);
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-move-submit"]').trigger("click");
    await flushPromises();
    expect(patchMock.mock.calls[0][1]).toMatchObject({ duration_minutes: 360 });
  });

  it("refuses a changed length over a day, and leaves a long visit's own length alone", async () => {
    const long = {
      ...LIST,
      items: LIST.items.map((v) => (v.id === "v3"
        ? { ...v, end_at: new Date(new Date(v.start_at).getTime() + 30 * 3600_000).toISOString() }
        : v)),
    };
    getMock.mockResolvedValue(long);
    const w = await mountCard();
    await w.get('[data-testid="visit-move-v3"]').trigger("click");
    const vm = w.vm.$.setupState;
    expect(vm.move.hours).toBe(30);
    expect(vm.moveMaxHours).toBe(30);
    patchMock.mockResolvedValue(long);
    vm.move.startTime = "07:00";
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-move-submit"]').trigger("click");
    await flushPromises();
    expect(patchMock.mock.calls[0][1]).not.toHaveProperty("duration_minutes");
    patchMock.mockClear();
    await w.get('[data-testid="visit-move-v3"]').trigger("click");
    vm.move.hours = 26;
    await w.vm.$nextTick();
    await w.get('[data-testid="visits-move-submit"]').trigger("click");
    await flushPromises();
    expect(patchMock).not.toHaveBeenCalled();
    expect(vm.move.error).toContain("at most 24 hours");
  });

  it("removes a day only after asking", async () => {
    const w = await mountCard();
    confirmAsync.mockResolvedValueOnce(false);
    await w.get('[data-testid="visit-remove-v3"]').trigger("click");
    await flushPromises();
    expect(delMock).not.toHaveBeenCalled();
    confirmAsync.mockResolvedValueOnce(true);
    delMock.mockResolvedValue(LIST);
    await w.get('[data-testid="visit-remove-v3"]').trigger("click");
    await flushPromises();
    expect(delMock).toHaveBeenCalledWith("/api/jobs/j1/visits/v3", { successMessage: "Day removed" });
    expect(w.emitted("changed")).toHaveLength(1);
  });

  it("completes an on-site visit and re-reads the list", async () => {
    const w = await mountCard();
    postMock.mockResolvedValue({});
    getMock.mockClear();
    await w.get('[data-testid="visit-complete-v2"]').trigger("click");
    await flushPromises();
    expect(postMock).toHaveBeenCalledWith("/api/appointments/v2/complete", {}, { successMessage: "Visit completed" });
    expect(getMock).toHaveBeenCalledWith("/api/jobs/j1/visits", { suppressErrorToast: true });
    expect(w.emitted("changed")).toHaveLength(1);
  });

  it("opens Undo arrival on the visit", async () => {
    const w = await mountCard();
    await w.get('[data-testid="visit-undo-v2"]').trigger("click");
    const undo = w.findComponent(STUBS.UndoArrivalDialog);
    expect(undo.props("mode")).toBe("visit");
    expect(undo.props("appointment").id).toBe("v2");
    expect(has(w, "undo-stub")).toBe(true);
  });
});
