import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import InvoiceDetailView from "../src/views/InvoiceDetailView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();

vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { id: "12" } }),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("primevue/useconfirm", () => ({
  useConfirm: () => ({ require: vi.fn() }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: postMock, patch: patchMock }),
}));

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DataTable: {
    props: ["value", "dataKey"],
    template: `<div>
      <div v-for="row in (value || [])" :key="row.id"
           :data-testid="'payment-row-' + row.id">
        {{ row.method }} {{ row.amount }}
      </div>
      <slot />
    </div>`,
  },
  Column: { template: "<div><slot /></div>" },
  Button: {
    props: ["label", "disabled", "loading"],
    emits: ["click"],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Dialog: {
    props: ["visible"],
    emits: ["update:visible"],
    template: "<div v-if='visible'><slot /><slot name='footer' /></div>",
  },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  InputNumber: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<input type="number" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
  },
  Select: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="option in (options || [])" :key="option" :value="option">{{ option }}</option>
    </select>`,
  },
  Dropdown: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="option in options" :key="option" :value="option">{{ option }}</option>
    </select>`,
  },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Divider: { template: "<hr />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Toast: { template: "<div />" },
  ConfirmDialog: { template: "<div />" },
};

describe("InvoiceDetailView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    getMock.mockResolvedValue({
      id: 12,
      invoice_number: "INV-2012",
      customer: "Acme",
      status: "Sent",
      amount: 1000,
      total: 1000,
      due_date: "2026-04-20",
      line_items: [{ id: 1, description: "Labor", quantity: 2, unit_price: 300 }],
      payments: [{ id: 1, amount: 100, method: "Cash", date: "2026-04-01" }],
    });
    postMock.mockResolvedValue({ id: 2, amount: 150, method: "ACH", reference: "REF-001" });
  });

  it("renders payment history", async () => {
    const wrapper = mount(InvoiceDetailView, { global: { stubs } });
    await flushPromises();

    expect(getMock).toHaveBeenCalledWith("/api/invoices/12");
    expect(wrapper.find("[data-testid='payment-row-1']").exists()).toBe(true);
    expect(wrapper.text()).toContain("100");
  });

  it("records a payment", async () => {
    const wrapper = mount(InvoiceDetailView, { global: { stubs } });
    await flushPromises();

    await wrapper.find("[data-testid='record-payment-btn']").trigger("click");
    await flushPromises();

    await wrapper.find("[data-testid='payment-amount']").setValue("150");
    // Use the Select stub for method
    await wrapper.find("[data-testid='payment-method']").trigger("change");
    await wrapper.find("[data-testid='save-payment']").trigger("click");
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith(
      "/api/invoices/12/payments",
      expect.objectContaining({
        amount: 150,
      }),
    );
  });
});
