import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import EstimatesView from "../src/views/EstimatesView.vue";

const pushMock = vi.fn();
const getMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: pushMock }),
}));

vi.mock("primevue/useconfirm", () => ({
  useConfirm: () => ({ require: vi.fn() }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("../src/composables/useApi", () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));

const sampleEstimates = [
  { id: 1, estimate_number: "EST-001", customer_name: "Acme", status: "Draft", total_amount: 500, created_at: "2026-04-01" },
  { id: 2, estimate_number: "EST-002", customer_name: "Globex", status: "Sent", total_amount: 750, created_at: "2026-04-02" },
  { id: 3, estimate_number: "EST-003", customer_name: "Northwind", status: "Accepted", total_amount: 1200, created_at: "2026-04-03" },
];

const stubs = {
  AppLayout: { template: "<div><slot /></div>" },
  DataTable: {
    props: ["value", "loading"],
    emits: ["row-click"],
    template: `<div>
      <div v-for="row in (value || [])" :key="row.id"
           :data-testid="'estimate-row-' + row.id"
           @click="$emit('row-click', { data: row })">
        {{ row.estimate_number }} {{ row.customer_name }} {{ row.status }}
      </div>
      <slot />
    </div>`,
  },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "severity", "size"],
    emits: ["click"],
    template: '<button @click="$emit(\'click\')">{{ label }}</button>',
  },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ConfirmDialog: { template: "<div />" },
  Toast: { template: "<div />" },
};

describe("EstimatesView", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    pushMock.mockReset();
    getMock.mockReset();
    getMock.mockImplementation(async (url) => {
      if (url === "/api/estimates") return sampleEstimates;
      if (url.startsWith("/api/customers")) return [];
      return [];
    });
  });

  it("renders estimate table", async () => {
    const wrapper = mount(EstimatesView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find("[data-testid='estimate-row-1']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='estimate-row-2']").exists()).toBe(true);
  });

  it("filters by status tabs and routes on click", async () => {
    const wrapper = mount(EstimatesView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='estimates-status-accepted']").trigger("click");
    await flushPromises();
    expect(wrapper.find("[data-testid='estimate-row-3']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='estimate-row-1']").exists()).toBe(false);
    await wrapper.find("[data-testid='estimate-row-3']").trigger("click");
    expect(pushMock).toHaveBeenCalledWith("/estimates/3");
  });
});
