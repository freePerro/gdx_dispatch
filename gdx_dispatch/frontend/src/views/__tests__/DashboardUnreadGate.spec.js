/**
 * The dashboard's one-shot unread refresh follows the module (GDXA-453).
 *
 * loadDashboard() fired both unread counts on every dashboard load. Both
 * backend routes are module-gated and both stores swallow the refusal, so a
 * tenant with email or phone.com off made a failing GET per dashboard visit.
 * The module payload goes through the REAL useTenantModules (only the HTTP
 * client is mocked), so a gate keyed on the wrong module name fails here.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { mockGet, moduleGet, emailFetch, smsFetch, modulesState } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  moduleGet: vi.fn(),
  emailFetch: vi.fn(),
  smsFetch: vi.fn(),
  modulesState: { value: [] },
}));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: moduleGet }),
  createApiClient: () => ({ get: moduleGet }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-10-10' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    isAdmin: true,
    role: 'admin',
    isAuthenticated: true,
    hasPermission: () => true,
    loadPermissions: async () => {},
  }),
}));
vi.mock('../../stores/emailUnread', () => ({
  useEmailUnreadStore: () => ({ count: 0, fetchCount: emailFetch }),
}));
vi.mock('../../stores/smsUnread', () => ({
  useSmsUnreadStore: () => ({ count: 0, fetchCount: smsFetch }),
}));

// eslint-disable-next-line import/first
import DashboardView from '../DashboardView.vue';
// eslint-disable-next-line import/first
import { useTenantModules } from '../../composables/useTenantModules';

const stubs = {
  Card: { template: '<div><slot /><slot name="title" /><slot name="content" /></div>' },
  Button: true,
  Skeleton: true,
  Tag: true,
  Dialog: true,
  InputText: true,
  Textarea: true,
  Select: true,
  PhoneInput: true,
  CashCalendarSummaryCard: true,
};

async function mountWith(entries) {
  modulesState.value = entries;
  // Prime the module-level cache the dashboard's loadTenantModules() reuses.
  await useTenantModules().loadTenantModules({ force: true });
  const wrapper = mount(DashboardView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

describe('DashboardView — unread refresh is module-gated', () => {
  beforeEach(() => {
    mockGet.mockReset().mockResolvedValue(null);
    moduleGet.mockReset().mockImplementation(async (url) =>
      url === '/api/settings/modules' ? { modules: modulesState.value } : []);
    emailFetch.mockReset().mockResolvedValue(undefined);
    smsFetch.mockReset().mockResolvedValue(undefined);
  });

  it('email and phone_com off: neither count is fetched', async () => {
    const w = await mountWith([{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }]);
    expect(emailFetch).not.toHaveBeenCalled();
    expect(smsFetch).not.toHaveBeenCalled();
    w.unmount();
  });

  it('both on: each count is fetched once', async () => {
    const w = await mountWith([{ key: 'email', enabled: true }, { key: 'phone_com', enabled: true }]);
    expect(emailFetch).toHaveBeenCalledTimes(1);
    expect(smsFetch).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it('only email on: only the email count is fetched', async () => {
    const w = await mountWith([{ key: 'email', enabled: true }, { key: 'phone_com', enabled: false }]);
    expect(emailFetch).toHaveBeenCalledTimes(1);
    expect(smsFetch).not.toHaveBeenCalled();
    w.unmount();
  });

  it('waits for the module payload: none fetched while it is in flight', async () => {
    // Before the payload lands isEnabled() reports the default (on). Start
    // from the empty payload a fresh session has; the cache is module-level.
    modulesState.value = [];
    await useTenantModules().loadTenantModules({ force: true });
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    moduleGet.mockImplementation(async (url) => (url === '/api/settings/modules' ? held : []));
    const loading = useTenantModules().loadTenantModules({ force: true });
    const w = mount(DashboardView, { global: { stubs } });
    await flushPromises();
    expect(emailFetch).not.toHaveBeenCalled();
    expect(smsFetch).not.toHaveBeenCalled();

    release({ modules: [{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }] });
    await loading;
    await flushPromises();
    expect(emailFetch).not.toHaveBeenCalled();
    expect(smsFetch).not.toHaveBeenCalled();
    w.unmount();
  });
});
