/**
 * Role pickers offer the backend's role vocabulary (GDXA-1200).
 *
 * The Settings invite dialog and UsersView each offered a role their own
 * endpoint 422ed, and neither offered Sales. Both now mirror
 * core/roles.ASSIGNABLE_ROLES, sent in its stored lowercase form.
 *
 * MOUNT tests: the options are read from the rendered Select's props and the
 * invite payload from the api mock, not from the source text.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn(), delete: vi.fn(), put: vi.fn(),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive: vi.fn(async () => true) }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ loadTenantModules: vi.fn() }),
}));
vi.mock('../../composables/useIdleLogout', () => ({
  getIdleTimeoutMin: () => 30,
  setIdleTimeoutMin: vi.fn(),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    user: { user_id: 'admin-1', sub: 'admin-1', role: 'admin' },
    role: 'admin',
    permissions: ['*'],
    permissionsLoaded: true,
    hasPermission: () => true,
  }),
}));

import SettingsView from '../SettingsView.vue';
import UsersView from '../UsersView.vue';

const SelectStub = {
  name: 'SelectStub',
  props: ['options', 'modelValue', 'optionLabel', 'optionValue'],
  template: '<select />',
};
const passthrough = { template: '<div><slot /></div>' };

// core/roles.ASSIGNABLE_ROLES, owner first.
const ASSIGNABLE = ['owner', 'admin', 'dispatcher', 'technician', 'sales', 'viewer'];

function selectByTestId(wrapper, testid) {
  const hit = wrapper.findAllComponents(SelectStub).find((s) => s.attributes('data-testid') === testid);
  expect(hit, `no Select with data-testid=${testid}`).toBeTruthy();
  return hit;
}

beforeEach(() => {
  setActivePinia(createPinia());
  apiGet.mockReset();
  apiPost.mockReset();
});

describe('Settings invite dialog', () => {
  const stubs = {
    Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
    Tab: passthrough, TabPanel: passthrough,
    Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
    Dialog: { template: '<div><slot /></div>' },
    DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: true,
    Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
    Select: SelectStub, ToggleSwitch: true, InputNumber: true,
    AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
    PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
    SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
  };

  async function mountSettings() {
    apiGet.mockImplementation((url) => Promise.reject(new Error(`unmocked GET ${url}`)));
    const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
    await flushPromises();
    return wrapper;
  }

  it('offers Sales, as lowercase backend values, and never Owner', async () => {
    const select = selectByTestId(await mountSettings(), 'invite-role-dropdown');
    const values = select.props('options').map((o) => o.value);
    expect(values).toContain('sales');
    // Owner is owner-only to grant; this tab serves admins too.
    expect(values).toEqual(ASSIGNABLE.filter((r) => r !== 'owner'));
    expect(select.props('optionValue')).toBe('value');
    expect(select.props('options').find((o) => o.value === 'sales').label).toBe('Sales');
  });

  it('posts the picked role as the backend spells it', async () => {
    const wrapper = await mountSettings();
    apiPost.mockResolvedValue({ invite_token: null, message: 'ok' });
    wrapper.vm.inviteForm = { email: 'new.sales@example.com', role: 'sales' };
    await wrapper.vm.submitInvite();
    expect(apiPost).toHaveBeenCalledWith('/api/admin/users/invite', {
      email: 'new.sales@example.com', role: 'sales',
    });
  });

  it('colours role tags from the stored lowercase role', async () => {
    const { roleSeverity } = (await mountSettings()).vm;
    expect(roleSeverity('sales')).toBe('warn');
    expect(roleSeverity('admin')).toBe('danger');
    expect(roleSeverity('tech')).toBe('success');
    expect(roleSeverity('dispatch')).toBe('info');
    expect(roleSeverity('viewer')).toBe('secondary');
  });
});

describe('UsersView role pickers', () => {
  const stubs = {
    Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
    ProgressSpinner: { template: '<div />' },
    ToggleSwitch: true, InputText: true, Textarea: true, Password: true,
    Select: SelectStub,
    Dialog: { props: ['visible'], template: '<div><slot /><slot name="footer" /></div>' },
    Button: true, Toast: true,
  };
  const rows = [
    { id: 's1', name: 'Sam Sales', email: 'sam@example.com', role: 'sales', active: true },
    { id: 't1', name: 'Tina Tech', email: 'tina@example.com', role: 'tech', active: true },
  ];

  async function mountUsers() {
    apiGet.mockResolvedValue(rows);
    const wrapper = mount(UsersView, { global: { stubs } });
    await flushPromises();
    return wrapper;
  }

  it('the per-user and create pickers offer every assignable role, Sales included', async () => {
    const wrapper = await mountUsers();
    for (const testid of ['users-role-select-s1', 'users-create-role']) {
      const values = selectByTestId(wrapper, testid).props('options').map((o) => o.value);
      expect(values, testid).toEqual(ASSIGNABLE);
    }
    // A sales user's picker shows Sales selected, not a blank.
    expect(selectByTestId(wrapper, 'users-role-select-s1').props('modelValue')).toBe('sales');
  });

  it('counts sales users in their own stat card', async () => {
    const wrapper = await mountUsers();
    const card = wrapper.find('[data-testid="users-stat-sales"]');
    expect(card.exists()).toBe(true);
    expect(card.find('.stat-count').text()).toBe('1');
  });
});
