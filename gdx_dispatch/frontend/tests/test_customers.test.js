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

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: {
    props: ["value"],
    template: `<div>
      <div v-for="row in (value || [])" :key="row.id"
           :data-testid="'customer-row-' + row.id">
        {{ row.name }} {{ row.phone }} {{ row.email }}
        <button :data-testid="'customer-edit-' + row.id" @click="$emit('row-click', { data: row })">Edit</button>
      </div>
      <slot />
    </div>`,
  },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "type"],
    emits: ["click"],
    template:
      '<button :type="type || \'button\'" @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Dialog: {
    props: ["visible"],
    template: "<div v-if='visible'><slot /><slot name='footer' /></div>",
  },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Dropdown: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template:
      '<select :value="modelValue ?? \'\'" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options || []" :key="option" :value="option">{{ option }}</option></select>',
  },
  Textarea: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  ProgressSpinner: { template: "<div />" },
  Select: {
    props: ["modelValue", "options", "optionLabel", "optionValue"],
    emits: ["update:modelValue"],
    template: '<select></select>',
  },
  InputMask: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ToggleSwitch: { props: ["modelValue"], template: "<div />" },
  Toast: { template: "<div />" },
};

const sampleCustomers = [
  {
    id: 1,
    name: "Acme Facilities",
    phone: "555-111-2222",
    email: "ops@acme.com",
    address: "123 Main St",
    lastJobDate: "2026-04-01",
  },
  {
    id: 2,
    name: "Northwind Health",
    phone: "555-444-7777",
    email: "admin@northwind.com",
    address: "801 Lake Ave",
    lastJobDate: "2026-03-28",
  },
];

describe("CustomersView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockResolvedValue(sampleCustomers);
  });

  it("renders customer table rows", async () => {
    const wrapper = mount(CustomersView, { global: { stubs } });
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith(expect.stringContaining("/api/customers"));
    expect(wrapper.find("[data-testid='customer-row-1']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='customer-row-2']").exists()).toBe(true);
  });

  it("filters customers by search and opens edit form from row", async () => {
    const wrapper = mount(CustomersView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='customers-search']").setValue("northwind");
    expect(wrapper.find("[data-testid='customer-row-2']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='customer-row-1']").exists()).toBe(false);
    // Directly trigger the edit dialog via the component's openEditDialog method
    wrapper.vm.openEditDialog(sampleCustomers[1]);
    await flushPromises();
    expect(wrapper.find("[data-testid='customer-name-input']").element.value).toBe(
      "Northwind Health",
    );
  });
});
