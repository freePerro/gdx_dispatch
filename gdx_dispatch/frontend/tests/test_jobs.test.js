import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import JobsView from "../src/views/JobsView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: "/jobs" }),
}));

vi.mock("primevue/usetoast", () => ({
  useToast: () => ({ add: vi.fn() }),
}));

vi.mock("primevue/useconfirm", () => ({
  useConfirm: () => ({ require: vi.fn() }),
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
           :data-testid="'job-row-' + row.id">
        {{ row.title }} {{ row.customer }} {{ row.status }}
        <button :data-testid="'job-edit-' + row.id" @click="$emit('row-click', { data: row })">Edit</button>
      </div>
      <slot />
    </div>`,
  },
  Column: { template: "<div />" },
  Button: {
    props: ["label", "type", "severity", "size"],
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
      '<select :value="modelValue ?? \'\'" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options || []" :key="typeof option === \'object\' ? option.value : option" :value="typeof option === \'object\' ? option.value : option">{{ typeof option === \'object\' ? option.label : option }}</option></select>',
  },
  Textarea: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  ProgressSpinner: { template: "<div />" },
  DatePicker: { props: ["modelValue"], template: "<div />" },
  Select: {
    props: ["modelValue", "options", "optionLabel", "optionValue"],
    emits: ["update:modelValue"],
    template: '<select :value="modelValue ?? \'\'" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options || []" :key="typeof option === \'object\' ? option.value : option" :value="typeof option === \'object\' ? option.value : option">{{ typeof option === \'object\' ? option.label : option }}</option></select>',
  },
  MultiSelect: {
    props: ["modelValue", "options", "optionLabel", "optionValue"],
    emits: ["update:modelValue"],
    template: '<select multiple :value="modelValue || []" @change="$emit(\'update:modelValue\', Array.from($event.target.selectedOptions).map(o => o.value))"><option v-for="option in options || []" :key="typeof option === \'object\' ? option.value : option" :value="typeof option === \'object\' ? option.value : option">{{ typeof option === \'object\' ? option.label : option }}</option></select>',
  },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ToggleSwitch: { props: ["modelValue"], template: "<div />" },
  Toast: { template: "<div />" },
};

const sampleJobs = [
  {
    id: 1,
    job_number: "J-1001",
    customer_name: "Acme Facilities",
    title: "HVAC repair",
    status: "Scheduled",
    scheduled_at: "2026-04-03",
    priority: "High",
  },
  {
    id: 2,
    job_number: "J-1002",
    customer_name: "Northwind",
    title: "Leak detection",
    status: "In Progress",
    scheduled_at: "2026-04-04",
    priority: "Medium",
  },
];
const sampleCustomers = [{ id: 10, name: "Acme Facilities" }];

describe("JobsView", () => {
  beforeEach(() => {
    // JobsView now persists the status tab + search to localStorage
    // (useListPrefs). jsdom shares localStorage across tests in a file, so
    // clear it between tests — otherwise an earlier test's status filter
    // bleeds into the next mount's restored state.
    localStorage.clear();
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockImplementation(async (url) => {
      if (url.startsWith("/api/jobs")) return sampleJobs;
      if (url.startsWith("/api/customers")) return sampleCustomers;
      if (url === "/api/technicians") return [];
      return [];
    });
  });

  it("renders jobs table rows from API", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    expect(getMock).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/jobs/));
    expect(getMock).toHaveBeenCalledWith(expect.stringMatching(/^\/api\/customers/));
    expect(wrapper.find("[data-testid='job-row-1']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-row-2']").exists()).toBe(true);
  });

  it("filters jobs by status tab", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='jobs-status-in-progress']").trigger("click");
    expect(wrapper.find("[data-testid='job-row-2']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-row-1']").exists()).toBe(false);
  });

  it("filters jobs by search query", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='jobs-search']").setValue("acme");
    expect(wrapper.find("[data-testid='job-row-1']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-row-2']").exists()).toBe(false);
  });

  it("opens edit dialog when row is clicked", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    // Trigger via component method since DataTable row-click is stubbed
    wrapper.vm.openEditDialog(sampleJobs[0]);
    await flushPromises();
    expect(wrapper.find("[data-testid='job-title-input']").element.value).toBe("HVAC repair");
  });
});
