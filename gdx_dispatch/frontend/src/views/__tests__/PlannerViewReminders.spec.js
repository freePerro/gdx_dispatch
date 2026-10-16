/**
 * PlannerView (desktop) — "Remind me" on the task forms. A reminder set at the
 * desk reaches the GDX Planner phone app; it must go out as an ISO instant.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import PlannerView from '../PlannerView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: apiPostMock, patch: apiPatchMock }),
}));

const DatePickerStub = { name: 'DatePicker', props: ['modelValue'], emits: ['update:modelValue'], template: '<div class="dp" />' };
const wrap = { template: '<div><slot /></div>' };
const stubs = {
  Dialog: { props: ['visible'], emits: ['update:visible', 'hide'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  InputText: { props: ['modelValue'], emits: ['update:modelValue'], template: `<input class="in" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />` },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: wrap, TabList: wrap, Tab: wrap,
  SelectButton: { template: '<div />' },
  Select: { props: ['modelValue'], template: '<select />' },
  DatePicker: DatePickerStub,
  Checkbox: { template: '<input type="checkbox" />' },
  Badge: { template: '<span />' },
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  Button: { props: ['label'], template: `<button class="pbtn" @click="$emit('click', $event)">{{ label }}</button>` },
};

const REMIND = '2026-10-15T14:30:00+00:00';
const TASK = { id: 't1', title: 'Call the supplier', status: 'todo', priority: 'low', remind_at: REMIND, reminder_sent_at: null };
const mountView = () => mount(PlannerView, { global: { stubs } });
// The task list is the My Tasks tab; the view opens on Today.
async function onTasksTab(w) {
  w.vm.activeTab = 'tasks';
  await flushPromises();
}
const picker = (w, test) => w.findAllComponents(DatePickerStub).find((c) => c.attributes('data-testid') === test);
const button = (w, label) => w.findAll('.pbtn').find((b) => b.text() === label);

beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
  localStorage.clear();
  apiGetMock.mockReset().mockImplementation(async (url) => (url.startsWith('/api/planner/tasks') ? { items: [TASK] } : { items: [] }));
  apiPostMock.mockReset().mockResolvedValue({ id: 'task_1' });
  apiPatchMock.mockReset().mockResolvedValue({});
});

describe('PlannerView — Remind me', () => {
  it('shows a pending reminder on the task card', async () => {
    const w = mountView();
    await flushPromises();
    await onTasksTab(w);
    expect(w.find('[data-testid="task-reminder"]').exists()).toBe(true);
  });

  it('creates the task with the picked reminder as an ISO instant', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-testid="new-task"]').trigger('click');
    await w.find('.dlg input.in').setValue('Order springs');
    const when = new Date(2026, 9, 16, 8, 0);
    picker(w, 'task-remind').vm.$emit('update:modelValue', when);
    await button(w, 'Create Task').trigger('click');
    await flushPromises();
    const body = apiPostMock.mock.calls.find(([url]) => url === '/api/planner/tasks')[1];
    expect(body.remind_at).toBe(when.toISOString());
  });

  it('an edit sends the reminder back as the same instant', async () => {
    const w = mountView();
    await flushPromises();
    await onTasksTab(w);
    await w.find('.task-body').trigger('click');
    await flushPromises();
    expect(picker(w, 'task-detail-remind').props('modelValue').getTime()).toBe(Date.parse(REMIND));
    await button(w, 'Save').trigger('click');
    await flushPromises();
    expect(Date.parse(apiPatchMock.mock.calls[0][1].remind_at)).toBe(Date.parse(REMIND));
  });
});
