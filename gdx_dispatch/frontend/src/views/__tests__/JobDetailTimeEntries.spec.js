/**
 * Job Detail Time Entries card — add / edit / delete (GDXA-1065).
 *
 * The card was read-only: POST /api/jobs/{id}/time-entries and
 * PATCH/DELETE /api/time-entries/{id} (routers/labor.py) had no UI caller.
 * Dispatch managers get the actions; a billed, day-closed or tech-owned (timer, closeout) row is
 * locked; the card names the table it edits. Harness copied from
 * JobDetailCrewUndo.spec.js, with a DataTable stub that renders each row
 * through its Column body slots.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { h, provide, inject, defineComponent } from "vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
const confirmAsync = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: { id: "job-1" }, query: {}, path: "/jobs/job-1" }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    del: delMock,
    request: vi.fn(),
  }),
}));
vi.mock("../../composables/useDestructiveConfirm", () => ({
  useDestructiveConfirm: () => ({ confirmAsync, confirmDestructive: vi.fn() }),
}));
let role = "dispatcher";
vi.mock("../../stores/auth", () => ({
  useAuthStore: () => ({ user: { role }, hasPermission: () => true }),
}));
// labor.py's writes are gated on the timeclock module (GDXA-1065 audit 3).
let modules = {};
vi.mock("../../composables/useTenantModules", () => ({
  useTenantModules: () => ({ isEnabled: (k) => modules[k] !== false }),
}));

const ENTRIES = [
  {
    id: "e1", tech_id: "t1", tech_name: "Mike", clock_in: "2026-10-05T13:00:00Z",
    clock_out: "2026-10-05T15:00:00Z", duration_minutes: 120, labor_cost: 100,
    entry_type: "manual", billed_invoice_id: null,
  },
  {
    id: "e2", tech_id: "t2", tech_name: "Sam", clock_in: "2026-10-04T13:00:00Z",
    clock_out: "2026-10-04T14:00:00Z", duration_minutes: 60, labor_cost: 50,
    billed_invoice_id: "inv-9", day_closed_at: "2026-10-04T23:00:00Z",
  },
  {
    id: "e3", tech_id: "t1", tech_name: "Mike", clock_in: "2026-10-03T13:00:00Z",
    clock_out: "2026-10-03T13:00:00Z", duration_minutes: 0, labor_cost: 0,
    entry_type: "job", billed_invoice_id: null, day_closed_at: "2026-10-03T23:00:00Z",
  },
  {
    id: "e4", tech_id: "t2", tech_name: "Sam", clock_in: "2026-10-06T13:00:00Z",
    clock_out: null, duration_minutes: null, labor_cost: 0,
    entry_type: "job", billed_invoice_id: null, day_closed_at: null,
  },
  {
    // a final closeout with no arrival timer writes its attested row as "work"
    id: "e5", tech_id: "t1", tech_name: "Mike", clock_in: "2026-10-07T13:00:00Z",
    clock_out: "2026-10-07T15:00:00Z", duration_minutes: 120, labor_cost: 100,
    entry_type: "work", billed_invoice_id: null, day_closed_at: null,
  },
];

function routeGet({ entries = ENTRIES } = {}) {
  getMock.mockImplementation(async (url) => {
    const u = String(url);
    if (u.startsWith("/api/customers/")) return { id: "cust-1", name: "Acme Door Co" };
    if (u === "/api/labor/jobs/job-1/time-entries") return { items: entries, total: entries.length };
    if (u.startsWith("/api/technicians")) return [{ id: "t1", name: "Mike" }, { id: "t2", name: "Sam" }];
    if (u === "/api/jobs/job-1") {
      return { id: "job-1", title: "Spring replacement", status: "Scheduled", customer_id: "cust-1" };
    }
    return [];
  });
}

const ROW = Symbol("row");
const RowScope = defineComponent({
  props: ["row"],
  setup(props, { slots }) {
    provide(ROW, props.row);
    return () => slots.default?.();
  },
});
const DataTableStub = defineComponent({
  props: ["value"],
  setup(props, { slots }) {
    return () => h("div", (props.value || []).map((row) => h(RowScope, { row, key: row.id }, slots)));
  },
});
const ColumnStub = defineComponent({
  setup(_, { slots }) {
    const row = inject(ROW, null);
    return () => (row && slots.body ? h("div", slots.body({ data: row })) : null);
  },
});

async function mountView() {
  const { default: View } = await import("../JobDetailView.vue");
  const w = mount(View, {
    global: {
      stubs: {
        RouterLink: { props: ["to"], template: "<a :href=\"to\"><slot /></a>" },
        Button: { props: ["label"], template: '<button v-bind="$attrs">{{ label }}</button>' },
        Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
        DataTable: DataTableStub, Column: ColumnStub,
        Dialog: { props: ['visible', 'header'], template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>', inheritAttrs: false },
        MobileJobCloseoutDialog: true, Select: true, InputText: true, MultiSelect: true,
        Textarea: true, DatePicker: true, FileUpload: true, ProgressSpinner: true,
        Tabs: true, TabList: true, Tab: true, Message: true, Checkbox: true,
        JobStateChip: true, JobStateOverrideDialog: true, CatalogPickerDialog: true,
        DoorSpecList: true, PhoneInput: true, EmailTimeline: true, InputNumber: true,
        AuthedImage: true, UndoArrivalDialog: true, JobVisitsCard: true, JobDailyLogCard: true,
      },
    },
  });
  await flushPromises();
  w.vm.activeTab = "costing"; // the card lives on the Costing tab
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  role = "dispatcher";
  modules = {};
});

describe("job page Time Entries card — writes", () => {
  it("names the table it edits", async () => {
    routeGet();
    const w = await mountView();
    const note = w.get('[data-testid="time-entry-scope-note"]').text();
    // payroll is the time clock's table, not this one (Doug, 2026-10-15)
    expect(note).toContain("Payroll is paid from the time clock");
    // mobile_invoicing.py sums every closed row into the tech's "Labor hours".
    expect(note).toContain("invoicing screen");
  });

  it("offers edit and delete on an unbilled row and a lock on a billed one", async () => {
    routeGet();
    const w = await mountView();
    expect(w.find('[data-testid="time-entry-edit-e1"]').exists()).toBe(true);
    expect(w.find('[data-testid="time-entry-delete-e1"]').exists()).toBe(true);
    expect(w.find('[data-testid="time-entry-edit-e2"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-delete-e2"]').exists()).toBe(false);
    expect(w.get('[data-testid="time-entry-billed-e2"]').text()).toContain("Billed");
    expect(w.find('[data-testid="time-entry-day-closed-e2"]').exists()).toBe(false);
  });

  it("locks a day-closed row even before any invoice claims it", async () => {
    routeGet();
    const w = await mountView();
    expect(w.get('[data-testid="time-entry-day-closed-e3"]').text()).toContain("Day closed");
    expect(w.find('[data-testid="time-entry-edit-e3"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-delete-e3"]').exists()).toBe(false);
  });

  it("locks a tech's running job timer — day close reads it", async () => {
    routeGet();
    const w = await mountView();
    expect(w.get('[data-testid="time-entry-tech-e4"]').text()).toContain("Tech time");
    expect(w.find('[data-testid="time-entry-edit-e4"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-delete-e4"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-tech-e1"]').exists()).toBe(false);
  });

  it("locks a closeout's attested row — only office-entered rows are editable", async () => {
    routeGet();
    const w = await mountView();
    expect(w.get('[data-testid="time-entry-tech-e5"]').text()).toContain("Tech time");
    expect(w.find('[data-testid="time-entry-edit-e5"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-delete-e5"]').exists()).toBe(false);
  });

  it("locks a 'manual' row that carries a user_id — that is a tech's own time", async () => {
    const e6 = { ...ENTRIES[0], id: "e6", user_id: "u-77" };
    routeGet({ entries: [ENTRIES[0], e6] });
    const w = await mountView();
    expect(w.get('[data-testid="time-entry-tech-e6"]').text()).toContain("Tech time");
    expect(w.find('[data-testid="time-entry-edit-e6"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-edit-e1"]').exists()).toBe(true);
  });

  it("the tech list follows the row being edited, not the first one opened", async () => {
    const e7 = { ...ENTRIES[0], id: "e7", tech_id: "t-gone", tech_name: "Former Tech" };
    routeGet({ entries: [ENTRIES[0], e7] });
    const w = await mountView();
    await w.get('[data-testid="time-entry-edit-e1"]').trigger("click");
    expect(w.vm.timeEntryTechOptions.map((o) => o.value)).toEqual(["t1", "t2"]);
    await w.get('[data-testid="time-entry-edit-e7"]').trigger("click");
    expect(w.vm.timeEntryTechOptions[0]).toEqual({ label: "Former Tech", value: "t-gone" });
  });

  it("adds a manual entry with clock_out = start + hours", async () => {
    routeGet();
    postMock.mockResolvedValue({});
    const w = await mountView();
    await w.get('[data-testid="time-entry-add"]').trigger("click");
    w.vm.timeEntryForm.tech_id = "t1";
    w.vm.timeEntryForm.clock_in = new Date("2026-10-06T14:00:00Z");
    w.vm.timeEntryForm.hours = 1.5;
    await w.vm.$nextTick();
    await w.get('[data-testid="time-entry-save"]').trigger("click");
    await flushPromises();
    expect(postMock).toHaveBeenCalledWith(
      "/api/jobs/job-1/time-entries",
      {
        tech_id: "t1",
        clock_in: "2026-10-06T14:00:00.000Z",
        clock_out: "2026-10-06T15:30:00.000Z",
      },
      { successMessage: "Time entry added" },
    );
  });

  it("an edit sends only what changed — the tech is not re-sent", async () => {
    routeGet();
    patchMock.mockResolvedValue({});
    const w = await mountView();
    await w.get('[data-testid="time-entry-edit-e1"]').trigger("click");
    expect(w.vm.timeEntryForm.hours).toBe(2);
    w.vm.timeEntryForm.hours = 3;
    await w.get('[data-testid="time-entry-save"]').trigger("click");
    await flushPromises();
    expect(patchMock).toHaveBeenCalledWith(
      "/api/time-entries/e1",
      { clock_in: "2026-10-05T13:00:00.000Z", clock_out: "2026-10-05T16:00:00.000Z" },
      { successMessage: "Time entry saved" },
    );
  });

  it("deletes after the confirm, and not without it", async () => {
    routeGet();
    delMock.mockResolvedValue({ deleted: true });
    confirmAsync.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const w = await mountView();
    await w.get('[data-testid="time-entry-delete-e1"]').trigger("click");
    await flushPromises();
    expect(delMock).not.toHaveBeenCalled();
    await w.get('[data-testid="time-entry-delete-e1"]').trigger("click");
    await flushPromises();
    expect(delMock).toHaveBeenCalledWith("/api/time-entries/e1", { successMessage: "Time entry deleted" });
  });

  it("is not offered to a role the server would refuse", async () => {
    role = "technician";
    routeGet();
    const w = await mountView();
    expect(w.find('[data-testid="time-entry-add"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-edit-e1"]').exists()).toBe(false);
  });

  it("is not offered when the timeclock module the endpoints need is off", async () => {
    modules = { timeclock: false };
    routeGet();
    const w = await mountView();
    expect(w.find('[data-testid="time-entry-add"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-edit-e1"]').exists()).toBe(false);
    expect(w.find('[data-testid="time-entry-delete-e1"]').exists()).toBe(false);
    // the read-only list still renders: it is served through the jobs module
    expect(w.find('[data-testid="time-entry-scope-note"]').exists()).toBe(true);
  });
});
