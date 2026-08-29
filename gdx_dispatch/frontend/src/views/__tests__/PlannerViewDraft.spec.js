/**
 * PlannerView (desktop) — the New Task draft survives the back button.
 *
 * On desktop the X and Escape already happened to keep the text (the form ref
 * was never reset on open), so the ONLY desktop loss vector was a route change:
 * PrimeVue overlays push no history entry and there is no keep-alive on
 * /planner, so back unmounts the whole view and takes `taskForm` with it.
 *
 * An unmount + fresh mount is exactly that navigation, which is why these
 * tests destroy the wrapper rather than merely closing the dialog.
 *
 * COUNTERFACTUAL: delete `taskDraft.applyTo(...)` from `openTaskForm()` and
 * both tests go red — the draft is written but never read back.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import PlannerView from '../PlannerView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: apiPostMock, patch: apiPatchMock }),
}));

const DialogStub = {
  name: 'Dialog',
  props: ['visible'],
  emits: ['update:visible', 'hide'],
  template: `<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>`,
};
const InputTextStub = {
  name: 'InputText',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<input class="in" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
};
const TextareaStub = {
  name: 'Textarea',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<textarea class="ta" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
};

const stubs = {
  Dialog: DialogStub,
  InputText: InputTextStub,
  Textarea: TextareaStub,
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  SelectButton: { template: '<div />' },
  Select: {
    name: 'Select',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<select />',
  },
  DatePicker: {
    name: 'DatePicker',
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<div class="dp" />',
  },
  Checkbox: { template: '<input type="checkbox" />' },
  Badge: { template: '<span />' },
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  Button: {
    props: ['label'],
    template: `<button class="pbtn" @click="$emit('click', $event)">{{ label }}</button>`,
  },
};

const mountView = () => mount(PlannerView, { global: { stubs } });

async function openNewTask(w) {
  await w.find('[data-testid="new-task"]').trigger('click');
  await flushPromises();
}

async function dismiss(w) {
  const dlg = w.findAllComponents(DialogStub)[0];
  dlg.vm.$emit('update:visible', false);
  dlg.vm.$emit('hide');
  await flushPromises();
}

const titleInput = (w) => w.find('.dlg input.in');

beforeEach(() => {
  sessionStorage.clear();
  apiGetMock.mockReset().mockResolvedValue({ items: [] });
  apiPostMock.mockReset().mockResolvedValue({ id: 'task_1' });
  apiPatchMock.mockReset().mockResolvedValue({});
});

describe('PlannerView — New Task draft survives navigation', () => {
  it('restores typed text after the view is unmounted and remounted', async () => {
    const first = mountView();
    await flushPromises();
    await openNewTask(first);
    await titleInput(first).setValue('Call the supplier about the 16x7');
    await dismiss(first);
    first.unmount(); // ← the back button

    const second = mountView();
    await flushPromises();
    await openNewTask(second);
    expect(titleInput(second).element.value).toBe('Call the supplier about the 16x7');
    expect(second.find('[data-testid="task-draft-note"]').exists()).toBe(true);
  });

  it('does not resurrect a draft the user explicitly discarded', async () => {
    const first = mountView();
    await flushPromises();
    await openNewTask(first);
    await titleInput(first).setValue('scratch that');
    await dismiss(first);
    await openNewTask(first);
    const discard = first.findAll('.dlg button.pbtn').find((b) => b.text() === 'Discard');
    await discard.trigger('click');
    await dismiss(first);
    first.unmount();

    const second = mountView();
    await flushPromises();
    await openNewTask(second);
    expect(titleInput(second).element.value).toBe('');
  });

  it('keeps a due date as a calendar day across the round-trip, not an instant', async () => {
    const w = mountView();
    await flushPromises();
    await openNewTask(w);
    // Drive the real v-model the way the DatePicker does: a Date object.
    await titleInput(w).setValue('dated note');
    const dlg = w.findAllComponents(DialogStub)[0];
    dlg.findComponent({ name: 'DatePicker' }).vm.$emit('update:modelValue', new Date(2026, 7, 29));
    await flushPromises();
    await dismiss(w);

    const stored = JSON.parse(sessionStorage.getItem('gdx_draft_planner_task_new'));
    expect(stored.due_date).toBe('2026-08-29');
    expect(stored.due_date).not.toMatch(/[TZ]/);
  });

  it('does NOT blank a linked job or customer when the dialog is reopened', async () => {
    // Regression guard for a bug this fix itself introduced and then removed:
    // desktop never reset the form on open, so an earlier cut that added a
    // reset restored the text while silently dropping the links — and the
    // "Draft restored" strip told the user everything had come back.
    const w = mountView();
    await flushPromises();
    await openNewTask(w);
    await titleInput(w).setValue('linked note');
    const dlg = () => w.findAllComponents(DialogStub)[0];
    // Dialog Select order: priority, assigned_to, job_id, customer_id.
    const selects = () => dlg().findAllComponents({ name: 'Select' });
    expect(selects().length).toBe(4);
    selects()[2].vm.$emit('update:modelValue', 'job_22222222');
    selects()[3].vm.$emit('update:modelValue', 'cus_11111111');
    await flushPromises();
    await dismiss(w);

    await openNewTask(w);
    expect(selects()[2].props('modelValue')).toBe('job_22222222');
    expect(selects()[3].props('modelValue')).toBe('cus_11111111');
  });
});
