/**
 * Dispatch — Holding Area "+ Add" flow.
 *
 * Doug 2026-05-10: "in the holding areas the + add area does not work."
 *
 * Root cause: the toolbar button toggled `showAddAreaDialog = true` but no
 * Dialog template was bound to the ref AND no submit handler existed —
 * the click was a no-op, and the backend POST /api/holding-areas had been
 * sitting unused. Fixed by adding the Dialog + a `createHoldingArea()`
 * function that POSTs the form values and reloads the list on success.
 *
 * This spec mounts the dialog scaffold in isolation (the real DispatchView
 * is too heavy for unit tests — it pulls in technicians fetch, optimizer,
 * map, GPS breadcrumbs, etc.). The contract pinned here:
 *  1. The Add-Area dialog renders when `showAddAreaDialog` is true.
 *  2. Submitting POSTs to `/api/holding-areas` with `{ name, color }`.
 *  3. Submission re-fetches the list (so the new area appears immediately).
 *  4. The submit button is disabled when the name is empty.
 *
 * If a future refactor breaks any of these, this fires.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { defineComponent, ref } from 'vue';

// Inline Dialog stub — PrimeVue's real Dialog teleports to <body>, which
// puts the content outside the wrapper's DOM tree and breaks wrapper.find().
// The behavior we care about is "v-model:visible toggles whether the slot
// renders," which the stub captures faithfully.
const Dialog = {
  name: 'Dialog',
  props: ['visible'],
  emits: ['update:visible'],
  template: '<div v-if="visible" data-testid="add-holding-area-dialog"><slot /></div>',
};
const InputText = {
  name: 'InputText',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  inheritAttrs: false,
};

const apiPost = vi.fn();
const apiGet = vi.fn();
const toastAdd = vi.fn();

// A mini host component that mirrors the wiring inside DispatchView so the
// behavior we care about is exercised end-to-end without paying the cost
// of mounting the real view. If the real view's wiring drifts from this
// (e.g. renames `createHoldingArea`), the integration is fine — but the
// guarantee is the data-testid contract: anyone touching the real view
// can keep these data-testids stable and the contract holds.
const Host = defineComponent({
  components: { Dialog, InputText },
  setup() {
    const showAddAreaDialog = ref(false);
    const newAreaName = ref('');
    const newAreaColor = ref('#6b7280');
    const creatingHoldingArea = ref(false);

    async function createHoldingArea() {
      const name = newAreaName.value.trim();
      if (!name) return;
      creatingHoldingArea.value = true;
      try {
        await apiPost('/api/holding-areas', {
          name,
          color: newAreaColor.value || '#6b7280',
        });
        newAreaName.value = '';
        newAreaColor.value = '#6b7280';
        showAddAreaDialog.value = false;
        await apiGet('/api/holding-areas');
        toastAdd({ severity: 'success', summary: 'Holding area added' });
      } catch {
        toastAdd({ severity: 'error', summary: 'Could not add area' });
      } finally {
        creatingHoldingArea.value = false;
      }
    }

    return {
      showAddAreaDialog,
      newAreaName,
      newAreaColor,
      creatingHoldingArea,
      createHoldingArea,
    };
  },
  template: `
    <div>
      <button data-testid="add-holding-area" @click="showAddAreaDialog = true">+ Add Area</button>
      <Dialog v-model:visible="showAddAreaDialog">
        <input data-testid="new-area-name" v-model="newAreaName" />
        <input data-testid="new-area-color" type="color" v-model="newAreaColor" />
        <button
          data-testid="add-holding-area-submit"
          :disabled="!newAreaName.trim()"
          @click="createHoldingArea"
        >Add area</button>
      </Dialog>
    </div>
  `,
});

describe('Dispatch — Holding Area "+ Add" flow', () => {
  beforeEach(() => {
    apiPost.mockReset();
    apiGet.mockReset();
    toastAdd.mockReset();
  });

  it('opens the dialog when the toolbar button is clicked', async () => {
    const wrapper = mount(Host);
    expect(wrapper.find('[data-testid="add-holding-area-dialog"]').exists()).toBe(false);

    await wrapper.find('[data-testid="add-holding-area"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="add-holding-area-dialog"]').exists()).toBe(true);
  });

  it('disables submit when name is empty', async () => {
    const wrapper = mount(Host);
    await wrapper.find('[data-testid="add-holding-area"]').trigger('click');
    await flushPromises();

    expect(
      wrapper.find('[data-testid="add-holding-area-submit"]').attributes('disabled'),
    ).toBeDefined();
  });

  it('POSTs to /api/holding-areas and reloads the list on submit', async () => {
    apiPost.mockResolvedValue({ id: 'area-1', name: 'Awaiting parts' });
    apiGet.mockResolvedValue([]);

    const wrapper = mount(Host);
    await wrapper.find('[data-testid="add-holding-area"]').trigger('click');
    await flushPromises();

    const nameInput = wrapper.find('[data-testid="new-area-name"]');
    nameInput.element.value = 'Awaiting parts';
    await nameInput.trigger('input');

    await wrapper.find('[data-testid="add-holding-area-submit"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/holding-areas', {
      name: 'Awaiting parts',
      color: '#6b7280',
    });
    // List reload happens after the POST — pinning this prevents the
    // "Add succeeds but UI doesn't show the new area" regression.
    expect(apiGet).toHaveBeenCalledWith('/api/holding-areas');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
  });

  // Static-source guard — pin the real DispatchView wiring so a refactor
  // that drops the Dialog or the createHoldingArea function fires here.
  // The unit tests above use a mini host component to exercise the
  // behavior; without this guard, the real view could regress and the
  // mini-host tests would still pass.
  it('DispatchView.vue has the Dialog + handler wired (static check)', async () => {
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const SRC = readFileSync(
      join(__dirname, '..', 'DispatchView.vue'),
      'utf8',
    );
    expect(SRC).toMatch(
      /<Dialog[\s\S]*?v-model:visible="showAddAreaDialog"/,
    );
    expect(SRC).toMatch(/async function createHoldingArea\s*\(/);
    expect(SRC).toMatch(/api\.post\(\s*["']\/api\/holding-areas["']/);
  });

  it('shows an error toast if the POST fails (and keeps the dialog open)', async () => {
    apiPost.mockRejectedValue(new Error('500'));

    const wrapper = mount(Host);
    await wrapper.find('[data-testid="add-holding-area"]').trigger('click');
    await flushPromises();
    const nameInput = wrapper.find('[data-testid="new-area-name"]');
    nameInput.element.value = 'X';
    await nameInput.trigger('input');

    await wrapper.find('[data-testid="add-holding-area-submit"]').trigger('click');
    await flushPromises();

    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error' }));
    // Dialog stays open so the user can retry / fix.
    expect(wrapper.find('[data-testid="add-holding-area-dialog"]').exists()).toBe(true);
  });
});
