/**
 * UsersView — Send Reset Link / Send invite tell the truth (GDXA-677).
 *
 * Pins: (1) a refused Send Reset Link shows the server's reason, not the
 * generic "try again" 5xx toast; (2) an invite whose email did not go out
 * warns the admin instead of only saying "User created".
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import UsersView from '../UsersView.vue';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() }),
}));

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ user: { user_id: 'admin-1', sub: 'admin-1', role: 'admin' } }),
}));

const vModelInput = (tag, type = 'text') => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<${tag} type="${type}" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  ProgressSpinner: { template: '<div />' },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input type="checkbox" v-bind="$attrs" :checked="modelValue"
        @change="$emit('update:modelValue', $event.target.checked)" />`,
  },
  Select: { template: '<select />' },
  InputText: vModelInput('input'),
  Textarea: vModelInput('textarea'),
  Password: vModelInput('input', 'password'),
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot name="header" /><slot /><slot name="footer" /></div>`,
  },
  Button: {
    inheritAttrs: false,
    props: ['label', 'disabled'],
    emits: ['click'],
    template: `<button v-bind="$attrs" :disabled="disabled" @click="$emit('click')">
        <slot>{{ label }}</slot>
      </button>`,
  },
  Toast: { template: '<div />' },
};

const tech = { id: 't1', name: 'Tina Tech', email: 'tina@example.com', role: 'tech', active: true, schedulable: false };

async function mountWithUsers(rows) {
  apiGet.mockResolvedValue(rows);
  const wrapper = mount(UsersView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('UsersView reset link and invite', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    apiPost.mockReset();
    toastAdd.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the server reason when Send Reset Link is refused', async () => {
    const wrapper = await mountWithUsers([tech]);
    apiPost.mockRejectedValueOnce(Object.assign(
      new Error('Email is not configured on this server, so no reset link was sent.'),
      { status: 503 },
    ));

    await wrapper.find('[data-testid="users-send-reset-t1"]').trigger('click');
    await flushPromises();

    const [url, , opts] = apiPost.mock.calls[0];
    expect(url).toBe('/api/users/t1/send-reset-link');
    expect(opts.suppressErrorToast).toBe(true);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'error',
      summary: 'Reset link not sent',
      detail: expect.stringContaining('not configured'),
    }));
  });

  it('warns when an invite was created but its email did not go out', async () => {
    const wrapper = await mountWithUsers([tech]);
    await wrapper.findAll('button').find((b) => b.text().includes('Add User')).trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="users-create-name"]').setValue('New Hire');
    await wrapper.find('[data-testid="users-create-email"]').setValue('new.hire@example.com');
    await wrapper.find('[data-testid="users-create-invite-toggle"]').setValue(true);
    apiPost.mockResolvedValueOnce({
      id: 'n1', invite_email_sent: false, invite_error: 'Email is not configured on this server.',
    });

    await wrapper.find('[data-testid="users-create-dialog"] form').trigger('submit');
    await flushPromises();

    expect(apiPost.mock.calls[0][0]).toBe('/api/users/invite');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'warn',
      summary: 'Invite email not sent',
      detail: expect.stringContaining('Reset Password'),
    }));
  });

  it('does not warn when the invite email went out', async () => {
    const wrapper = await mountWithUsers([tech]);
    await wrapper.findAll('button').find((b) => b.text().includes('Add User')).trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="users-create-name"]').setValue('Sent Hire');
    await wrapper.find('[data-testid="users-create-email"]').setValue('sent.hire@example.com');
    await wrapper.find('[data-testid="users-create-invite-toggle"]').setValue(true);
    apiPost.mockResolvedValueOnce({ id: 'n2', invite_email_sent: true, invite_error: null });

    await wrapper.find('[data-testid="users-create-dialog"] form').trigger('submit');
    await flushPromises();

    expect(apiPost.mock.calls[0][0]).toBe('/api/users/invite');
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Invite email not sent' }));
  });
});
