/**
 * The sidebar's unread polls follow the module that serves them (GDXA-453).
 *
 * AppSidebar started the email and SMS unread polls unconditionally on mount.
 * Both backend routes are module-gated (`require_module("email")` on the
 * Outlook unread-count, `require_module("phone_com")` on the whole phone.com
 * router), and both stores swallow the refusal, so a tenant with either module
 * off made a failing GET every 60s for the whole session with nothing visible.
 *
 * This drives the REAL useTenantModules with a mocked /api/settings/modules
 * payload — a mocked isEnabled() would pass whatever the gate keyed on (see
 * useTenantModules.emailGate.spec for the time that hid a dead guard).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/dashboard', query: {}, fullPath: '/dashboard' }),
  useRouter: () => ({ push: vi.fn(), resolve: (path) => ({ matched: [{ path }] }) }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useTour', () => ({
  useTour: () => ({ defaultTourFor: () => null, launch: vi.fn() }),
}));

// eslint-disable-next-line import/first
import AppSidebar from '../AppSidebar.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { useTenantModules } from '../../composables/useTenantModules';

const EMAIL_URL = '/api/outlook/messages/unread-count';
const SMS_URL = '/api/phone-com/messages/unread-count';

// An admin JWT: the SMS pin carries `permission: 'nav.office'`, which the
// admin escape hatch grants before any permission set loads.
function adminToken() {
  const b64 = (o) => btoa(JSON.stringify(o)).replace(/=+$/, '');
  return `${b64({ alg: 'none' })}.${b64({ role: 'admin', sub: 'u1' })}.sig`;
}

let modulesState;
function setModules(entries) {
  modulesState = entries;
}

async function loadModules() {
  // Prime the module-level cache the sidebar's own loadTenantModules() reuses.
  // Called outside a component: useTenantModules' onMounted hook is a no-op here.
  await useTenantModules().loadTenantModules({ force: true });
}

function mountSidebar() {
  return mount(AppSidebar, {
    global: {
      plugins: [PrimeVue],
      stubs: { RouterLink: RouterLinkStub, PanelMenu: true },
    },
  });
}

const calls = (url) => apiGet.mock.calls.filter(([u]) => u === url).length;

describe('AppSidebar — unread polls are module-gated', () => {
  let wrapper;

  beforeEach(() => {
    vi.useFakeTimers();
    setActivePinia(createPinia());
    useAuthStore().accessToken = adminToken();
    apiGet.mockReset();
    apiGet.mockImplementation(async (url) => {
      if (url === '/api/settings/modules') return { modules: modulesState };
      if (url === EMAIL_URL || url === SMS_URL) return { count: 2 };
      return [];
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: false });
  });

  afterEach(() => {
    if (wrapper) wrapper.unmount();
    wrapper = null;
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('email and phone_com off: no unread request, not even after a poll interval', async () => {
    setModules([{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }]);
    await loadModules();
    wrapper = mountSidebar();
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(0);
    expect(calls(SMS_URL)).toBe(0);
  });

  it('both on: each count is fetched on mount and again every 60s', async () => {
    setModules([{ key: 'email', enabled: true }, { key: 'phone_com', enabled: true }]);
    await loadModules();
    wrapper = mountSidebar();
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(1);
    expect(calls(SMS_URL)).toBe(1);
    vi.advanceTimersByTime(60000);
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(2);
    expect(calls(SMS_URL)).toBe(2);
  });

  it('email switched on later starts the poll; switched off again releases it', async () => {
    setModules([{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }]);
    await loadModules();
    wrapper = mountSidebar();
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(0);

    // A settings save re-fetches the payload with { force: true }.
    setModules([{ key: 'email', enabled: true }, { key: 'phone_com', enabled: false }]);
    await loadModules();
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(1);

    setModules([{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }]);
    await loadModules();
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(1);
    expect(calls(SMS_URL)).toBe(0);
  });

  it('a module load skipped on /login is not reused after sign-in', async () => {
    // CommandPalette mounts on /login and loads modules signed out; sign-in is
    // a router.push, not a reload. A cached skip read as "every module on".
    setModules([{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }]);
    useAuthStore().accessToken = null;
    await useTenantModules().loadTenantModules({ force: true });
    expect(calls('/api/settings/modules')).toBe(0);

    useAuthStore().accessToken = adminToken();
    wrapper = mountSidebar();
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(calls('/api/settings/modules')).toBe(1);
    expect(calls(EMAIL_URL)).toBe(0);
    expect(calls(SMS_URL)).toBe(0);
  });

  it('signed out (the shell flashes on a cold /login load): no unread request', async () => {
    setModules([]);
    await loadModules();
    useAuthStore().accessToken = null;
    await useTenantModules().loadTenantModules({ force: true });
    wrapper = mountSidebar();
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(0);
    expect(calls(SMS_URL)).toBe(0);
  });

  it('no poll starts while the module payload is still in flight', async () => {
    // Before the payload lands isEnabled() reports the default (on). Start
    // from the empty payload a fresh session has; the cache is module-level.
    setModules([]);
    await loadModules();
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    apiGet.mockImplementation(async (url) => {
      if (url === '/api/settings/modules') return held;
      if (url === EMAIL_URL || url === SMS_URL) return { count: 2 };
      return [];
    });
    const loading = useTenantModules().loadTenantModules({ force: true });
    wrapper = mountSidebar();
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(0);
    expect(calls(SMS_URL)).toBe(0);

    release({ modules: [{ key: 'email', enabled: false }, { key: 'phone_com', enabled: false }] });
    await loading;
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(calls(EMAIL_URL)).toBe(0);
    expect(calls(SMS_URL)).toBe(0);
  });
});
