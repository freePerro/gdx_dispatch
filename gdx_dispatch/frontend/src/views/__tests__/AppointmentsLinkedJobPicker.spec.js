/**
 * Appointments page — the Linked Job and Customer pickers search the server
 * (GDXA-1965). They used to load `/api/jobs?page_size=200` and
 * `/api/customers?per_page=500` once and filter in the browser, so a job past
 * row 200 could not be linked and an edited appointment's older job showed no
 * label.
 *
 * Real PrimeVue Select (tests/setup.js installs PrimeVue); dialogs stubbed.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";

const getMock = vi.fn();
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("vue-router", () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("../../stores/auth", () => ({ useAuthStore: () => ({ user: { role: "dispatcher" } }) }));

const RECENT_JOB = { id: "j-recent", job_number: "JOB-2026-0500", title: "Spring swap", customer_name: "Ada" };
const OLD_JOB = {
  id: "j-old", job_number: "JOB-2024-0007", title: "Opener install", customer_name: "Grace",
  customer_id: "c-grace", address: "1 Elm St",
};
const OLD_CUSTOMER = { id: "c-old", name: "Old Customer", phone: "(555) 123-4567" };

function routeGet(url) {
  if (url.startsWith("/api/appointments/unconfirmed")) return Promise.resolve([]);
  if (url.startsWith("/api/appointments")) return Promise.resolve([]);
  if (url === "/api/technicians") return Promise.resolve([]);
  if (url === "/api/jobs/j-old") return Promise.resolve(OLD_JOB);
  if (url === "/api/customers/c-old") return Promise.resolve(OLD_CUSTOMER);
  if (url.startsWith("/api/jobs?")) {
    const search = new URLSearchParams(url.split("?")[1]).get("search");
    return Promise.resolve({ items: search ? (search.includes("Grace") ? [OLD_JOB] : []) : [RECENT_JOB] });
  }
  if (url.startsWith("/api/customers?")) return Promise.resolve({ items: [] });
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
        UndoArrivalDialog: true,
      },
    },
  });
  await flushPromises();
  return w;
}

function selectByTestId(w, testid) {
  return w.findAllComponents({ name: "Select" }).find((c) => c.attributes("data-testid") === testid);
}

const urls = () => getMock.mock.calls.map((c) => c[0]);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

beforeEach(() => {
  getMock.mockReset().mockImplementation(routeGet);
});

describe("Appointments page — linked job and customer pickers", () => {
  it("loads a first page and never the capped bulk lists", async () => {
    await mountView();
    expect(urls()).toContain("/api/jobs?page_size=50");
    expect(urls()).toContain("/api/customers?per_page=50");
    expect(urls().some((u) => u.includes("page_size=200") || u.includes("per_page=500"))).toBe(false);
  });

  it("typing in the job picker asks the server and lists a job outside the first page", async () => {
    const w = await mountView();
    w.vm.openCreate();
    await flushPromises();
    const select = selectByTestId(w, "appointment-job-select");
    expect(select.props("options").map((o) => o.value)).toEqual(["j-recent"]);
    // Select's own client filter must not hide a server match on customer name.
    expect(select.props("filterFields")).toEqual(["searchText"]);

    select.vm.$emit("filter", { value: "Grace" });
    await wait(300);
    await flushPromises();

    expect(urls()).toContain("/api/jobs?page_size=50&search=Grace");
    const opts = selectByTestId(w, "appointment-job-select").props("options");
    expect(opts).toEqual([
      { value: "j-old", label: "JOB-2024-0007 — Opener install", searchText: "Grace JOB-2024-0007 Opener install Grace" },
    ]);
  });

  it("picking a searched job fills customer and address from it", async () => {
    const w = await mountView();
    w.vm.openCreate();
    await flushPromises();
    const select = selectByTestId(w, "appointment-job-select");
    select.vm.$emit("filter", { value: "Grace" });
    await wait(300);
    await flushPromises();

    select.vm.$emit("update:modelValue", "j-old");
    select.vm.$emit("change", { value: "j-old" });
    await flushPromises();

    expect(w.vm.form.customer_id).toBe("c-grace");
    expect(w.vm.form.address).toBe("1 Elm St");
  });

  it("editing an appointment linked to an older job and customer shows both labels", async () => {
    const w = await mountView();
    w.vm.openEdit({ id: "a1", title: "Visit", job_id: "j-old", customer_id: "c-old" });
    await flushPromises();

    expect(urls()).toContain("/api/jobs/j-old");
    expect(urls()).toContain("/api/customers/c-old");
    const jobOpts = selectByTestId(w, "appointment-job-select").props("options");
    expect(jobOpts[0]).toMatchObject({ value: "j-old", label: "JOB-2024-0007 — Opener install" });
    const custOpts = selectByTestId(w, "appointment-customer-select").props("options");
    expect(custOpts[0]).toMatchObject({ value: "c-old", label: "Old Customer" });
    expect(custOpts[0].searchText).toContain("5551234567");
    expect(w.text()).toContain("JOB-2024-0007 — Opener install");
  });
});
