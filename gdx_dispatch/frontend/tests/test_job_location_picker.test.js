// Sprint customer-multi-location (2026-05-21) — regression coverage for
// binding a job to a specific customer_locations row.
//
// Covers:
//   • JobsView emptyForm seeds location_id = null
//   • openEditDialog populates location_id from the job row
//   • Watching customer_id triggers a locations fetch
//   • Switching customer in a live dialog clears location_id (peer-customer guard)
//   • submitForm payload carries location_id
//   • CustomersView "N sites" badge shows only when location_count > 1
//   • DispatchView displayCustomer suffixes the location label when present

import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises as vtuFlushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import JobsView from "../src/views/JobsView.vue";
import CustomersView from "../src/views/CustomersView.vue";
import DispatchView from "../src/views/DispatchView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

const mockRoute = { query: {}, path: "/jobs" };
vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => mockRoute,
  RouterLink: { template: "<a><slot /></a>" },
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock("primevue/useconfirm", () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock("../src/composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: getMock, post: postMock, patch: patchMock, del: delMock }),
}));
vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock, del: delMock }),
}));

const baseStubs = {
  AppLayout: { template: "<div><slot /></div>" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: { props: ["value"], template: "<div><slot /></div>" },
  Column: { template: "<div><slot /></div>" },
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
  Select: { props: ["modelValue", "options"], emits: ["update:modelValue"], template: "<select />" },
  MultiSelect: { props: ["modelValue", "options"], emits: ["update:modelValue"], template: "<select multiple />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ToggleSwitch: { props: ["modelValue"], template: "<div />" },
  Toast: { template: "<div />" },
  Calendar: { props: ["modelValue"], template: "<div />" },
};

// ─────────────────────────────────────────────────────────────────────
// JobsView — picker shape + payload round-trip
// ─────────────────────────────────────────────────────────────────────

describe("JobsView — location_id picker", () => {
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
      if (url === "/api/customers?per_page=1000") return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      // No locations by default — opt-in per test below.
      if (url.includes("/locations")) return [];
      return [];
    });
  });

  it("emptyForm seeds location_id as null", async () => {
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openCreateDialog?.();
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.location_id).toBe(null);
  });

  it("openEditDialog populates location_id from the job row", async () => {
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42,
      title: "Door spring",
      customer_id: "cust-1",
      location_id: "loc-abc",
    });
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.location_id).toBe("loc-abc");
  });

  it("customer_id watch fetches that customer's locations", async () => {
    const LOCS = [
      { id: "loc-1", label: "Primary", address: "1 First", is_primary: true },
      { id: "loc-2", label: "Warehouse", address: "2 Second", is_primary: false },
    ];
    getMock.mockImplementation(async (url) => {
      if (url === "/api/customers/cust-1/locations") return LOCS;
      if (url.startsWith("/api/jobs")) return [];
      if (url === "/api/customers?per_page=1000") return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openCreateDialog?.();
    wrapper.vm.jobForm.customer_id = "cust-1";
    await vtuFlushPromises();
    await vtuFlushPromises();
    expect(wrapper.vm.customerLocations.length).toBe(2);
    expect(getMock).toHaveBeenCalledWith("/api/customers/cust-1/locations");
  });

  it("switching customer in a live dialog clears stale location_id (peer-customer guard)", async () => {
    getMock.mockImplementation(async (url) => {
      if (url === "/api/customers/cust-1/locations") return [
        { id: "loc-1", label: "L1", address: "a", is_primary: true },
      ];
      if (url === "/api/customers/cust-2/locations") return [
        { id: "loc-9", label: "L9", address: "b", is_primary: true },
      ];
      if (url.startsWith("/api/jobs")) return [];
      if (url === "/api/customers?per_page=1000") return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42, title: "Repair", customer_id: "cust-1", location_id: "loc-1",
    });
    await vtuFlushPromises();
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.location_id).toBe("loc-1");
    // User picks a different customer.
    wrapper.vm.jobForm.customer_id = "cust-2";
    await vtuFlushPromises();
    await vtuFlushPromises();
    // Stale location_id is wiped — it belonged to cust-1, the backend would 400.
    expect(wrapper.vm.jobForm.location_id).toBe(null);
  });

  it("submitForm includes location_id in the PATCH payload", async () => {
    getMock.mockImplementation(async (url) => {
      if (url === "/api/customers/cust-1/locations") return [
        { id: "loc-1", label: "L1", address: "a", is_primary: true },
        { id: "loc-2", label: "L2", address: "b", is_primary: false },
      ];
      if (url.startsWith("/api/jobs")) return [];
      if (url === "/api/customers?per_page=1000") return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({
      id: 42, title: "Repair", customer_id: "cust-1", location_id: "loc-2",
    });
    await vtuFlushPromises();
    await vtuFlushPromises();
    await wrapper.vm.submitForm();
    expect(patchMock).toHaveBeenCalledTimes(1);
    const [url, payload] = patchMock.mock.calls[0];
    expect(url).toBe("/api/jobs/42");
    expect(payload.location_id).toBe("loc-2");
  });

  it("edit job A then edit job B (different customers) preserves job B's seeded location_id", async () => {
    // /audit catch 2026-05-21: the prev-not-null watcher guard only
    // protects the very first edit per page load. Opening job B without
    // closing the dialog had prev=A.customer, next=B.customer — both
    // truthy — and the watcher silently nulled B's seeded location.
    // Symptom: dispatcher reopens, location field looks blank, hits Save,
    // backend writes NULL. Pure UI race, no error.
    getMock.mockImplementation(async (url) => {
      if (url === "/api/customers/cust-A/locations") return [
        { id: "loc-A1", label: "A1", address: "a", is_primary: true },
        { id: "loc-A2", label: "A2", address: "a2", is_primary: false },
      ];
      if (url === "/api/customers/cust-B/locations") return [
        { id: "loc-B1", label: "B1", address: "b", is_primary: true },
        { id: "loc-B2", label: "B2", address: "b2", is_primary: false },
      ];
      if (url.startsWith("/api/jobs")) return [];
      if (url === "/api/customers?per_page=1000") return [];
      if (url === "/api/technicians") return [];
      if (url === "/api/dispatch-settings") return {};
      return [];
    });
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    // Open job A.
    wrapper.vm.openEditDialog({
      id: 1, title: "Job A", customer_id: "cust-A", location_id: "loc-A2",
    });
    await vtuFlushPromises();
    await vtuFlushPromises();
    expect(wrapper.vm.jobForm.location_id).toBe("loc-A2");
    // Without closing the dialog, open job B.
    wrapper.vm.openEditDialog({
      id: 2, title: "Job B", customer_id: "cust-B", location_id: "loc-B2",
    });
    await vtuFlushPromises();
    await vtuFlushPromises();
    // Job B's location MUST still be seeded — the watcher must NOT have
    // wiped it just because customer_id changed.
    expect(wrapper.vm.jobForm.location_id).toBe("loc-B2");
  });

  it("submitForm sends null location_id when none is selected", async () => {
    const wrapper = mount(JobsView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    wrapper.vm.openEditDialog({ id: 42, title: "Repair", customer_id: "cust-1" });
    await vtuFlushPromises();
    await wrapper.vm.submitForm();
    const [, payload] = patchMock.mock.calls[0];
    expect(payload.location_id).toBe(null);
  });
});

// ─────────────────────────────────────────────────────────────────────
// CustomersView — "N sites" badge
// ─────────────────────────────────────────────────────────────────────

describe("CustomersView — sites badge", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
  });

  it("merges location_count onto each customer row from the API payload", async () => {
    // We don't DOM-test the badge — DataTable + Column body slots in
    // PrimeVue's render machinery are awkward to stub faithfully. The
    // useful regression check is "did the API payload's location_count
    // flow through to wrapper.vm.customers" — the badge itself is a
    // straight v-if over that field in the template.
    getMock.mockImplementation(async (url) => {
      if (url.startsWith("/api/customers")) {
        return {
          items: [
            { id: "c1", name: "Single",  address: "1 St", location_count: 0 },
            { id: "c2", name: "OneSite", address: "1 St", location_count: 1 },
            { id: "c3", name: "Multi",   address: "1 St", location_count: 3 },
          ],
          total: 3, page: 1, per_page: 50,
        };
      }
      return [];
    });
    const wrapper = mount(CustomersView, { global: { stubs: baseStubs } });
    await vtuFlushPromises();
    await vtuFlushPromises();
    expect(wrapper.vm.customers).toBeDefined();
    const customers = wrapper.vm.customers;
    expect(customers.find((c) => c.id === "c3").location_count).toBe(3);
    expect(customers.find((c) => c.id === "c2").location_count).toBe(1);
    expect(customers.find((c) => c.id === "c1").location_count).toBe(0);
  });
});

