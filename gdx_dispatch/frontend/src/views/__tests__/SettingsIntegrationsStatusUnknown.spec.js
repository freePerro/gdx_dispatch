/**
 * GDXA-1129 — a failed integration status load is "Status unknown", never
 * "Not Connected" / "Not Configured".
 *
 * MOUNT tests, like SettingsRefuseDebit.spec.js. The old loadIntegrations()
 * swallowed every error and left the defaults, so a 500 from /api/qb/status
 * read as a definite "Not Connected" beside a Connect button that invited a
 * pointless re-OAuth of a live company. A 403 "module not enabled" (or a 404,
 * router not mounted) is QuickBooks genuinely off and stays "Not Connected".
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';

const apiGet = vi.fn();
const apiPatch = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, patch: apiPatch, post: apiPost, delete: vi.fn(), put: vi.fn() }),
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

import SettingsView from '../SettingsView.vue';

const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true,
  Tag: { props: ['value', 'severity'], template: '<span :data-severity="severity">{{ value }}</span>' },
  Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
  Select: true, InputNumber: true,
  ToggleSwitch: { props: ['modelValue', 'disabled'], emits: ['update:modelValue'], template: '<input type="checkbox" :checked="modelValue" :disabled="disabled" @change="$emit(\'update:modelValue\', $event.target.checked)" />' },
  AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
  PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
  SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
};

function httpError(status, message) {
  const err = new Error(message);
  err.status = status;
  return err;
}

const SETTINGS_OK = {
  integrations: { quickbooks: true, quickbooks_catalog_sync: false },
  stripe: { configured: true },
};

// routes: url -> value, or a function returning a promise.
function serve(routes) {
  apiGet.mockImplementation((url) => {
    if (!(url in routes)) return Promise.reject(new Error(`unmocked GET ${url}`));
    const r = routes[url];
    return typeof r === 'function' ? r() : Promise.resolve(r);
  });
}

async function mountSettings() {
  const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
  await flushPromises();
  return wrapper;
}

const tag = (w, id) => w.find(`[data-testid="${id}"]`);

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockReset();
  apiPost.mockReset();
  apiPatch.mockResolvedValue({});
  apiPost.mockResolvedValue({});
});

describe('Settings integrations — failed status load', () => {
  it('a rejecting /api/qb/status reads "Status unknown", not "Not Connected", with a Retry', async () => {
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => Promise.reject(httpError(500, 'Internal Server Error')),
    });
    const wrapper = await mountSettings();
    const qbo = tag(wrapper, 'qbo-status-tag');
    expect(qbo.text()).toBe('Status unknown');
    expect(qbo.attributes('data-severity')).toBe('secondary');
    expect(wrapper.find('[data-testid="qbo-status-unknown"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="qbo-connect-btn"]').text()).toContain('Connect or reconnect');
    expect(wrapper.find('[data-testid="qbo-status-retry"]').exists()).toBe(true);
  });

  it('a network failure (no status) is also "Status unknown"', async () => {
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => Promise.reject(new TypeError('Failed to fetch')),
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Status unknown');
  });

  it('Retry reloads the status and shows the real state', async () => {
    let fail = true;
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => (fail
        ? Promise.reject(httpError(502, 'Bad Gateway'))
        : Promise.resolve({ connected: true, last_sync_at: null, needs_reconnect: false })),
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Status unknown');
    fail = false;
    await wrapper.find('[data-testid="qbo-status-retry"]').trigger('click');
    await flushPromises();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Connected');
    expect(wrapper.find('[data-testid="qbo-status-retry"]').exists()).toBe(false);
  });

  it.each([
    [403, "Module 'quickbooks' is not enabled"],
    [404, 'Not Found'],
  ])('QB module off (%i) stays a definite "Not Connected"', async (status, message) => {
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => Promise.reject(httpError(status, message)),
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Not Connected');
    expect(wrapper.find('[data-testid="qbo-status-retry"]').exists()).toBe(false);
  });

  it('a 403 that is not the module gate is "Status unknown"', async () => {
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => Promise.reject(httpError(403, 'Forbidden')),
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Status unknown');
  });

  it('a successful disconnected status is "Not Connected" and replaces a connected snapshot', async () => {
    let connected = true;
    serve({
      '/api/settings/integrations': SETTINGS_OK,
      '/api/qb/status': () => Promise.resolve({ connected }),
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Connected');
    connected = false;
    await wrapper.vm.$.setupState.loadIntegrations();
    await flushPromises();
    expect(tag(wrapper, 'qbo-status-tag').text()).toBe('Not Connected');
  });

  it('a failed /api/settings/integrations load reads Stripe "Status unknown" and locks the catalog toggle', async () => {
    serve({
      '/api/settings/integrations': () => Promise.reject(httpError(500, 'Internal Server Error')),
      '/api/qb/status': { connected: true },
    });
    const wrapper = await mountSettings();
    const stripe = tag(wrapper, 'stripe-status-tag');
    expect(stripe.text()).toBe('Status unknown');
    expect(stripe.attributes('data-severity')).toBe('secondary');
    expect(wrapper.find('[data-testid="integrations-settings-retry"]').exists()).toBe(true);
    // PATCH /api/settings replaces the whole integrations map: toggling with
    // the map unknown would switch every other flag off.
    const toggle = wrapper.find('[data-testid="qbo-catalog-sync-toggle"]');
    expect(toggle.element.disabled).toBe(true);
    await wrapper.vm.$.setupState.toggleCatalogSync(true);
    expect(apiPatch).not.toHaveBeenCalledWith('/api/settings', expect.anything());
  });

  it('a 403 on the admin-only settings load says why and offers no Retry that cannot work', async () => {
    serve({
      '/api/settings/integrations': () => Promise.reject(httpError(403, 'Admin access required')),
      '/api/qb/status': { connected: false },
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'stripe-status-tag').text()).toBe('Status unknown');
    expect(wrapper.find('[data-testid="stripe-status-forbidden"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="integrations-settings-retry"]').exists()).toBe(false);
  });

  it('a failed reload after a success does not keep showing the old catalog toggle value', async () => {
    let fail = false;
    serve({
      '/api/settings/integrations': () => (fail
        ? Promise.reject(httpError(500, 'Internal Server Error'))
        : Promise.resolve({ integrations: { quickbooks_catalog_sync: true }, stripe: { configured: true } })),
      '/api/qb/status': { connected: true },
    });
    const wrapper = await mountSettings();
    const toggle = () => wrapper.find('[data-testid="qbo-catalog-sync-toggle"]').element;
    expect(toggle().checked).toBe(true);
    fail = true;
    await wrapper.vm.$.setupState.loadIntegrations();
    await flushPromises();
    expect(toggle().checked).toBe(false);
    expect(toggle().disabled).toBe(true);
    expect(tag(wrapper, 'stripe-status-tag').text()).toBe('Status unknown');
  });

  it('a loaded Stripe state is definite', async () => {
    serve({
      '/api/settings/integrations': { integrations: {}, stripe: { configured: false } },
      '/api/qb/status': { connected: false },
    });
    const wrapper = await mountSettings();
    expect(tag(wrapper, 'stripe-status-tag').text()).toBe('Not Configured');
    expect(wrapper.find('[data-testid="qbo-catalog-sync-toggle"]').element.disabled).toBe(false);
  });
});
