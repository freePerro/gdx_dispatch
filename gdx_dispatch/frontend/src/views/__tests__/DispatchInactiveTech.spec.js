/**
 * DispatchView — an inactive tech is not assignable, but stays on the board
 * while a job references them (GDXA-1255).
 *
 * normalizeTech kept id/name/skills/shift and dropped `active`, so every
 * `tech.active !== false` filter read undefined and passed: inactive techs
 * were pickable in the queue card's dropdown and the Assign dialog, and had
 * a day column and a week row with no job on them.
 *
 * Pinned:
 *  1. The queue card's "Assign to tech" options leave the inactive tech out.
 *  2. With no job on them, the inactive tech has no day column and no week row.
 *  3. A job assigned to the inactive tech still shows them by name in the day
 *     column and the week row, and they still cannot be picked.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DispatchView from "../DispatchView.vue";

const getMock = vi.fn();
let granted = new Set();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/dispatch" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
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
  template: "<div data-testid='timeline-stub'>{{ tech?.name }}</div>",
};

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DatePicker: { props: ["modelValue"], template: '<input type="date" />' },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Avatar: { props: ["label"], template: "<span>{{ label }}</span>" },
  Button: { props: ["label"], emits: ["click"], template: '<button @click="$emit(\'click\', $event)">{{ label }}</button>' },
  Dialog: { props: ["visible"], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
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
const at = (h) => { const d = new Date(); d.setHours(h, 0, 0, 0); return d; };

const BASE = {
  title: "Spring replacement", job_type: "Service", status: "Scheduled",
  customer_id: "c1", customer_name: "Lee Park", holding_area_id: null,
  scheduled_duration_hours: 1, effective_duration_hours: 1,
};
// Undated and unassigned: sits in the intake queue with an assign dropdown.
const QUEUED = { ...BASE, id: "job-q", scheduled_at: null, assigned_to: null, technician_id: null, assigned_tech_ids: [] };
const ON_INACTIVE = {
  ...BASE, id: "job-old", scheduled_at: at(10).toISOString(),
  assigned_to: "tech-gone", technician_id: "tech-gone", lead_tech_id: "tech-gone", assigned_tech_ids: ["tech-gone"],
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
      { id: "tech-gone", name: "Former Tech", active: false, effective_shift_start: "08:00", effective_shift_end: "17:00" },
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

const queueOptions = (w) => w.findAllComponents(stubs.Select)
  .find((c) => c.attributes("data-testid") === "assign-dropdown-job-q")
  .props("options").map((t) => t.id);
const columnIds = (w) => w.findAllComponents(TechTimelineColumn).map((c) => String(c.props("tech")?.id));
const weekRowIds = (w) => w.vm.$.setupState.weekRows.map((r) => r.id);

describe("DispatchView — inactive techs", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    granted = new Set(["jobs.read_all"]);
  });

  it("leaves an inactive tech out of the queue card's assign options", async () => {
    boardRows = [QUEUED];
    const w = await mountBoard();
    expect(queueOptions(w)).toEqual(["tech-1"]);
    expect(w.vm.$.setupState.assignableTechnicians.map((t) => t.id)).toEqual(["tech-1"]);
  });

  it("gives an inactive tech with no job no day column and no week row", async () => {
    boardRows = [QUEUED];
    const day = await mountBoard();
    expect(columnIds(day)).toEqual(["tech-1"]);
    const week = await mountBoard("week");
    expect(weekRowIds(week)).toEqual(["tech-1", "unassigned"]);
  });

  it("still shows an inactive tech by name while a job references them, and still cannot pick them", async () => {
    boardRows = [QUEUED, ON_INACTIVE];
    const day = await mountBoard();
    expect(columnIds(day)).toEqual(["tech-1", "tech-gone"]);
    expect(day.text()).toContain("Former Tech");
    expect(queueOptions(day)).toEqual(["tech-1"]);
    const week = await mountBoard("week");
    const row = week.vm.$.setupState.weekRows.find((r) => r.id === "tech-gone");
    expect(row?.name).toBe("Former Tech");
  });
});
