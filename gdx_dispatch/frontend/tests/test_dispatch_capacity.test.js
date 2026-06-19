// Sprint dispatch-capacity (2026-05-21) — regression coverage for the
// scheduled_duration_hours field across the assignment surfaces.
//
// Covers:
//   • JobsView Edit/Create dialog round-trips scheduled_duration_hours
//   • JobsView submitForm coerces blank → null, value → Number
//   • DispatchView assignJob opens the duration prompt when both
//     scheduled_duration_hours and effective_duration_hours are missing
//   • DispatchView assignJob bypasses the prompt when duration is present
//   • DispatchView onTimelinePlace same-tech reschedule patches
//     scheduled_at only (no prompt, no tech change)
//
// Why this file exists: Doug 2026-05-21 — "Make tests so we don't get
// regression." Two prior incidents that motivated these specifically:
//   1. The dropdown-assignment path bypassed the prompt (audit caught
//      it pre-deploy 7e77f48a). The DispatchView block of tests below
//      pins the new gate at the assignJob level so any future refactor
//      that re-attaches to handleDrop only will fail this suite.
//   2. The 9:00am fake fallback in _doAssignJob made every no-time
//      drop land at 09:00 local. The same-tech-reschedule test pins
//      the new "midnight = tray" + "explicit ISO from timeline =
//      exact time" behavior.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises as vtuFlushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import JobsView from "../src/views/JobsView.vue";
import DispatchView from "../src/views/DispatchView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

// `route.query` is shared across the JobsView specs so a single test
// can flip ?edit=<id> on a fresh mount. The default is an empty query.
const mockRoute = { query: {}, path: "/jobs" };
vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => mockRoute,
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../src/composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: patchMock, del: delMock }),
}));

const jobsStubs = {
  AppLayout: { template: "<div><slot /></div>" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: { props: ["value"], template: "<div><slot /></div>" },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "type"],
    emits: ["click"],
    template: '<button :type="type || \'button\'" @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Textarea: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  ProgressSpinner: { template: "<div />" },
  DatePicker: { props: ["modelValue"], template: "<div />" },
  Select: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template: "<select />",
  },
  MultiSelect: { props: ["modelValue", "options"], emits: ["update:modelValue"], template: "<select multiple />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ToggleSwitch: { props: ["modelValue"], template: "<div />" },
  Toast: { template: "<div />" },
  Calendar: { props: ["modelValue"], template: "<div />" },
};

