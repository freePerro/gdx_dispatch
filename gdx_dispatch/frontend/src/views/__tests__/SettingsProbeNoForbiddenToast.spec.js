/**
 * Settings — an expected 403 on an integration status probe shows no toast
 * (GDXA-1143).
 *
 * /settings has no route permission, so a viewer mounts it;
 * /api/settings/integrations is admin/owner only, and /api/qb/status 403s
 * whenever the quickbooks module is off. Each used to raise a "Permission
 * denied" toast on every visit. GET /api/settings (admin-only too) is answered
 * 200 here: its seven loaders keep their toast until their cards can say
 * "admin only" inline, so they are not part of this class.
 *
 * The real useApi runs here; only fetch and the toast sink are faked. Mocking
 * useApi and asserting the option was passed would prove authorship, not that
 * no toast appears. The control tests prove this harness CAN see the toast:
 * an unsuppressed 403 still raises one, and a 5xx, 401 or 429 on a probe keeps
 * useApi's own handling (for a 401, since 2026-10-15, that is the trip to
 * /login?reason=expired rather than a toast).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter } from 'vue-router';

const toastAdd = vi.fn();

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
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

import SettingsView from '../SettingsView.vue';

const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: true,
  Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
  Select: true, ToggleSwitch: true, InputNumber: true,
  AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
  PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
  SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
};

function reply(status, body) {
  const text = body === undefined ? '' : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Map([['content-type', 'application/json']]),
    json: async () => JSON.parse(text || 'null'),
    text: async () => text,
  };
}

const ADMIN_ONLY = { detail: 'Admin access required' };
const QB_OFF = { detail: "Module 'quickbooks' is not enabled" };

// What a viewer's browser receives today, by path. Anything else answers 200.
let overrides;
function fetchStub(url) {
  const path = String(url).split('?')[0];
  if (path in overrides) return Promise.resolve(reply(...overrides[path]));
  if (path === '/api/users') return Promise.resolve(reply(200, []));
  return Promise.resolve(reply(200, {}));
}

async function mountSettings(router) {
  const plugins = router ? [createPinia(), router] : [createPinia()];
  const wrapper = mount(SettingsView, { global: { stubs, plugins } });
  await flushPromises();
  return wrapper;
}

const summaries = () => toastAdd.mock.calls.map((c) => c[0]?.summary);

beforeEach(() => {
  toastAdd.mockReset();
  overrides = {
    '/api/settings/integrations': [403, ADMIN_ONLY],
    '/api/qb/status': [403, QB_OFF],
  };
  vi.stubGlobal('fetch', vi.fn(fetchStub));
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Settings — expected 403 on a status probe', () => {
  it('a 403 on the integration status probes raises no toast', async () => {
    await mountSettings();
    const probed = fetch.mock.calls.map((c) => String(c[0]));
    // The probes really ran and really got their 403s; otherwise "no toast"
    // would be vacuous.
    expect(probed).toContain('/api/settings/integrations');
    expect(probed).toContain('/api/qb/status');
    expect(summaries()).not.toContain('Permission denied');
    expect(toastAdd).not.toHaveBeenCalled();
  });

  it('control: a 403 on a call outside the probe class still toasts', async () => {
    overrides['/api/settings/branding'] = [403, { detail: 'Forbidden' }];
    await mountSettings();
    expect(summaries()).toContain('Permission denied');
  });

  it('a 5xx on a probe is not swallowed with the 403', async () => {
    overrides['/api/settings/integrations'] = [500, { detail: 'boom' }];
    await mountSettings();
    expect(summaries()).toContain('Something went wrong');
    expect(summaries()).not.toContain('Permission denied');
  });

  // Only the 403 is quieted: the probes alone (Retry, the QuickBooks OAuth
  // callback) must still run useApi's session-expired and rate-limit handling.
  it('a 401 that survives a token refresh still goes to the session-expired login', async () => {
    overrides['/api/settings/integrations'] = [401, { detail: 'Unauthorized' }];
    overrides['/api/qb/status'] = [401, { detail: 'Unauthorized' }];
    overrides['/auth/refresh'] = [200, { access_token: 'fresh' }];
    const blank = { template: '<div />' };
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/settings', component: blank }, { path: '/login', component: blank }],
    });
    await router.push('/settings');
    await mountSettings(router);
    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query).toMatchObject({ redirect: '/settings', reason: 'expired' });
    expect(summaries()).not.toContain('Permission denied');
  });

  it('a 429 on a probe still says Slow down', async () => {
    overrides['/api/qb/status'] = [429, { detail: 'Too many requests' }];
    await mountSettings();
    expect(summaries()).toContain('Slow down');
  });
});
