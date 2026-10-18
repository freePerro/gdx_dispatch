/**
 * The job page's Technician picker on a crew job keeps the crew (2026-10-15).
 *
 * The jobs PATCH reads a single `assigned_tech_id` as the whole crew, and the
 * picker sent exactly that — and sent it as PrimeVue Select's whole @change
 * payload ({ originalEvent, value }, primevue/select updateModel), not the id.
 * Harness copied from JobDetailCrewUndo.spec.js; the real CrewChangeDialog
 * renders through the Dialog stub.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const putMock = vi.fn();
const toastAdd = vi.fn();
const confirmMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-1" }, query: {}, path: "/jobs/job-1" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    put: putMock,
    del: vi.fn(),
    request: vi.fn(),
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync: confirmMock, confirmDestructive: vi.fn() }),
}));
let role = "admin";
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role }, hasPermission: () => true }),
}));

/**
 * @param job         what GET /api/jobs/job-1 returns
 * @param customer    what GET /api/customers/{id} returns; null = the read fails
 */
function routeGet({ crew = [], job = {}, customer = { id: "cust-1", name: "Acme Door Co" }, crewFails = false } = {}) {
  getMock.mockImplementation(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/customers/") && !u.includes("/locations")) {
      if (!customer) throw Object.assign(new Error("denied"), { status: 404 });
      return customer;
    }
    if (u === "/api/jobs/job-1/assignments") {
      if (crewFails) throw Object.assign(new Error("boom"), { status: 500 });
      return crew;
    }
    if (u.startsWith("/api/technicians")) return [{ id: "t1", name: "Mike" }, { id: "t2", name: "Sam" }, { id: "t3", name: "Pat" }];
    if (u === "/api/jobs/job-1") {
      return { id: "job-1", title: "Spring replacement", status: "Scheduled", ...job };
    }
    return [];
  });
}

async function mountView() {
  const { default: View } = await import("../JobDetailView.vue");
  const w = mount(View, {
    global: {
      stubs: {
        // Renders `to` as an href so the assertion reads the real binding —
        // a `true` stub would swallow it and pass for any destination.
        RouterLink: {
          props: ["to"],
          template: '<a :href="to" :data-testid="$attrs[\'data-testid\']"><slot /></a>',
          inheritAttrs: false,
        },
        Button: { props: ["label"], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
        DataTable: true, Column: true,
        Dialog: { props: ['visible', 'header'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        MobileJobCloseoutDialog: { props: ['visible', 'jobId'], template: '<div data-testid="closeout-sheet" :data-open="String(visible)" :data-job="jobId" />' }, Select: true, InputText: true,
        Textarea: { props: ['modelValue'], emits: ['update:modelValue'], template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' }, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true,
        JobStateOverrideDialog: { props: ['modelValue'], template: '<div data-testid="reopen-dialog" :data-open="String(modelValue)" />' }, CatalogPickerDialog: true,
        DoorSpecList: true, PhoneInput: true, EmailTimeline: true, InputNumber: true,
        AuthedImage: true,
        UndoArrivalDialog: { props: ['modelValue', 'mode', 'jobId', 'techId', 'techName'], template: '<div v-if="modelValue" data-testid="crew-undo-dialog">{{ mode }}:{{ jobId }}:{{ techId }}:{{ techName }}</div>' },
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  role = "admin";
  patchMock.mockResolvedValue({});
  putMock.mockResolvedValue({});
});

const CREW = [
  { id: "a1", tech_id: "t1", is_lead: true },
  { id: "a2", tech_id: "t2", is_lead: false },
];
const OPEN = {
  status: "Scheduled", lifecycle_stage: "Scheduled", lifecycle_stage_raw: "scheduled",
  customer_id: "cust-1", customer_name: "Acme", assigned_to: "t1",
};
// What primevue/select emits on @change.
const pick = (value) => ({ originalEvent: {}, value });

async function choose(w, techId) {
  const done = w.vm.$.setupState.assignTech(pick(techId));
  await flushPromises();
  return { done };
}

describe("job page Technician picker on a crew job", () => {
  it("picking the lead again sends nothing", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    await (await choose(w, "t1")).done;
    expect(patchMock).not.toHaveBeenCalled();
    expect(putMock).not.toHaveBeenCalled();
  });

  it("picking another crew member makes them lead", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    await (await choose(w, "t2")).done;
    expect(putMock).toHaveBeenCalledWith("/api/jobs/job-1/lead", { tech_id: "t2" }, expect.anything());
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("picking an outsider asks, and Add keeps everyone", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    const { done } = await choose(w, "t3");
    expect(patchMock).not.toHaveBeenCalled();
    expect(w.get('[data-testid="crew-change-prompt"]').text()).toContain("Acme has a crew: Mike, Sam");
    await w.get('[data-testid="crew-change-add"]').trigger("click");
    await done;
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1",
      { assigned_tech_ids: ["t1", "t2", "t3"], lead_tech_id: "t1" },
      expect.anything(),
    );
  });

  it("Swap of the lead makes the new tech lead", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    const { done } = await choose(w, "t3");
    await w.get('[data-testid="crew-change-swap-t1"]').trigger("click");
    await done;
    expect(patchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1",
      { assigned_tech_ids: ["t3", "t2"], lead_tech_id: "t3" },
      expect.anything(),
    );
  });

  it("Cancel changes nothing and puts the picker back", async () => {
    routeGet({ crew: CREW, job: OPEN });
    const w = await mountView();
    const { done } = await choose(w, "t3");
    w.vm.$.setupState.selectedTech = "t3";
    await w.get('[data-testid="crew-change-cancel"]').trigger("click");
    await done;
    expect(patchMock).not.toHaveBeenCalled();
    expect(w.vm.$.setupState.selectedTech).toBe("t1");
  });

  it("a one-tech job sends the picked id, not the event", async () => {
    routeGet({ crew: [CREW[0]], job: OPEN });
    const w = await mountView();
    await (await choose(w, "t3")).done;
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_tech_id: "t3" }, expect.anything());
  });

  it("clearing the picker asks first, names the crew, then unassigns", async () => {
    routeGet({ crew: CREW, job: OPEN });
    confirmMock.mockResolvedValue(true);
    const w = await mountView();
    await (await choose(w, null)).done;
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({
      header: "Remove every technician?",
      message: expect.stringContaining("Mike, Sam"),
    }));
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_tech_id: null }, expect.anything());
  });

  it("clearing the picker and saying no sends nothing and puts the picker back", async () => {
    routeGet({ crew: CREW, job: OPEN });
    confirmMock.mockResolvedValue(false);
    const w = await mountView();
    w.vm.$.setupState.selectedTech = null;
    await (await choose(w, null)).done;
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(patchMock).not.toHaveBeenCalled();
    expect(w.vm.$.setupState.selectedTech).toBe("t1");
  });

  it("clearing a one-tech job asks too", async () => {
    routeGet({ crew: [CREW[0]], job: OPEN });
    confirmMock.mockResolvedValue(false);
    const w = await mountView();
    await (await choose(w, null)).done;
    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ header: "Remove the technician?" }));
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("sends nothing when the crew could not be read", async () => {
    routeGet({ crew: CREW, job: OPEN, crewFails: true });
    const w = await mountView();
    w.vm.$.setupState.selectedTech = "t3";
    await (await choose(w, "t3")).done;
    expect(patchMock).not.toHaveBeenCalled();
    expect(w.vm.$.setupState.selectedTech).toBe("t1");
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: "Crew not loaded" }));
  });

  it("puts the picker back when the lead change fails", async () => {
    routeGet({ crew: CREW, job: OPEN });
    putMock.mockRejectedValue(Object.assign(new Error("denied"), { status: 403 }));
    const w = await mountView();
    w.vm.$.setupState.selectedTech = "t2";
    await (await choose(w, "t2")).done;
    expect(w.vm.$.setupState.selectedTech).toBe("t1");
  });

  it("puts the picker back when a one-tech reassign fails", async () => {
    routeGet({ crew: [CREW[0]], job: OPEN });
    const w = await mountView();
    patchMock.mockRejectedValueOnce(Object.assign(new Error("denied"), { status: 403 }));
    w.vm.$.setupState.selectedTech = "t3";
    await (await choose(w, "t3")).done;
    expect(patchMock).toHaveBeenCalledWith("/api/jobs/job-1", { assigned_tech_id: "t3" }, expect.anything());
    expect(w.vm.$.setupState.selectedTech).toBe("t1");
  });
});

