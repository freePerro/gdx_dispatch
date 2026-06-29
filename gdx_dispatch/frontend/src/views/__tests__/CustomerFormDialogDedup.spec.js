/**
 * CustomerFormDialog — at-entry duplicate warning (feat/daily-ux-improvements).
 *
 * Pins:
 *  1. Typing an identifier that matches an existing customer surfaces a
 *     non-blocking warning naming the match.
 *  2. The warning never blocks submit (it may be a different person).
 *  3. No false warning when nothing matches.
 *  4. Edit mode excludes the record itself (no self-match).
 *  5. A failed lookup fails open (no warning, no crash).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

vi.mock("../../composables/useApi", () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock("../../composables/useApiWithToast", () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock("primevue/usetoast", () => ({ useToast: () => ({ add: toastAdd }) }));

import CustomerFormDialog from "../../components/CustomerFormDialog.vue";

const stubs = {
  Dialog: {
    props: ["visible", "header"],
    emits: ["update:visible"],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /></div>',
    inheritAttrs: false,
  },
  Button: {
    props: ["label", "type", "loading", "disabled"],
    emits: ["click"],
    template:
      '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ["modelValue"],
    emits: ["update:modelValue"],
    template:
      '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Select: {
    props: ["modelValue", "options"],
    emits: ["update:modelValue"],
    template: '<select :data-testid="$attrs[\'data-testid\']"><option /></select>',
    inheritAttrs: false,
  },
};

function mountDialog(props = {}) {
  return mount(CustomerFormDialog, {
    props: { visible: true, mode: "create", customer: null, ...props },
    global: { stubs },
  });
}

// Advance past the 400ms debounce and let the async lookup settle.
async function settleLookup() {
  vi.advanceTimersByTime(400);
  await flushPromises();
  await nextTick();
}

beforeEach(() => {
  vi.useFakeTimers();
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  toastAdd.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe("CustomerFormDialog duplicate warning", () => {
  it("warns when a typed phone matches an existing customer", async () => {
    apiGet.mockResolvedValue([
      { id: 99, name: "Existing Person", phone: "(555) 123-4567", email: "x@y.com" },
    ]);
    const wrapper = mountDialog();
    await wrapper.get('[data-testid="customer-name-input"]').setValue("New Person");
    await wrapper.get('[data-testid="customer-phone-input"]').setValue("555-123-4567");
    await settleLookup();

    const warn = wrapper.find('[data-testid="customer-dup-warning"]');
    expect(warn.exists()).toBe(true);
    expect(warn.text()).toContain("Existing Person");
    expect(apiGet).toHaveBeenCalled();
  });

  it("does not block submit when a duplicate is flagged", async () => {
    apiGet.mockResolvedValue([{ id: 99, name: "Dup", phone: "5551234567" }]);
    apiPost.mockResolvedValue({ id: "new" });
    const wrapper = mountDialog();
    await wrapper.get('[data-testid="customer-name-input"]').setValue("Dup");
    await wrapper.get('[data-testid="customer-phone-input"]').setValue("5551234567");
    await settleLookup();
    expect(wrapper.find('[data-testid="customer-dup-warning"]').exists()).toBe(true);

    await wrapper.get("form").trigger("submit.prevent");
    await flushPromises();
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it("shows no warning when nothing matches", async () => {
    apiGet.mockResolvedValue([{ id: 1, name: "Someone Else", phone: "5559990000" }]);
    const wrapper = mountDialog();
    await wrapper.get('[data-testid="customer-name-input"]').setValue("Brand New");
    await wrapper.get('[data-testid="customer-phone-input"]').setValue("5550001111");
    await settleLookup();
    expect(wrapper.find('[data-testid="customer-dup-warning"]').exists()).toBe(false);
  });

  it("edit mode does not flag the record against itself", async () => {
    apiGet.mockResolvedValue([
      { id: "cust-1", name: "Acme", phone: "5551234567" },
    ]);
    const wrapper = mountDialog({
      mode: "edit",
      customer: { id: "cust-1", name: "Acme", phone: "5551234567" },
    });
    await nextTick();
    // Trigger a re-check by editing a field to the same identity.
    await wrapper.get('[data-testid="customer-phone-input"]').setValue("555-123-4567");
    await settleLookup();
    expect(wrapper.find('[data-testid="customer-dup-warning"]').exists()).toBe(false);
  });

  it("fails open (no warning, no throw) when the lookup errors", async () => {
    apiGet.mockRejectedValue(new Error("network"));
    const wrapper = mountDialog();
    await wrapper.get('[data-testid="customer-name-input"]').setValue("Whoever");
    await wrapper.get('[data-testid="customer-phone-input"]').setValue("5551234567");
    await settleLookup();
    expect(wrapper.find('[data-testid="customer-dup-warning"]').exists()).toBe(false);
  });
});
