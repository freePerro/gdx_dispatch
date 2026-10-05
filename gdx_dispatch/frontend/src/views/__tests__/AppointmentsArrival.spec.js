/**
 * Appointments page — arrivals (multi-day jobs plan §5.2a, *Appointments
 * page*, *Arrival is always recorded*).
 *
 * - A tapped visit (arrived_at set, status still "scheduled") reads
 *   "On site since h:mm" and offers Complete, not Confirm.
 * - An old status-only "arrived" row reads "Arrived — time not recorded" and
 *   offers Enter arrival time and Undo arrival.
 * - Office Arrived asks the time (prefilled now) and POSTs {arrived_at}.
 * - Cancel answered 409 needs_answer / status_only_arrival shows the
 *   question, not a toast.
 *
 * Real DataTable/Column (tests/setup.js installs PrimeVue); dialogs stubbed.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: patchMock, del: vi.fn() }),
}));
const toastAdd = vi.fn();
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock("vue-router", () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
let role = "dispatcher";
vi.mock("../../stores/auth", () => ({ useAuthStore: () => ({ user: { role } }) }));

const ROWS = [
  { id: "tapped", title: "Spring swap", tech_id: "t1", status: "scheduled",
    start_at: "2026-10-05T13:00:00Z", arrived_at: "2026-10-05T13:14:00Z" },
  { id: "old", title: "Opener", tech_id: "t1", status: "arrived",
    start_at: "2026-10-04T13:00:00Z", arrived_at: null },
  { id: "enroute", title: "Tune-up", tech_id: "t1", status: "en_route",
    start_at: "2026-10-05T15:00:00Z", arrived_at: null },
  { id: "done", title: "Install", tech_id: "t1", status: "completed",
    start_at: "2026-10-03T13:00:00Z", arrived_at: "2026-10-03T13:05:00Z" },
];

function routeGet(url) {
  if (url.startsWith("/api/appointments/unconfirmed")) return Promise.resolve([]);
  if (url.startsWith("/api/appointments")) return Promise.resolve(ROWS.map((r) => ({ ...r })));
  if (url === "/api/technicians") return Promise.resolve([{ id: "t1", name: "Mike" }]);
  return Promise.resolve([]);
}

async function mountView() {
  const { default: View } = await import("../AppointmentsView.vue");
  const w = mount(View, {
    global: {
      directives: { tooltip: {} },
      stubs: {
        Toolbar: { template: "<div><slot name='start' /><slot name='end' /></div>" },
        DatePicker: true,
        Tabs: true, TabList: true, Tab: true,
        Dialog: {
          props: ["visible", "header"],
          template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
        },
        UndoArrivalDialog: {
          props: ["modelValue", "appointment", "mode", "techName"],
          template: '<div v-if="modelValue" data-testid="undo-dialog-stub">{{ mode }}:{{ appointment && appointment.id }}:{{ techName }}</div>',
        },
      },
    },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  role = "dispatcher";
  getMock.mockReset().mockImplementation(routeGet);
  postMock.mockReset().mockResolvedValue({});
  patchMock.mockReset().mockResolvedValue({});
  toastAdd.mockReset();
});

describe("Appointments page — arrivals", () => {
  it("a tapped visit reads On site since and offers Complete", async () => {
    const w = await mountView();
    expect(w.get('[data-testid="on-site-tapped"]').text()).toMatch(/^On site since \d{1,2}:\d{2}/);
    expect(w.get('[data-testid="row-action-tapped"]').text()).toBe("Complete");
    await w.get('[data-testid="row-action-tapped"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0][0]).toBe("/api/appointments/tapped/complete");
    // A closed visit is not "on site".
    expect(w.find('[data-testid="on-site-done"]').exists()).toBe(false);
  });

  it("an old status-only row reads time not recorded, with Enter time and Undo", async () => {
    const w = await mountView();
    expect(w.get('[data-testid="no-time-old"]').text()).toBe("Arrived — time not recorded");
    expect(w.find('[data-testid="row-action-old"]').exists()).toBe(false);
    expect(w.find('[data-testid="undo-arrival-old"]').exists()).toBe(true);
    await w.get('[data-testid="enter-time-old"]').trigger("click");
    await w.get('[data-testid="arrival-save"]').trigger("click");
    await flushPromises();
    const [url, body] = patchMock.mock.calls[0];
    expect(url).toBe("/api/appointments/old");
    expect(Number.isNaN(Date.parse(body.arrived_at))).toBe(false);
  });

  it("office Arrived asks the time, defaulting to now, and POSTs arrived_at", async () => {
    const w = await mountView();
    expect(w.get('[data-testid="row-action-enroute"]').text()).toBe("Arrived");
    const before = Date.now();
    await w.get('[data-testid="row-action-enroute"]').trigger("click");
    expect(postMock).not.toHaveBeenCalled();
    await w.get('[data-testid="arrival-save"]').trigger("click");
    await flushPromises();
    const [url, body] = postMock.mock.calls[0];
    expect(url).toBe("/api/appointments/enroute/arrived");
    expect(Math.abs(Date.parse(body.arrived_at) - before)).toBeLessThan(5000);
  });

  it("a refused Arrived shows the server's sentence in the dialog", async () => {
    postMock.mockRejectedValueOnce(Object.assign(new Error("An arrival is already recorded at 8:14 AM."), {
      status: 409, body: { detail: "An arrival is already recorded at 8:14 AM.", code: "arrival_recorded" } }));
    const w = await mountView();
    await w.get('[data-testid="row-action-enroute"]').trigger("click");
    await w.get('[data-testid="arrival-save"]').trigger("click");
    await flushPromises();
    expect(w.get('[data-testid="arrival-error"]').text()).toBe("An arrival is already recorded at 8:14 AM.");
  });

  it("Cancel answered needs_answer shows the question, not a toast", async () => {
    vi.spyOn(window, "prompt").mockReturnValue("Customer request");
    postMock.mockRejectedValueOnce(Object.assign(new Error("Enter its arrival time or undo the arrival first."), {
      status: 409, body: { detail: "Enter its arrival time or undo the arrival first.",
        code: "needs_answer", question: "status_only_arrival", visit_ids: ["tapped"] } }));
    const w = await mountView();
    const row = w.findAll("tr").find((tr) => tr.text().includes("Spring swap"));
    await row.get('button[aria-label="Cancel appointment"]').trigger("click");
    await flushPromises();
    expect(postMock.mock.calls[0][2]).toMatchObject({ suppressErrorToast: true });
    expect(w.get('[data-testid="needs-answer-text"]').text()).toBe("Enter its arrival time or undo the arrival first.");
    expect(toastAdd).not.toHaveBeenCalled();
    await w.get('[data-testid="needs-answer-undo"]').trigger("click");
    expect(w.get('[data-testid="undo-dialog-stub"]').text()).toBe("visit:tapped:Mike");
  });

  it("an old status-only row asks before Cancel even posts", async () => {
    const promptSpy = vi.spyOn(window, "prompt");
    promptSpy.mockClear();
    const w = await mountView();
    const row = w.findAll("tr").find((tr) => tr.text().includes("Opener"));
    await row.get('button[aria-label="Cancel appointment"]').trigger("click");
    expect(promptSpy).not.toHaveBeenCalled();
    expect(postMock).not.toHaveBeenCalled();
    expect(w.find('[data-testid="needs-answer-text"]').exists()).toBe(true);
  });

  it("Undo arrival is offered only to dispatch managers", async () => {
    role = "technician";
    const w = await mountView();
    expect(w.find('[data-testid="undo-arrival-tapped"]').exists()).toBe(false);
    expect(w.find('[data-testid="needs-answer-undo"]').exists()).toBe(false);
  });
});
