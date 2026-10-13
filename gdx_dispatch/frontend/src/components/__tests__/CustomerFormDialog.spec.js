/**
 * CustomerFormDialog — unsaved-changes guard (2026-07-01 UX audit).
 *
 * The form/submit contract is pinned by src/views/__tests__/
 * CustomerFormDialog.spec.js + CustomerFormDialogDedup.spec.js; this spec
 * pins only the useDirtyDialog wiring:
 *  1. Clean form → Cancel closes silently (no confirm prompt).
 *  2. Dirty form → Cancel prompts; declining keeps the dialog open.
 *  3. Dirty form → accepting the prompt closes the dialog.
 *  4. The Dialog gets :closable / :close-on-escape = false while dirty.
 * and the footer placement (GDXA-872): Save/Cancel sit outside the scrolling
 * form, with Save still submitting it through form=.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

import CustomerFormDialog from '../CustomerFormDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible', 'closable', 'closeOnEscape'],
    emits: ['update:visible'],
    template:
      '<div v-if="visible" data-testid="dlg" :data-closable="String(closable)" :data-close-on-escape="String(closeOnEscape)"><slot /><slot name="footer" /></div>',
    inheritAttrs: false,
  },
  Button: {
    props: ['label', 'type', 'text', 'loading', 'disabled'],
    emits: ['click'],
    template:
      '<button :type="type || \'button\'" :form="$attrs.form" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template:
      '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  FormField: {
    props: ['modelValue', 'label', 'required', 'type', 'as', 'rows', 'options', 'optionLabel', 'optionValue', 'placeholder'],
    emits: ['update:modelValue'],
    template:
      '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
};

function mountDialog(props = {}) {
  return mount(CustomerFormDialog, {
    props: { visible: true, mode: 'create', customer: null, ...props },
    global: { stubs },
  });
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue([]);
  apiPost.mockReset();
  apiPatch.mockReset();
  toastAdd.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CustomerFormDialog dirty guard', () => {
  it('clean form: Cancel closes silently without prompting', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const wrapper = mountDialog();
    await nextTick();

    await wrapper.get('[data-testid="customer-cancel-btn"]').trigger('click');
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(wrapper.emitted('update:visible').at(-1)).toEqual([false]);
  });

  it('dirty form: Cancel prompts; declining keeps the dialog open', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const wrapper = mountDialog();
    await nextTick();

    await wrapper.get('[data-testid="customer-name-input"]').setValue('Someone New');
    await wrapper.get('[data-testid="customer-cancel-btn"]').trigger('click');

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('update:visible')).toBeFalsy();
  });

  it('dirty form: accepting the prompt closes the dialog', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const wrapper = mountDialog();
    await nextTick();

    await wrapper.get('[data-testid="customer-name-input"]').setValue('Someone New');
    await wrapper.get('[data-testid="customer-cancel-btn"]').trigger('click');

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('update:visible').at(-1)).toEqual([false]);
  });

  it('disables the header X and Esc while dirty', async () => {
    const wrapper = mountDialog();
    await nextTick();

    const dlg = wrapper.get('[data-testid="dlg"]');
    expect(dlg.attributes('data-closable')).toBe('true');
    expect(dlg.attributes('data-close-on-escape')).toBe('true');

    await wrapper.get('[data-testid="customer-name-input"]').setValue('Someone New');
    expect(dlg.attributes('data-closable')).toBe('false');
    expect(dlg.attributes('data-close-on-escape')).toBe('false');
  });
});

describe('CustomerFormDialog footer actions (GDXA-872)', () => {
  it('puts Save/Cancel outside the form, with Save naming the form it submits', async () => {
    const wrapper = mountDialog();
    await nextTick();

    const form = wrapper.get('form');
    const submit = wrapper.get('[data-testid="customer-submit-btn"]');
    expect(form.find('[data-testid="customer-submit-btn"]').exists()).toBe(false);
    expect(form.find('[data-testid="customer-cancel-btn"]').exists()).toBe(false);
    expect(form.attributes('id')).toBeTruthy();
    expect(submit.attributes('type')).toBe('submit');
    expect(submit.attributes('form')).toBe(form.attributes('id'));
  });

  it('gives each dialog in one app its own form id', async () => {
    // useId is unique per app, so both dialogs go under one parent.
    const wrapper = mount(
      {
        components: { CustomerFormDialog },
        template: '<div><CustomerFormDialog visible /><CustomerFormDialog visible /></div>',
      },
      { global: { stubs } },
    );
    await nextTick();
    const ids = wrapper.findAll('form').map((f) => f.attributes('id'));
    expect(ids).toHaveLength(2);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it('scrolls the error into view, since Save no longer sits beside it', async () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const wrapper = mountDialog();
    await nextTick();
    await wrapper.get('form').trigger('submit');
    await nextTick();
    await nextTick();
    expect(wrapper.get('[data-testid="customer-form-error"]').text()).toBe('Name is required.');
    expect(scroll).toHaveBeenCalledTimes(1);

    // The same refusal again (the user scrolled up and tapped Save) scrolls again.
    await wrapper.get('form').trigger('submit');
    await nextTick();
    await nextTick();
    expect(scroll).toHaveBeenCalledTimes(2);
    delete Element.prototype.scrollIntoView;
  });
});
