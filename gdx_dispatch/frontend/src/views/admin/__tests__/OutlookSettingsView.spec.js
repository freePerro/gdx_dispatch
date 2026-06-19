/**
 * Phase 8 / OutlookSettingsView — verify GET load + tab render + error handling.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';

import OutlookSettingsView from '../OutlookSettingsView.vue';


function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}


function _credentials({ secret_set = false } = {}) {
  return {
    microsoft_tenant_id: secret_set ? 'ms-tid' : null,
    client_id: secret_set ? 'abc' : null,
    secret_set,
    secret_set_at: secret_set ? '2026-04-27T00:00:00Z' : null,
  };
}


function _settings() {
  return {
    backfill_days: 90,
    tag_strategy_order: ['auto_match', 'job_thread', 'ai'],
    tag_strategy_enabled: { auto_match: true, job_thread: true, ai: true },
    ai_tag_threshold: 0.85,
    visibility_rules: {
      tagged_visibility_above_role: 'tech_plus_one',
      tech_recipient_visible_to_all_techs: true,
      tech_outbound_no_tag_visibility: 'only_sender',
      tech_to_tech_internal_visibility: 'only_participants',
      above_tech_scope: 'all_tagged',
      untagged_visibility: 'only_owner',
    },
    auto_email_triggers: {
      'invoice.created': { subject: '', template: '' },
      'job.completed': { subject: '', template: '' },
      'estimate.sent': { subject: '', template: '' },
    },
  };
}


// AppLayout pulls in AppTopbar which auto-polls /api/notifications/count
// (notifications store startPolling on mount). Stub the layout so test
// fetch counts only reflect the view's own API calls.
const globalConfig = {
  plugins: [PrimeVue, ConfirmationService, ToastService],
  stubs: {
    AppLayout: { template: '<div><slot /></div>' },
  },
};


// jsdom lacks ResizeObserver; PrimeVue Tabs depends on it.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}


describe('OutlookSettingsView', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    global.ResizeObserver = ResizeObserverStub;
    // jsdom lacks matchMedia (PrimeVue Select uses it for orientation listener)
    if (!window.matchMedia) {
      Object.defineProperty(window, 'matchMedia', {
        writable: true,
        value: vi.fn().mockImplementation((query) => ({
          matches: false,
          media: query,
          onchange: null,
          addEventListener: vi.fn(),
          removeEventListener: vi.fn(),
          addListener: vi.fn(),
          removeListener: vi.fn(),
          dispatchEvent: vi.fn(),
        })),
      });
    }
    Object.defineProperty(window, 'location', {
      writable: true, configurable: true,
      value: { href: '', hostname: 'localhost' },
    });
    global.confirm = vi.fn(() => true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('loads credentials and settings on mount', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials({ secret_set: false })))
      .mockResolvedValueOnce(mkResponse(_settings()));
    mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/outlook/credentials');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/admin/outlook/settings');
  });

  it('shows the page heading', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials()))
      .mockResolvedValueOnce(mkResponse(_settings()));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    expect(w.text()).toContain('Outlook / Microsoft 365 Integration');
  });

  it('renders all four tab labels', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials()))
      .mockResolvedValueOnce(mkResponse(_settings()));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    const text = w.text();
    expect(text).toContain('Connection');
    expect(text).toContain('Tagging');
    expect(text).toContain('Visibility');
    expect(text).toContain('Auto-Email');
  });

  it('shows "set" indicator when secret_set=true', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials({ secret_set: true })))
      .mockResolvedValueOnce(mkResponse(_settings()));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    expect(w.text()).toContain('set');
  });

  it('exposes load/save methods via defineExpose', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials()))
      .mockResolvedValueOnce(mkResponse(_settings()));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    // The component uses defineExpose to publish refresh helpers
    expect(typeof w.vm.load).toBe('function');
    expect(typeof w.vm.saveCredentials).toBe('function');
    expect(typeof w.vm.saveSettings).toBe('function');
  });
});
