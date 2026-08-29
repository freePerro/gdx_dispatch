/**
 * MobilePlannerView — the New Task draft survives an accidental dismissal.
 *
 * Doug, 2026-08-29: "in the planner if I have something typed into a new one
 * and don't click save it does not automatically save it. I might press the
 * back button or X by accident."
 *
 * Mobile was the destructive surface: `openCreate()` ran
 * `taskForm.value = emptyTaskForm()` on every open, so the X discarded for
 * good what the desktop view happened to keep in memory.
 *
 * TWO counterfactuals, because the fix has two halves:
 *   - put `taskForm.value = emptyTaskForm()` back into `openCreate()` and the
 *     reopen test goes red (that reset WAS the mobile bug);
 *   - delete `taskDraft.applyTo(...)` and the unmount/remount test goes red
 *     (that is the back-button half).
 * Without both, the spec would only assert that the code I wrote is the code
 * I wrote.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MobilePlannerView from '../MobilePlannerView.vue';

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
  Button: {
    props: ['label'],
    template: `<button class="pbtn" @click="$emit('click', $event)">{{ label }}</button>`,
  },
  CustomerFormDialog: { template: '<div />' },
};

function mountView() {
  return mount(MobilePlannerView, { global: { stubs } });
}

/** The task-create dialog is the first Dialog in the template. */
function taskDialog(w) {
  return w.findAllComponents(DialogStub)[0];
}

/** Open via the real "+ New" control, exactly as a thumb would. */
async function tapNew(w) {
  await w.find('[data-test="mp-head-add"]').trigger('click');
  await flushPromises();
}

/** The X / Escape / Cancel path: PrimeVue emits update:visible then hide. */
async function dismiss(w) {
  const dlg = taskDialog(w);
  dlg.vm.$emit('update:visible', false);
  dlg.vm.$emit('hide');
  await flushPromises();
}

function titleInput(w) {
  return w.find('.dlg input.in');
}

beforeEach(() => {
  sessionStorage.clear();
  apiGetMock.mockReset().mockResolvedValue({ items: [] });
  apiPostMock.mockReset().mockResolvedValue({ id: 'task_1' });
  apiPatchMock.mockReset().mockResolvedValue({});
});

describe('MobilePlannerView — New Task draft', () => {
  it('restores typed text after an accidental dismissal and reopen', async () => {
    const w = mountView();
    await flushPromises();

    await tapNew(w);
    await titleInput(w).setValue('Wants 2 openers quoted, call back Thu');
    await dismiss(w);

    await tapNew(w);
    expect(titleInput(w).element.value).toBe('Wants 2 openers quoted, call back Thu');
  });

  it('shows the Draft restored hint only when something was actually restored', async () => {
    const w = mountView();
    await flushPromises();

    await tapNew(w);
    expect(w.find('[data-test="mp-task-draft-note"]').exists()).toBe(false);

    await titleInput(w).setValue('half a thought');
    await dismiss(w);
    await tapNew(w);
    expect(w.find('[data-test="mp-task-draft-note"]').exists()).toBe(true);
  });

  it('Discard clears the draft so the next open is blank', async () => {
    const w = mountView();
    await flushPromises();

    await tapNew(w);
    await titleInput(w).setValue('changed my mind');
    await dismiss(w);
    await tapNew(w);

    const discard = w.findAll('.dlg button.pbtn').find((b) => b.text() === 'Discard');
    expect(discard).toBeTruthy();
    await discard.trigger('click');
    await dismiss(w);

    await tapNew(w);
    expect(titleInput(w).element.value).toBe('');
    expect(w.find('[data-test="mp-task-draft-note"]').exists()).toBe(false);
  });

  it('a created task does not come back as a draft on the next open', async () => {
    const w = mountView();
    await flushPromises();

    await tapNew(w);
    await titleInput(w).setValue('Replace torsion spring');
    const create = w.findAll('.dlg button.pbtn').find((b) => b.text() === 'Create');
    await create.trigger('click');
    await flushPromises();
    await dismiss(w);

    expect(apiPostMock).toHaveBeenCalledWith(
      '/api/planner/tasks',
      expect.objectContaining({ title: 'Replace torsion spring' }),
      expect.anything(),
    );
    await tapNew(w);
    expect(titleInput(w).element.value).toBe('');
  });

  it('never persists a linked customer, job or assignee', async () => {
    const w = mountView();
    await flushPromises();

    await tapNew(w);
    await titleInput(w).setValue('note with links');
    // Pick an assignee, a job and a customer through the real v-model bindings.
    // Dialog Select order: priority, assigned_to, job_id, customer_id.
    const selects = taskDialog(w).findAllComponents({ name: 'Select' });
    expect(selects.length).toBe(4); // guards against a vacuous pass if the form changes
    selects[1].vm.$emit('update:modelValue', 'usr_33333333');
    selects[2].vm.$emit('update:modelValue', 'job_22222222');
    selects[3].vm.$emit('update:modelValue', 'cus_11111111');
    await flushPromises();
    await dismiss(w);

    const stored = sessionStorage.getItem('gdx_draft_planner_task_new');
    expect(stored).toBeTruthy();
    expect(stored).not.toContain('cus_11111111');
    expect(stored).not.toContain('job_22222222');
    expect(stored).not.toContain('usr_33333333');
  });

  it('survives an unmount and remount — the back button', async () => {
    const first = mountView();
    await flushPromises();
    await tapNew(first);
    await titleInput(first).setValue('spring broke on the 9x7, call Tuesday');
    first.unmount(); // route change: no dismiss, straight to unmount

    const second = mountView();
    await flushPromises();
    await tapNew(second);
    expect(titleInput(second).element.value).toBe('spring broke on the 9x7, call Tuesday');
  });

  it('does NOT blank a linked job or customer when the dialog is reopened', async () => {
    // Regression guard. An earlier cut of this fix reset the form on open and
    // then restored only the text, so reopening silently dropped the links
    // while the "Draft restored" strip claimed everything came back.
    const w = mountView();
    await flushPromises();
    await tapNew(w);
    await titleInput(w).setValue('linked note');
    const selects = () => taskDialog(w).findAllComponents({ name: 'Select' });
    selects()[2].vm.$emit('update:modelValue', 'job_22222222');
    selects()[3].vm.$emit('update:modelValue', 'cus_11111111');
    await flushPromises();
    await dismiss(w);

    await tapNew(w);
    expect(selects()[2].props('modelValue')).toBe('job_22222222');
    expect(selects()[3].props('modelValue')).toBe('cus_11111111');
  });
});
