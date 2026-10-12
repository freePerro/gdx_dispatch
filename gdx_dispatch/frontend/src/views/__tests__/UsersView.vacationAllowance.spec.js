/**
 * UsersView — per-person vacation allowance override (GDXA-700).
 *
 * Pins: the Edit Profile dialog opens with the saved override (blank when
 * the person inherits the shop default), and Save sends it in HOURS — null
 * when blank, so clearing it hands the person back to the shop default.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import UsersView from '../UsersView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGet = vi.fn();
const apiPatch = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn(), patch: apiPatch, del: vi.fn() }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ get user() { return { user_id: 'admin-1', sub: 'admin-1', role: 'admin' }; } }),
}));

const passInput = (tag = 'input') => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<${tag} :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  ProgressSpinner: { template: '<div />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  Select: { props: ['modelValue', 'options'], template: '<select />' },
  InputText: passInput(),
  Textarea: passInput('textarea'),
  // PrimeVue InputNumber emits a number, or null when emptied.
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input :value="modelValue ?? ''"
        @input="$emit('update:modelValue', $event.target.value === '' ? null : Number($event.target.value))" />`,
  },
  Password: { template: '<input type="password" />' },
  Dialog: { props: ['visible'], template: '<div v-if="visible" class="dlg"><slot name="header" /><slot /><slot name="footer" /></div>' },
  Button: {
    inheritAttrs: false,
    props: ['label', 'type', 'disabled'],
    emits: ['click'],
    template: '<button v-bind="$attrs" :type="type || \'button\'" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Toast: { template: '<div />' },
};

function tech(overrides = {}) {
  return { id: 't1', name: 'Tina Tech', email: 'tina@example.com', role: 'technician', active: true, ...overrides };
}

async function openEdit(row) {
  apiGet.mockResolvedValue([row]);
  const w = mount(UsersView, { global: { stubs } });
  await flushPromises();
  await w.find('[data-testid="users-edit-t1"]').trigger('click');
  return w;
}

async function save(w) {
  await w.find('form.dialog-form').trigger('submit');
  await flushPromises();
  return apiPatch.mock.calls.at(-1)[1];
}

describe('UsersView vacation allowance override', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    apiPatch.mockReset();
    apiPatch.mockResolvedValue({});
  });

  it('opens blank for someone who inherits, and saves null when left blank', async () => {
    const w = await openEdit(tech({ vacation_allowance_hours: null }));
    const input = w.find('[data-testid="users-edit-vacation-allowance"]');
    expect(input.element.value).toBe('');
    expect((await save(w)).vacation_allowance_hours).toBe(null);
  });

  it('opens with the saved override and sends a new one in hours', async () => {
    const w = await openEdit(tech({ vacation_allowance_hours: 120 }));
    const input = w.find('[data-testid="users-edit-vacation-allowance"]');
    expect(input.element.value).toBe('120');
    await input.setValue('96.5');
    const body = await save(w);
    expect(apiPatch.mock.calls.at(-1)[0]).toBe('/api/users/t1');
    expect(body.vacation_allowance_hours).toBe(96.5);
  });

  it('clearing the override sends null, back to the shop default', async () => {
    const w = await openEdit(tech({ vacation_allowance_hours: 120 }));
    await w.find('[data-testid="users-edit-vacation-allowance"]').setValue('');
    expect((await save(w)).vacation_allowance_hours).toBe(null);
  });
});
