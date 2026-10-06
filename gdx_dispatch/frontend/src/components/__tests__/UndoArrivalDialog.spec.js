/**
 * UndoArrivalDialog — multi-day jobs plan §5.2a "Undo arrival".
 * Visit mode: GET/POST /api/appointments/{id}/undo-arrival
 *   ({reason, tap_record_id?, also_undo}).
 * Job mode: GET/POST /api/jobs/{id}/undo-arrival ({tech_id, tap_record_ids, reason}).
 * Reason required; not_reverted shown; later taps kept as real are offered as
 * "Record arrival at" through office Arrived.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: vi.fn(), del: vi.fn() }),
}));

const STUBS = {
  Dialog: { props: ["visible", "header"], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Button: { props: ["label", "disabled", "loading"], template: '<button v-bind="$attrs" :disabled="disabled">{{ label }}</button>' },
  Textarea: {
    props: ["modelValue"], emits: ["update:modelValue"],
    template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  RadioButton: {
    props: ["modelValue", "value"], emits: ["update:modelValue"],
    template: '<input type="radio" v-bind="$attrs" :checked="modelValue === value" @change="$emit(\'update:modelValue\', value)" />',
  },
  Checkbox: {
    props: ["modelValue", "value"], emits: ["update:modelValue"],
    template: '<input type="checkbox" v-bind="$attrs" :checked="(modelValue || []).includes(value)" @change="$emit(\'update:modelValue\', $event.target.checked ? [...(modelValue || []), value] : (modelValue || []).filter(v => v !== value))" />',
  },
};

const VISIT = { id: "v1", job_id: "j1", tech_id: "t1", status: "scheduled", arrived_at: "2026-10-05T13:14:00Z" };
const TAP = (id, at) => ({ id, tech_id: "t1", arrived_at: at, day: "2026-10-05" });

async function mountDlg(props) {
  const { default: Dlg } = await import("../UndoArrivalDialog.vue");
  const w = mount(Dlg, { props: { modelValue: true, techName: "Mike", ...props }, global: { stubs: STUBS } });
  await flushPromises();
  return w;
}

beforeEach(() => { getMock.mockReset(); postMock.mockReset(); });

describe("visit mode", () => {
  it("previews the visit and needs a reason before it posts", async () => {
    getMock.mockResolvedValueOnce({ visit_id: "v1", arrived_at: VISIT.arrived_at,
      tap: TAP("tap1", VISIT.arrived_at), unmatched_taps: [], later_taps: {} });
    postMock.mockResolvedValueOnce({ appointment: {}, not_reverted: [], tap_record_ids: ["tap1"] });
    const w = await mountDlg({ mode: "visit", appointment: VISIT });
    expect(getMock).toHaveBeenCalledWith("/api/appointments/v1/undo-arrival", { suppressErrorToast: true });
    expect(w.get('[data-testid="undo-question"]').text()).toMatch(/^Undo Mike's arrival at /);
    expect(w.find('[data-testid="undo-matched-tap"]').exists()).toBe(true);
    expect(w.get('[data-testid="undo-submit"]').attributes("disabled")).toBeDefined();
    await w.get('[data-testid="undo-reason"]').setValue("wrong job");
    expect(w.get('[data-testid="undo-submit"]').attributes("disabled")).toBeUndefined();
    await w.get('[data-testid="undo-submit"]').trigger("click");
    await flushPromises();
    const [url, body] = postMock.mock.calls[0];
    expect(url).toBe("/api/appointments/v1/undo-arrival");
    // The matched tap is the server's to find; it is not named back.
    expect(body).toEqual({ reason: "wrong job", also_undo: [] });
    expect(w.find('[data-testid="undo-result"]').exists()).toBe(true);
    expect(w.emitted("done")).toHaveLength(1);
  });

  it("names a picked unmatched tap and the later taps asked about", async () => {
    getMock.mockResolvedValueOnce({ visit_id: "v1", arrived_at: null, tap: null,
      unmatched_taps: [TAP("tapA", "2026-10-05T13:00:00Z")],
      later_taps: { tapA: [TAP("tapB", "2026-10-05T13:00:01Z")] } });
    postMock.mockResolvedValueOnce({ not_reverted: [], tap_record_ids: ["tapA", "tapB"] });
    const w = await mountDlg({ mode: "visit", appointment: { ...VISIT, arrived_at: null, status: "arrived" } });
    expect(w.get('[data-testid="undo-question"]').text()).toContain("No arrival time was recorded");
    expect(w.find('[data-testid="undo-later-taps"]').exists()).toBe(false);
    await w.get('[data-testid="undo-tap-tapA"]').trigger("change");
    await flushPromises();
    await w.get('[data-testid="undo-later-tapB"]').setValue(true);
    await w.get('[data-testid="undo-reason"]').setValue("pocket tap");
    await w.get('[data-testid="undo-submit"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0][1]).toEqual({ reason: "pocket tap", also_undo: ["tapB"], tap_record_id: "tapA" });
  });

  it("shows not_reverted and offers to record a kept later tap", async () => {
    getMock.mockResolvedValueOnce({ visit_id: "v1", arrived_at: VISIT.arrived_at,
      tap: TAP("tap1", VISIT.arrived_at), unmatched_taps: [],
      later_taps: { tap1: [TAP("tap2", "2026-10-05T14:02:00Z")] } });
    postMock
      .mockResolvedValueOnce({ not_reverted: [{ field: "visit.start_at", reason: "edited_since" }], tap_record_ids: ["tap1"] })
      .mockResolvedValueOnce({ ok: true });
    const w = await mountDlg({ mode: "visit", appointment: VISIT });
    await w.get('[data-testid="undo-reason"]').setValue("wrong job");
    await w.get('[data-testid="undo-submit"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="undo-not-reverted"]').text()).toContain("The visit stays where it is — it was changed since the tap.");
    await w.get('[data-testid="undo-record-tap2"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[1][0]).toBe("/api/appointments/v1/arrived");
    expect(postMock.mock.calls[1][1]).toEqual({ arrived_at: "2026-10-05T14:02:00Z" });
  });

  it("keeps a refused undo inline and stays open", async () => {
    getMock.mockResolvedValueOnce({ visit_id: "v1", arrived_at: null, tap: null, unmatched_taps: [], later_taps: {} });
    postMock.mockRejectedValueOnce(Object.assign(new Error("This visit has no arrival to undo."), {
      status: 409, body: { detail: "This visit has no arrival to undo.", code: "no_arrival" } }));
    const w = await mountDlg({ mode: "visit", appointment: VISIT });
    await w.get('[data-testid="undo-reason"]').setValue("x");
    await w.get('[data-testid="undo-submit"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="undo-error"]').text()).toBe("This visit has no arrival to undo.");
    expect(w.emitted("done")).toBeUndefined();
  });
});

describe("job mode", () => {
  it("lists unmatched taps and posts tech_id + tap_record_ids", async () => {
    getMock.mockResolvedValueOnce({ job_id: "j1", tech_id: "t1",
      unmatched_taps: [TAP("tapX", "2026-10-05T12:00:00Z"), TAP("tapY", "2026-10-05T12:30:00Z")] });
    postMock.mockResolvedValueOnce({ not_reverted: [], tap_record_ids: ["tapY"] });
    const w = await mountDlg({ mode: "job", jobId: "j1", techId: "t1" });
    expect(getMock.mock.calls[0][0]).toBe("/api/jobs/j1/undo-arrival?tech_id=t1");
    await w.get('[data-testid="undo-reason"]').setValue("tapped the wrong job");
    // A reason alone is not enough: a tap must be picked.
    expect(w.get('[data-testid="undo-submit"]').attributes("disabled")).toBeDefined();
    await w.get('[data-testid="undo-jobtap-tapY"]').setValue(true);
    await w.get('[data-testid="undo-submit"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0]).toEqual([
      "/api/jobs/j1/undo-arrival",
      { tech_id: "t1", tap_record_ids: ["tapY"], reason: "tapped the wrong job" },
      expect.objectContaining({ suppressErrorToast: true }),
    ]);
  });

  it("with no unmatched taps, says so and points at the Appointments page", async () => {
    getMock.mockResolvedValueOnce({ job_id: "j1", tech_id: "t1", unmatched_taps: [] });
    const w = await mountDlg({ mode: "job", jobId: "j1", techId: "t1" });
    expect(w.get('[data-testid="undo-no-taps"]').text()).toContain("Each of Mike's taps stamped a visit");
    expect(w.find('[data-testid="undo-reason"]').exists()).toBe(false);
    await w.get('[data-testid="undo-no-taps"] a').trigger("click");
    expect(w.emitted("open-appointments")).toHaveLength(1);
  });
});
