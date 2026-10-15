/**
 * MobilePlannerView — "Remind me" and ?tab=, the two things the GDX Planner
 * app leans on: a reminder is set from the task forms, and a tapped reminder
 * (/mobile/planner?tab=tasks, mapped onto /planner-app?tab=tasks) opens Tasks.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineComponent, h, reactive, ref } from 'vue';
import DatePicker from 'primevue/datepicker';
import { createPinia, setActivePinia } from 'pinia';

const route = reactive({ query: {} });
vi.mock('vue-router', () => ({ useRoute: () => route }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: apiPostMock, patch: apiPatchMock }),
}));

import MobilePlannerView from '../MobilePlannerView.vue';

const field = (cls, tag) => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<${tag} class="${cls}" :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
});
const DatePickerStub = {
  name: 'DatePicker',
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<div class="dp" />',
};
const stubs = {
  Dialog: {
    props: ['visible'],
    emits: ['update:visible', 'hide'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  InputText: field('in', 'input'),
  Textarea: field('ta', 'textarea'),
  SelectButton: { template: '<div />' },
  Select: { props: ['modelValue'], template: '<select />' },
  DatePicker: DatePickerStub,
  Checkbox: { template: '<input type="checkbox" />' },
  Button: { props: ['label'], template: `<button class="pbtn" @click="$emit('click', $event)">{{ label }}</button>` },
  CustomerFormDialog: { template: '<div />' },
  PlannerTodayPanel: { template: '<div class="today-panel" />', methods: { reload() {} } },
};

// Every view shares the reactive route, so one left mounted would react to the
// next test's query change.
const mounted = [];
const mountView = () => {
  const w = mount(MobilePlannerView, { global: { stubs } });
  mounted.push(w);
  return w;
};
afterEach(() => {
  while (mounted.length) mounted.pop().unmount();
});
const picker = (w, test) => w.findAllComponents(DatePickerStub).find((c) => c.attributes('data-test') === test);
const button = (w, label) => w.findAll('.pbtn').find((b) => b.text() === label);

const REMIND = '2026-10-15T14:30:00+00:00';
const TASK = { id: 't1', title: 'Call the supplier', status: 'todo', priority: 'low', remind_at: REMIND, reminder_sent_at: null };

beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
  localStorage.clear();
  route.query = {};
  apiGetMock.mockReset().mockImplementation(async (url) => (url.startsWith('/api/planner/tasks') ? { items: [TASK] } : { items: [] }));
  apiPostMock.mockReset().mockResolvedValue({ id: 'task_1' });
  apiPatchMock.mockReset().mockResolvedValue({});
});

describe('MobilePlannerView — ?tab=', () => {
  it('opens on Today by default', async () => {
    const w = mountView();
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(true);
  });

  it('?tab=tasks opens on Tasks, where the reminder shows on the card', async () => {
    route.query = { tab: 'tasks' };
    const w = mountView();
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(false);
    expect(w.find('[data-test="mp-task-reminder"]').exists()).toBe(true);
  });

  it('a tap while the page is open (query change only) switches to Tasks', async () => {
    const w = mountView();
    await flushPromises();
    route.query = { tab: 'tasks' };
    await flushPromises();
    expect(w.find('[data-test="mp-task-card"]').exists()).toBe(true);
  });

  it('a tap while the page is open reloads the list, so the reminded task is in it', async () => {
    const w = mountView();
    await flushPromises();
    const before = apiGetMock.mock.calls.filter(([url]) => url.startsWith('/api/planner/tasks')).length;
    route.query = { tab: 'tasks' };
    await flushPromises();
    const after = apiGetMock.mock.calls.filter(([url]) => url.startsWith('/api/planner/tasks')).length;
    expect(after).toBe(before + 1);
  });

  it('coming back to the app on Tasks reloads the list; on Today it does not', async () => {
    const tasksCalls = () => apiGetMock.mock.calls.filter(([url]) => url.startsWith('/api/planner/tasks')).length;
    const w = mountView();
    await flushPromises();
    const onToday = tasksCalls();
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(tasksCalls()).toBe(onToday);
    route.query = { tab: 'tasks' };
    await flushPromises();
    const onTasks = tasksCalls();
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(tasksCalls()).toBe(onTasks + 1);
    mounted.splice(mounted.indexOf(w), 1);
    w.unmount();
    document.dispatchEvent(new Event('visibilitychange'));
    await flushPromises();
    expect(tasksCalls()).toBe(onTasks + 1);
  });

  it('a second tap, with the URL already on ?tab=tasks, still opens Tasks and reloads', async () => {
    const tasksCalls = () => apiGetMock.mock.calls.filter(([url]) => url.startsWith('/api/planner/tasks')).length;
    route.query = { tab: 'tasks' };
    const w = mountView();
    await flushPromises();
    w.findComponent(stubs.SelectButton).vm.$emit('update:modelValue', 'today');
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(true);
    const before = tasksCalls();
    window.dispatchEvent(new CustomEvent('gdx:alert-tap', { detail: { category: 'planner_reminder', dest: '/mobile/planner?tab=tasks' } }));
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(false);
    expect(tasksCalls()).toBe(before + 1);
  });

  it('a tap for another page is ignored', async () => {
    const w = mountView();
    await flushPromises();
    window.dispatchEvent(new CustomEvent('gdx:alert-tap', { detail: { category: 'sms', dest: '/mobile/sms' } }));
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(true);
  });

  it('ignores an unknown tab', async () => {
    route.query = { tab: 'nope' };
    const w = mountView();
    await flushPromises();
    expect(w.find('.today-panel').exists()).toBe(true);
  });
});

describe('MobilePlannerView — Remind me', () => {
  it('creates the task with the picked reminder as an ISO instant', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mp-head-add"]').trigger('click');
    await w.find('.dlg input.in').setValue('Order springs');
    const when = new Date(2026, 9, 16, 8, 0);
    picker(w, 'mp-task-remind').vm.$emit('update:modelValue', when);
    await button(w, 'Create').trigger('click');
    await flushPromises();
    const body = apiPostMock.mock.calls.find(([url]) => url === '/api/planner/tasks')[1];
    expect(body.remind_at).toBe(when.toISOString());
  });

  it('creates without a reminder when none is picked', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mp-head-add"]').trigger('click');
    await w.find('.dlg input.in').setValue('No reminder');
    await button(w, 'Create').trigger('click');
    await flushPromises();
    const body = apiPostMock.mock.calls.find(([url]) => url === '/api/planner/tasks')[1];
    expect(body.remind_at).toBeNull();
  });

  it('an edit saved untouched sends the same instant back, so the server leaves it alone', async () => {
    route.query = { tab: 'tasks' };
    const w = mountView();
    await flushPromises();
    await w.find('.task-body').trigger('click');
    await button(w, 'Save').trigger('click');
    await flushPromises();
    const [url, body] = apiPatchMock.mock.calls[0];
    expect(url).toBe('/api/planner/tasks/t1');
    expect(Date.parse(body.remind_at)).toBe(Date.parse(REMIND));
  });

  it('clearing the picker in the edit dialog clears the reminder', async () => {
    route.query = { tab: 'tasks' };
    const w = mountView();
    await flushPromises();
    await w.find('.task-body').trigger('click');
    picker(w, 'mp-task-edit-remind').vm.$emit('update:modelValue', null);
    await button(w, 'Save').trigger('click');
    await flushPromises();
    expect(apiPatchMock.mock.calls[0][1].remind_at).toBeNull();
  });
});

// The reminder pickers are typeable. Their format carries the year, so a typed
// or edited reminder keeps it: with `M d,` the year was dropped from the text
// and read back as the current year (Jan 5 next year became Jan 5 this year).
describe('the reminder picker format keeps the year', () => {
  const VIEWS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const formats = ['PlannerView.vue', 'MobilePlannerView.vue'].flatMap((f) =>
    [...fs.readFileSync(path.join(VIEWS, f), 'utf8').matchAll(/v-model="[\w.]+\.remind_at"[^>]*?dateFormat="([^"]+)"/g)].map((m) => [f, m[1]]));

  it('finds all four reminder pickers', () => {
    expect(formats).toHaveLength(4);
  });

  it.each(formats)('%s: a 2027 reminder typed into the real picker stays in 2027 (%s)', async (_file, dateFormat) => {
    const model = ref(null);
    const Host = defineComponent({
      setup: () => () => h(DatePicker, {
        modelValue: model.value,
        'onUpdate:modelValue': (v) => { model.value = v; },
        showTime: true, hourFormat: '12', dateFormat,
      }),
    });
    const w = mount(Host, { attachTo: document.body });
    const picker = w.findComponent(DatePicker);
    const shown = picker.vm.formatValue(new Date(2027, 0, 5, 9, 0));
    const input = w.find('input').element;
    input.value = shown;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await flushPromises();
    expect(model.value?.getFullYear()).toBe(2027);
    expect(model.value?.getMonth()).toBe(0);
    expect(model.value?.getDate()).toBe(5);
    w.unmount();
  });
});
