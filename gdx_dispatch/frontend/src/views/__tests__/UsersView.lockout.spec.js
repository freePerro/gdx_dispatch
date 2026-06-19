/**
 * UsersView — Lock Out / Unlock UX.
 *
 * Pins: (1) Lock Out button is hidden for owners, locked users, and self;
 * (2) the submit button only enables once a reason is picked AND "LOCK" is
 * typed; (3) the locked badge fetches reason on click; (4) Unlock dialog
 * exists for inactive users.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import UsersView from '../UsersView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: apiPost,
    patch: apiPatch,
    del: apiDel,
  }),
}));

// Current user = admin-1 (so admin-1 is "self" for the self-guard test).
// The `currentUser` ref is mutated by tests that need a different role
// to exercise the actor-role gate.
const currentUser = { user_id: 'admin-1', sub: 'admin-1', role: 'admin' };
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    get user() { return currentUser; },
  }),
}));

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  ProgressSpinner: { template: '<div />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue"
        @change="$emit('update:modelValue', $event.target.value)">
        <option v-for="o in (options || [])" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>`,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input :value="modelValue"
        @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<textarea :value="modelValue"
        @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Password: { template: '<input type="password" />' },
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot name="header" /><slot /><slot name="footer" /></div>`,
  },
  Button: {
    inheritAttrs: false,
    props: ['label', 'icon', 'severity', 'disabled'],
    emits: ['click'],
    template: `<button v-bind="$attrs" :disabled="disabled" @click="$emit('click')">
        <slot>{{ label }}</slot>
      </button>`,
  },
  Toast: { template: '<div />' },
};

function user(overrides = {}) {
  return {
    id: 't1',
    name: 'Tina Tech',
    email: 'tina@example.com',
    role: 'tech',
    active: true,
    schedulable: false,
    ...overrides,
  };
}

async function mountWithUsers(rows) {
  apiGet.mockResolvedValueOnce(rows);
  const wrapper = mount(UsersView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('UsersView lockout', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
    apiPost.mockReset();
    // Reset to admin for every test; individual tests mutate as needed
    currentUser.user_id = 'admin-1';
    currentUser.sub = 'admin-1';
    currentUser.role = 'admin';
    sessionStorage.setItem('gdx_tenant_slug', 'gdx');
  });

  afterEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it('renders a Lock Out button on an active non-owner non-self user', async () => {
    const wrapper = await mountWithUsers([user({ id: 't1' })]);
    expect(wrapper.find('[data-testid="users-lockout-t1"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="users-unlock-t1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="users-locked-badge-t1"]').exists()).toBe(false);
  });

  it('hides the Lock Out button on an owner', async () => {
    const wrapper = await mountWithUsers([user({ id: 'o1', role: 'owner' })]);
    expect(wrapper.find('[data-testid="users-lockout-o1"]').exists()).toBe(false);
  });

  it('hides the Lock Out button on yourself', async () => {
    // Mock returns admin-1 as current user; the row in the list has id=admin-1
    const wrapper = await mountWithUsers([
      user({ id: 'admin-1', role: 'admin', name: 'Me' }),
    ]);
    expect(wrapper.find('[data-testid="users-lockout-admin-1"]').exists()).toBe(false);
  });

  it('hides the Lock Out button on already-locked users and shows Unlock + Locked badge', async () => {
    const wrapper = await mountWithUsers([user({ id: 'x1', active: false })]);
    expect(wrapper.find('[data-testid="users-lockout-x1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="users-unlock-x1"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="users-locked-badge-x1"]').exists()).toBe(true);
  });

  it('opens the lockout dialog and disables submit until reason + LOCK typed', async () => {
    const wrapper = await mountWithUsers([user({ id: 't1' })]);
    await wrapper.find('[data-testid="users-lockout-t1"]').trigger('click');
    await flushPromises();

    const submit = wrapper.find('[data-testid="users-lockout-submit"]');
    expect(submit.exists()).toBe(true);
    expect(submit.attributes('disabled')).toBeDefined();

    // Pick a reason — still disabled (no LOCK typed)
    const reason = wrapper.find('[data-testid="users-lockout-reason"]');
    await reason.setValue('terminated');
    await flushPromises();
    expect(wrapper.find('[data-testid="users-lockout-submit"]').attributes('disabled')).toBeDefined();

    // Type LOCK — now enabled
    const confirm = wrapper.find('[data-testid="users-lockout-confirm-input"]');
    await confirm.setValue('LOCK');
    await flushPromises();
    expect(wrapper.find('[data-testid="users-lockout-submit"]').attributes('disabled')).toBeUndefined();
  });

  it('does NOT enable submit when LOCK is typed but no reason picked', async () => {
    const wrapper = await mountWithUsers([user({ id: 't1' })]);
    await wrapper.find('[data-testid="users-lockout-t1"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="users-lockout-confirm-input"]').setValue('LOCK');
    await flushPromises();
    expect(wrapper.find('[data-testid="users-lockout-submit"]').attributes('disabled')).toBeDefined();
  });

  it('POSTs to /lockout with reason + notes on submit', async () => {
    apiPost.mockResolvedValueOnce({ id: 't1', active: false });
    const wrapper = await mountWithUsers([user({ id: 't1' })]);
    await wrapper.find('[data-testid="users-lockout-t1"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="users-lockout-reason"]').setValue('security_incident');
    await wrapper.find('[data-testid="users-lockout-notes"]').setValue('laptop missing');
    await wrapper.find('[data-testid="users-lockout-confirm-input"]').setValue('LOCK');
    await flushPromises();

    await wrapper.find('[data-testid="users-lockout-submit"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith(
      '/api/users/t1/lockout',
      { reason: 'security_incident', notes: 'laptop missing' },
      expect.objectContaining({ successMessage: expect.any(String) }),
    );
  });

  it('clicking the Locked badge fetches lockout-info and renders reason', async () => {
    const wrapper = await mountWithUsers([user({ id: 'x1', active: false })]);
    // Queue AFTER the mount — fetchUsers consumed the initial rows mock.
    apiGet.mockResolvedValueOnce({
      reason: 'policy_violation',
      notes: 'shared password',
      locked_by: 'admin-2',
      locked_at: '2026-05-20T14:00:00Z',
    });
    await wrapper.find('[data-testid="users-locked-badge-x1"]').trigger('click');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/users/x1/lockout-info');
    const dlg = wrapper.find('[data-testid="users-lockout-info-dialog"]');
    expect(dlg.exists()).toBe(true);
    expect(wrapper.find('[data-testid="users-lockout-info-reason"]').text()).toContain('Policy violation');
    expect(wrapper.find('[data-testid="users-lockout-info-notes"]').text()).toContain('shared password');
  });

  it('Unlock dialog POSTs to /unlock and clears active=false', async () => {
    apiPost.mockResolvedValueOnce({ id: 'x1', active: true });
    const wrapper = await mountWithUsers([user({ id: 'x1', active: false })]);
    await wrapper.find('[data-testid="users-unlock-x1"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="users-unlock-dialog"]').exists()).toBe(true);
    await wrapper.find('[data-testid="users-unlock-submit"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/users/x1/unlock',
      {},
      expect.objectContaining({ successMessage: expect.any(String) }),
    );
  });

  it('hides Lock Out + Unlock buttons when current user is a dispatcher', async () => {
    // Dispatcher with users.write (custom tenant role) — the backend
    // still 403s, but the UI gate cuts off the surface entirely.
    currentUser.role = 'dispatch';
    currentUser.user_id = 'disp-1';
    currentUser.sub = 'disp-1';
    const wrapper = await mountWithUsers([
      user({ id: 't1' }),
      user({ id: 'x1', active: false }),
    ]);
    expect(wrapper.find('[data-testid="users-lockout-t1"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="users-unlock-x1"]').exists()).toBe(false);
    // Locked badge stays visible — non-actors can still see *who* is locked,
    // they just can't undo it.
    expect(wrapper.find('[data-testid="users-locked-badge-x1"]').exists()).toBe(true);
  });

  it('the deprecated /toggle-active endpoint is no longer called', async () => {
    const wrapper = await mountWithUsers([user({ id: 't1' })]);
    // Sanity: no remaining users-active-toggle in the DOM
    expect(wrapper.find('[data-testid="users-active-toggle-t1"]').exists()).toBe(false);
    expect(apiPost).not.toHaveBeenCalledWith(
      expect.stringContaining('/toggle-active'),
      expect.anything(),
      expect.anything(),
    );
  });
});
