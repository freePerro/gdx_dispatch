/**
 * CustomersView ↔ CustomerFormDialog integration.
 *
 * The actual form behavior (fields, validation, POST/PATCH) is unit-tested in
 * src/views/__tests__/CustomerFormDialog.spec.js — that's the canonical test
 * for the dialog now that it's extracted (2026-05-21). This file pins the
 * CustomersView side: clicking "+ New Customer" opens the dialog in create
 * mode with no customer prop, and clicking row-Edit opens it in edit mode
 * pre-loaded with the row's customer.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import CustomersView from "../src/views/CustomersView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/customers" }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApiWithToast", () => ({
  useApiWithToast: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    del: delMock,
  }),
}));

const sampleCustomers = [
  { id: "cust-a", name: "Acme", email: "a@x", phone: "555-0001", address: "1 Pine", customer_type: "Residential" },
  { id: "cust-b", name: "Beta",  email: "b@x", phone: "555-0002", address: "2 Oak",  customer_type: "Commercial" },
];

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  RouterLink: { props: ["to"], template: "<a><slot /></a>" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: { template: "<div />" },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "type"],
    emits: ["click"],
    template:
      '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}<slot /></button>',
    inheritAttrs: false,
  },
  Dialog: {
    props: ["visible"],
    template: "<div v-if='visible'><slot /><slot name='footer' /></div>",
  },
  InputText: {
    props: ["modelValue"],
    template: '<input :value="modelValue" />',
  },
  ProgressSpinner: { template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Toast: { template: "<div />" },
  // The extracted dialog is the unit under test in its own spec — here we
  // stub it so the integration test only proves CustomersView passes the
  // right props/handlers, not the dialog's internals.
  CustomerFormDialog: {
    props: ["visible", "mode", "customer"],
    emits: ["update:visible", "saved"],
    template:
      '<div v-if="visible" data-testid="customer-form-dialog-stub">mode={{ mode }};customer={{ customer ? customer.id : "" }}</div>',
  },
};

describe("CustomersView ↔ CustomerFormDialog", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockResolvedValue(sampleCustomers);
  });

  it("opens the dialog in create mode with no customer when + New Customer is clicked", async () => {
    const wrapper = mount(CustomersView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find("[data-testid='customer-form-dialog-stub']").exists()).toBe(false);

    await wrapper.find("[data-testid='new-customer-btn']").trigger("click");
    await flushPromises();

    const dialog = wrapper.find("[data-testid='customer-form-dialog-stub']");
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("mode=create");
    expect(dialog.text()).toMatch(/customer=\s*$/);
  });

  it("opens the dialog in edit mode with the row's customer", async () => {
    const wrapper = mount(CustomersView, { global: { stubs } });
    await flushPromises();

    // DataTable is stubbed away in this layer (table internals are exercised
    // by browser walks, not unit tests). Drive openEditDialog directly with
    // the row payload — same callsite as the row-Edit button.
    wrapper.vm.openEditDialog(sampleCustomers[0]);
    await flushPromises();

    const dialog = wrapper.find("[data-testid='customer-form-dialog-stub']");
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("mode=edit");
    expect(dialog.text()).toContain("customer=cust-a");
  });

  it("re-fetches customers when the dialog emits `saved`", async () => {
    const wrapper = mount(CustomersView, { global: { stubs } });
    await flushPromises();
    expect(getMock).toHaveBeenCalledTimes(1);

    await wrapper.find("[data-testid='new-customer-btn']").trigger("click");
    await flushPromises();

    // Invoke the parent's saved-handler directly. The integration the
    // CustomersView side owns is "on saved, refetch" — the dialog itself
    // emitting saved is covered in CustomerFormDialog.spec.js.
    await wrapper.vm.onCustomerSaved({ id: "cust-new", name: "New" });
    await flushPromises();

    expect(getMock).toHaveBeenCalledTimes(2);
  });
});
