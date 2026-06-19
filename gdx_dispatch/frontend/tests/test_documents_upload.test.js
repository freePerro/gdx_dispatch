import { describe, it, expect, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import DocumentsView from "../src/views/DocumentsView.vue";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

vi.mock("vue-router", () => ({
  useRouter: () => ({ push: vi.fn() }),
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
  Textarea: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Select: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template: '<select :value="modelValue ?? \'\'" @change="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  ProgressBar: { template: "<div />" },
  ProgressSpinner: { template: "<div />" },
  Tag: { props: ["value"], template: "<span>{{ value }}</span>" },
  Toast: { template: "<div />" },
  Card: { template: '<div><slot name="title" /><slot name="content" /></div>' },
  Tooltip: {},
};

describe("DocumentsView upload form", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    getMock.mockResolvedValue([]);
  });

  it("upload dialog has both polished drop-zone (label) AND a native input fallback", async () => {
    const wrapper = mount(DocumentsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='upload-document-btn']").trigger("click");
    await flushPromises();

    // Polished path: <label for=...><input id=... hidden></label>
    const dropZone = wrapper.find("[data-testid='doc-drop-zone']");
    const primaryInput = wrapper.find("[data-testid='doc-file-input']");
    expect(dropZone.element.tagName).toBe("LABEL");
    expect(dropZone.attributes("for")).toBe(primaryInput.attributes("id"));
    expect(primaryInput.attributes("type")).toBe("file");

    // Always-works fallback: visible native <input> in a <details>.
    const fallback = wrapper.find("[data-testid='doc-upload-fallback']");
    const fallbackInput = wrapper.find("[data-testid='doc-file-input-fallback']");
    expect(fallback.exists()).toBe(true);
    expect(fallback.element.tagName).toBe("DETAILS");
    expect(fallbackInput.attributes("type")).toBe("file");
    expect(fallbackInput.classes()).toContain("native-file-input");
  });

  it("change event on the file input populates the upload form", async () => {
    const wrapper = mount(DocumentsView, { global: { stubs } });
    await flushPromises();
    await wrapper.find("[data-testid='upload-document-btn']").trigger("click");
    await flushPromises();

    const fileInput = wrapper.find("[data-testid='doc-file-input']");
    const file = new File(["hello"], "hello.pdf", { type: "application/pdf" });
    Object.defineProperty(fileInput.element, "files", { value: [file] });
    await fileInput.trigger("change");
    await flushPromises();

    expect(wrapper.vm.uploadForm.file).toBe(file);
    expect(wrapper.vm.uploadForm.name).toBe("hello.pdf");
  });
});
