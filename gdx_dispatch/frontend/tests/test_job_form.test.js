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
  DataTable: { template: "<div><slot /></div>" },
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
    props: ["modelValue", "options", "optionLabel", "optionValue", "filter", "showClear", "disabled"],
    emits: ["update:modelValue"],
    template: '<select :value="modelValue ?? \'\'" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="option in options || []" :key="typeof option === \'object\' ? option.value : option" :value="typeof option === \'object\' ? option.value : option">{{ typeof option === \'object\' ? option.label : option }}</option></select>',
  },
  MultiSelect: {
    props: ["modelValue", "options", "optionLabel", "optionValue", "filter", "showClear", "display"],
    emits: ["update:modelValue"],
    template: '<select multiple :value="modelValue || []" @change="$emit(\'update:modelValue\', Array.from($event.target.selectedOptions).map(o => o.value))"><option v-for="option in options || []" :key="typeof option === \'object\' ? option.value : option" :value="typeof option === \'object\' ? option.value : option">{{ typeof option === \'object\' ? option.label : option }}</option></select>',
  },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  ToggleSwitch: { props: ["modelValue"], template: "<div />" },
  Toast: { template: "<div />" },
};

describe("Job form", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockImplementation(async (url) => {
      if (url === "/api/jobs") return [];
      if (url.startsWith("/api/customers")) return [{ id: 20, name: "Acme Facilities" }];
      if (url === "/api/technicians") return [];
      return [];
    });
    postMock.mockResolvedValue({ id: 99 });
  });

  it("renders create form fields", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='new-job-btn']").trigger("click");
    expect(wrapper.find("[data-testid='job-title-input']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-customer-dropdown']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-type-dropdown']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-priority-dropdown']").exists()).toBe(true);
    expect(wrapper.find("[data-testid='job-notes-input']").exists()).toBe(true);
  });

  it("validates required title field", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='new-job-btn']").trigger("click");
    await wrapper.find("form").trigger("submit.prevent");
    expect(wrapper.find("[data-testid='job-form-error']").text()).toContain("Title is required");
    expect(postMock).not.toHaveBeenCalled();
  });

  it("submits create form to API", async () => {
    const wrapper = mount(JobsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='new-job-btn']").trigger("click");
    await wrapper.find("[data-testid='job-title-input']").setValue("New install");
    // Set customer_id directly since Select stub doesn't render real options
    wrapper.vm.jobForm.customer_id = 20;
    await wrapper.find("[data-testid='job-notes-input']").setValue("Bring tools");
    await wrapper.find("form").trigger("submit.prevent");
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith(
      "/api/jobs",
      expect.objectContaining({
        title: "New install",
        customer_id: 20,
        notes: "Bring tools",
      }),
    );
  });
});