// ─────────────────────────────────────────────────────────────────────
// DispatchView — displayCustomer suffixes the location label
// ─────────────────────────────────────────────────────────────────────

const dispatchStubs = {
  ...baseStubs,
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  Avatar: { props: ["label"], template: "<span>{{ label }}</span>" },
  Drawer: { props: ["visible"], template: "<div v-if='visible'><slot /></div>" },
  SelectButton: { props: ["modelValue", "options"], template: "<div />" },
  TechTimelineColumn: { template: "<div />" },
  TechEfficiencyPanel: { template: "<div />" },
  MobileJobCloseoutDialog: { template: "<div />" },
};

describe("DispatchView — displayCustomer location suffix", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    patchMock.mockReset();
    patchMock.mockResolvedValue({ ok: true });
  });

  it("appends ' · {location_label}' when the job has location_label", async () => {
    getMock.mockImplementation((url) => {
      const u = String(url || "");
      if (u.includes("/api/technicians")) return Promise.resolve([]);
      if (u.includes("/api/jobs")) return Promise.resolve([]);
      if (u.includes("/api/dispatch-settings")) return Promise.resolve({});
      return Promise.resolve([]);
    });
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await vtuFlushPromises();
    const out = wrapper.vm.displayCustomer({
      customer_name: "Acme Corp",
      location_label: "Warehouse #3",
    });
    expect(out).toBe("Acme Corp · Warehouse #3");
  });

  it("returns just the customer name when no location_label is present", async () => {
    getMock.mockImplementation(() => Promise.resolve([]));
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await vtuFlushPromises();
    const out = wrapper.vm.displayCustomer({ customer_name: "Acme Corp" });
    expect(out).toBe("Acme Corp");
  });

  it("preserves the lead-empty-state when there is no customer_name (location_label ignored)", async () => {
    getMock.mockImplementation(() => Promise.resolve([]));
    const wrapper = mount(DispatchView, { global: { stubs: dispatchStubs } });
    await vtuFlushPromises();
    const out = wrapper.vm.displayCustomer({
      customer_name: null,
      location_label: "Warehouse #3",
    });
    expect(out).toBe("No customer attached (lead)");
  });
});
