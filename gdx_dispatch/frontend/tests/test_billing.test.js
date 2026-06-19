import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import BillingView from "../src/views/BillingView.vue";

const pushMock = vi.fn();
const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: pushMock }),
  useRoute: () => ({ query: {} }),
}));

vi.mock("primevue/useconfirm", () => ({
  useConfirm: () => ({ require: vi.fn() }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock, del: delMock }),
}));

const sampleInvoices = [
  { id: 1, invoice_number: "INV-2001", customer_name: "Acme", total: 1200, balance_due: 1200, status: "Sent", due_date: "2026-04-20" },
  { id: 2, invoice_number: "INV-2002", customer_name: "Globex", total: 980, balance_due: 980, status: "Overdue", due_date: "2026-03-15" },
  { id: 3, invoice_number: "INV-2003", customer_name: "Northwind", total: 1400, balance_due: 0, status: "Paid", due_date: "2026-04-01", paid_at: new Date().toISOString() },
];

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DataTable: {
    props: ["value", "selection"],
    emits: ["row-click", "update:selection"],
    template: `<div>
      <div v-for="row in (value || [])" :key="row.id"
           :data-testid="'invoice-row-' + row.id"
           @click="$emit('row-click', { data: row })">
        {{ row.invoice_number }} {{ row.customer_name }}
      </div>
      <slot />
    </div>`,
  },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "disabled", "severity", "size"],
    emits: ["click"],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Badge: { props: ["value"], template: "<span>{{ value }}</span>" },
  ConfirmDialog: { template: "<div />" },
  DatePicker: { props: ["modelValue"], template: "<div />" },
  Dialog: {
    props: ["visible"],
    template: "<div v-if='visible'><slot /><slot name='footer' /></div>",
  },
  Dropdown: {
    props: ["modelValue", "options", "optionLabel", "optionValue"],
    emits: ["update:modelValue"],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', Number($event.target.value))">
      <option v-for="option in options" :key="option.value" :value="option.value">{{ option.label }}</option>
    </select>`,
  },
  InputNumber: { props: ["modelValue"], template: "<div />" },
  InputText: { props: ["modelValue"], emits: ["update:modelValue"], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
  Select: { props: ["modelValue", "options"], template: "<select></select>" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Textarea: { props: ["modelValue"], template: "<textarea />" },
  Toast: { template: "<div />" },
};

describe("BillingView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    pushMock.mockReset();
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockImplementation(async (url) => {
      if (url === "/api/invoices") return sampleInvoices;
      if (url.startsWith("/api/customers")) return [];
      if (url === "/api/jobs") return [];
      return [];
    });
    postMock.mockResolvedValue({ id: 99, invoice_number: "INV-2099", customer: "A", amount: 100, status: "Draft", due_date: "2026-04-20" });
  });

  it("renders invoice table rows", async () => {
    const wrapper = mount(BillingView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.text()).toContain("INV-2001");
    expect(wrapper.text()).toContain("INV-2002");
  });

  it("shows summary cards values", async () => {
    const wrapper = mount(BillingView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find("[data-testid='billing-total-outstanding']").text()).toContain("$2,180");
    expect(wrapper.find("[data-testid='billing-overdue-amount']").text()).toContain("$980");
    expect(wrapper.find("[data-testid='billing-paid-this-month']").text()).toContain("$1,400");
  });

  it("routes to invoice detail when row is clicked", async () => {
    const wrapper = mount(BillingView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='invoice-row-1']").trigger("click");
    expect(pushMock).toHaveBeenCalledWith("/billing/1");
  });
});