describe("JobsView — Estimated time (hours) field", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    patchMock.mockResolvedValue({ ok: true });
    postMock.mockResolvedValue({ id: 999 });
    getMock.mockImplementation(async (url) => {
      if (url.startsWith("/api/jobs")) return [];
      if (url.startsWith("/api/customers")) return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
  });

  it("emptyForm seeds scheduled_duration_hours as null (no estimate yet)", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    // Open create dialog so jobForm is reset to emptyForm()
    wrapper.vm.openCreateDialog?.();
    await vtuFlushPromises();
    // emptyForm is called on mount AND on dialog open. Confirm shape.
    expect(wrapper.vm.jobForm.scheduled_duration_hours).toBe(null);
  });

  it("openEditDialog populates scheduled_duration_hours from the job row", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42, title: "Door spring", customer_id: 1,
      scheduled_duration_hours: 1.75,
    });
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.scheduled_duration_hours).toBe(1.75);
  });

  it("openEditDialog handles missing scheduled_duration_hours as null (not undefined)", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({ id: 42, title: "Door spring", customer_id: 1 });
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.scheduled_duration_hours).toBe(null);
  });

  it("submitForm sends scheduled_duration_hours as Number when set", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({ id: 42, title: "Door spring", customer_id: 1 });
    await vtuFlushPromises();
    // Edit-mode populated; set duration as a string (as the InputText would).
    wrapper.vm.jobForm.scheduled_duration_hours = "2.5";
    await wrapper.vm.submitForm();
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [url, payload] = patchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/42");
    expect(payload).toMatchObject({ scheduled_duration_hours: 2.5 });
    // Number, not string.
    expect(typeof payload.scheduled_duration_hours).toBe("number");
  });

  it("submitForm sends scheduled_duration_hours as null when blank", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42, title: "Door spring", customer_id: 1,
      scheduled_duration_hours: 1.5,
    });
    await vtuFlushPromises();
    // User clears the field → empty string from InputText.
    wrapper.vm.jobForm.scheduled_duration_hours = "";
    await wrapper.vm.submitForm();
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [, payload] = patchMock.mock.calls[0];
    expect(payload.scheduled_duration_hours).toBe(null);
  });

  it("submitForm preserves 0 as 0 (does not clobber an explicit zero from external writers)", async () => {
    // Regression for /audit 2026-05-21 finding 2: the previous coercion
    // (Number(raw) > 0) silently rewrote 0 → null on every JobsView save,
    // dropping data from any non-JobsView writer (CSV importer, API
    // client) that stored 0 deliberately.
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42, title: "Door spring", customer_id: 1,
      scheduled_duration_hours: 0,
    });
    await vtuFlushPromises();
    // Form populated with 0 (form-coerced openEditDialog already ran).
    expect(wrapper.vm.jobForm.scheduled_duration_hours).toBe(0);
    await wrapper.vm.submitForm();
    const [, payload] = patchMock.mock.calls[0];
    expect(payload.scheduled_duration_hours).toBe(0);
  });

  it("?edit=<id> on mount opens the Edit dialog with that job (regression — Doug 2026-05-21)", async () => {
    // Pre-fix: JobsView only honored ?new=1. JobDetailView's Edit
    // button pushed /jobs?edit=<id>, JobsView ignored the query, and
    // the user landed on the jobs list instead of the edit dialog.
    const targetJob = {
      id: 99,
      title: "Spring replacement",
      customer_id: 7,
      customer_name: "Acme",
      status: "Scheduled",
      scheduled_duration_hours: 2,
    };
    getMock.mockImplementation(async (url) => {
      if (url.startsWith("/api/jobs")) return [targetJob];
      if (url.startsWith("/api/customers")) return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
    // Activate the query for this single test.
    mockRoute.query = { edit: "99" };
    try {
      const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
      await vtuFlushPromises();
      await vtuFlushPromises();
      expect(wrapper.vm.jobForm.id).toBe(99);
      expect(wrapper.vm.jobForm.title).toBe("Spring replacement");
      expect(wrapper.vm.jobForm.scheduled_duration_hours).toBe(2);
    } finally {
      mockRoute.query = {};
    }
  });

  it("submitForm sends scheduled_duration_hours=null when non-numeric junk", async () => {
    const wrapper = mount(JobsView, { global: { stubs: jobsStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({ id: 42, title: "Door spring", customer_id: 1 });
    await vtuFlushPromises();
    wrapper.vm.jobForm.scheduled_duration_hours = "not-a-number";
    await wrapper.vm.submitForm();
    const [, payload] = patchMock.mock.calls[0];
    expect(payload.scheduled_duration_hours).toBe(null);
  });
});

// ─────────────────────────────────────────────────────────────────────
// DispatchView — duration prompt gate + onTimelinePlace flow
// ─────────────────────────────────────────────────────────────────────

const dispatchStubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DatePicker: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<input type="date" />',
  },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Avatar: { props: ["label"], template: "<span>{{ label }}</span>" },
  Button: { props: ["label"], emits: ["click"], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  Drawer: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  Select: { props: ["modelValue", "options"], emits: ["change"], template: "<select />" },
  SelectButton: { props: ["modelValue", "options"], template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  InputText: { props: ["modelValue"], template: "<input />" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  TechTimelineColumn: { template: "<div />" },
  TechEfficiencyPanel: { template: "<div />" },
  MobileJobCloseoutDialog: { template: "<div />" },
};

const flushAll = async () => {
  await vtuFlushPromises();
  await vtuFlushPromises();
};

describe("DispatchView — duration prompt gate", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    patchMock.mockResolvedValue({ ok: true });
  });

  function setupBoard(jobOverrides = {}) {
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const TECHS = [{ id: "tech-1", user_id: "Mike" }];
    const JOBS = [{
      id: 101,
      status: "Scheduled",
      customer_name: "Acme",
      address: "123 Main",
      job_type: "Repair",
      time_window: "Anytime",
      technician_id: null,
      scheduled_at: `${today}T09:00:00`,
      ...jobOverrides,
    }];
    getMock.mockImplementation((url) => {
      const u = String(url || '');
      if (u.includes('/api/technicians')) return Promise.resolve(TECHS);
      if (u.includes('/api/jobs')) return Promise.resolve(JOBS);
      if (u.includes('/api/dispatch-settings')) return Promise.resolve({});
      return Promise.resolve([]);
    });
  }

  it("opens the duration prompt for a job without scheduled_duration_hours", async () => {
    setupBoard({ scheduled_duration_hours: null, effective_duration_hours: null });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    await wrapper.vm.assignJob(101, "tech-1");
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(true);
    expect(wrapper.vm.durationPromptJobId).toBe(101);
    expect(wrapper.vm.durationPromptTechId).toBe("tech-1");
    // The actual PATCH does not fire yet — gate is closed.
    expect(patchMock).not.toHaveBeenCalled();
  });

  it("bypasses the duration prompt when scheduled_duration_hours is set", async () => {
    setupBoard({ scheduled_duration_hours: 2.0 });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    await wrapper.vm.assignJob(101, "tech-1");
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(false);
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [url, payload] = patchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/101");
    expect(payload).toMatchObject({ assigned_to: "tech-1", technician_id: "tech-1" });
  });

  it("bypasses the duration prompt when only effective_duration_hours is set (estimate-derived)", async () => {
    setupBoard({ scheduled_duration_hours: null, effective_duration_hours: 1.5 });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    await wrapper.vm.assignJob(101, "tech-1");
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(false);
    expect(patchMock).toHaveBeenCalledTimes(1);
  });

  it("does not open the prompt for unassign (techId=null) even without duration", async () => {
    setupBoard({ scheduled_duration_hours: null, effective_duration_hours: null });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    await wrapper.vm.assignJob(101, null);
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(false);
  });
});

describe("DispatchView — onTimelinePlace", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    patchMock.mockResolvedValue({ ok: true });
  });

  function setupBoard(jobOverrides = {}) {
    const d = new Date();
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const TECHS = [
      { id: "tech-1", user_id: "Mike" },
      { id: "tech-2", user_id: "Burt" },
    ];
    const JOBS = [{
      id: 101,
      status: "Scheduled",
      customer_name: "Acme",
      job_type: "Repair",
      technician_id: "tech-1",
      assigned_tech_ids: ["tech-1"],
      scheduled_at: `${today}T09:00:00`,
      scheduled_duration_hours: 2,
      effective_duration_hours: 2,
      ...jobOverrides,
    }];
    getMock.mockImplementation((url) => {
      const u = String(url || '');
      if (u.includes('/api/technicians')) return Promise.resolve(TECHS);
      if (u.includes('/api/jobs')) return Promise.resolve(JOBS);
      if (u.includes('/api/dispatch-settings')) return Promise.resolve({});
      return Promise.resolve([]);
    });
  }

  it("same-tech reschedule with existing duration patches scheduled_at only (no prompt)", async () => {
    setupBoard();
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    const newSlot = new Date();
    newSlot.setHours(11, 30, 0, 0);
    await wrapper.vm.onTimelinePlace({
      jobId: 101, techId: "tech-1", startISO: newSlot.toISOString(),
    });
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(false);
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [url, payload] = patchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/101");
    // Pure scheduled_at patch — NO assigned_to / technician_id keys.
    expect(payload).toEqual({ scheduled_at: newSlot.toISOString() });
  });

  it("cross-tech place fires duration prompt when the moving job lacks hours", async () => {
    setupBoard({
      technician_id: "tech-1",
      assigned_tech_ids: ["tech-1"],
      scheduled_duration_hours: null,
      effective_duration_hours: null,
    });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    const newSlot = new Date();
    newSlot.setHours(10, 0, 0, 0);
    await wrapper.vm.onTimelinePlace({
      jobId: 101, techId: "tech-2", startISO: newSlot.toISOString(),
    });
    await flushAll();
    expect(wrapper.vm.durationPromptOpen).toBe(true);
    expect(wrapper.vm.durationPromptScheduledAt).toBe(newSlot.toISOString());
  });

  it("onTimelinePlaceTray sets scheduled_at to midnight (tray placement, NOT 9am)", async () => {
    setupBoard({
      technician_id: null,
      assigned_tech_ids: [],
      scheduled_duration_hours: 1,
      effective_duration_hours: 1,
    });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await flushAll();
    await wrapper.vm.onTimelinePlaceTray({ jobId: 101, techId: "tech-2" });
    await flushAll();
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [, payload] = patchMock.mock.calls[0];
    // payload.scheduled_at is an ISO string; parsing back to local should
    // give midnight, NOT 9am (regression on the old hardcoded 9am fake).
    const d = new Date(payload.scheduled_at);
    expect(d.getHours()).toBe(0);
    expect(d.getMinutes()).toBe(0);
  });
});