// The Schedule dialog sends assigned_tech_ids, which the server takes as the
// whole crew; seeded after a failed read it held the job's lone assigned_to,
// so a reschedule cut a crew to one (GDXA-1499).
describe("job page Schedule dialog on an unread crew", () => {
  async function saveSchedule(w) {
    const s = w.vm.$.setupState;
    s.openSchedule();
    await flushPromises();
    await s.saveSchedule();
    await flushPromises();
    return patchMock.mock.calls.find(([url]) => url === "/api/jobs/job-1")?.[1];
  }

  it("leaves the crew out when the read failed", async () => {
    routeGet({ crew: CREW, job: { ...OPEN, scheduled_at: "2026-10-20T14:30:00Z" }, crewFails: true });
    const w = await mountView();
    const payload = await saveSchedule(w);
    expect(payload).toBeTruthy();
    expect(payload).not.toHaveProperty("assigned_tech_ids");
    expect(payload.scheduled_at).toBe("2026-10-20T14:30:00.000Z");
  });

  it("a dialog opened before the read lands sends the crew the read returned", async () => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    routeGet({ crew: CREW, job: { ...OPEN, scheduled_at: "2026-10-20T14:30:00Z" } });
    const base = getMock.getMockImplementation();
    getMock.mockImplementation(async (url) => {
      if (String(url) === "/api/jobs/job-1/assignments") { await held; }
      return base(url);
    });
    const w = await mountView();
    const s = w.vm.$.setupState;
    s.openSchedule();
    await flushPromises();
    // The guess (assigned_to alone) is on the form, read-only, and Save waits.
    expect(s.scheduleForm.tech_ids).toEqual(["t1"]);
    expect(s.scheduleForm.crewFromRead).toBe(false);
    await s.saveSchedule();
    await flushPromises();
    expect(patchMock).not.toHaveBeenCalled();
    release();
    await flushPromises();
    await s.saveSchedule();
    await flushPromises();
    const payload = patchMock.mock.calls.find(([url]) => url === "/api/jobs/job-1")?.[1];
    expect(payload.assigned_tech_ids).toEqual(["t1", "t2"]);
  });

  it("sends the crew it read (control)", async () => {
    routeGet({ crew: CREW, job: { ...OPEN, scheduled_at: "2026-10-20T14:30:00Z" } });
    const w = await mountView();
    const payload = await saveSchedule(w);
    expect(payload.assigned_tech_ids).toEqual(["t1", "t2"]);
  });
});
