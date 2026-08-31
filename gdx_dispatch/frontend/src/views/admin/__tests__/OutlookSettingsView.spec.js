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


function _settings({ allowlist = [] } = {}) {
  return {
    vendor_bill_sender_allowlist: allowlist,
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

  it('renders all five tab labels', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials()))
      .mockResolvedValueOnce(mkResponse(_settings()));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    const text = w.text();
    expect(text).toContain('Connection');
    expect(text).toContain('Tagging');
    expect(text).toContain('Visibility');
    expect(text).toContain('Vendor Bills');
    // Auto-Email tab retired 2026-08-31 (its templates were read by nothing).
    expect(text).not.toContain('Auto-Email');
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

  // ── Vendor Bills tab ────────────────────────────────────────────────
  //
  // The allowlist is the on-switch for auto-filing supplier PDFs. It had no UI
  // at all until 2026-07-28 — it could only be set with hand-written SQL
  // against prod, so nobody but an engineer could add a vendor.

  async function mountWith(allowlist) {
    fetchMock
      .mockResolvedValueOnce(mkResponse(_credentials()))
      .mockResolvedValueOnce(mkResponse(_settings({ allowlist })));
    const w = mount(OutlookSettingsView, { global: globalConfig });
    await flushPromises();
    return w;
  }

  it('saves the allowlist back to the settings endpoint', async () => {
    const w = await mountWith(['installed.net']);
    w.vm.$.setupState.settings.vendor_bill_sender_allowlist.push('newvendor.com');
    await flushPromises();
    fetchMock.mockResolvedValueOnce(
      mkResponse(_settings({ allowlist: ['installed.net', 'newvendor.com'] })),
    );

    await w.vm.saveSettings();
    await flushPromises();

    const patch = fetchMock.mock.calls.at(-1);
    expect(patch[0]).toBe('/api/admin/outlook/settings');
    expect(JSON.parse(patch[1].body).vendor_bill_sender_allowlist)
      .toEqual(['installed.net', 'newvendor.com']);
  });

  it('warns that intake is off while the allowlist is empty', async () => {
    const w = await mountWith([]);
    expect(w.text()).toContain('Vendor bill intake is off');
  });

  it('drops the off-warning once a sender is listed', async () => {
    const w = await mountWith(['installed.net']);
    expect(w.text()).not.toContain('Vendor bill intake is off');
  });

  it('flags a consumer mail domain as over-broad', async () => {
    // Allowlisting gmail.com matches every gmail sender, not just the vendor.
    const w = await mountWith(['gmail.com']);
    expect(w.text()).toContain('consumer mail domain');
  });

  it('does not flag an ordinary vendor domain', async () => {
    const w = await mountWith(['installed.net']);
    expect(w.text()).not.toContain('consumer mail domain');
  });

  it('queues a sweep with the chosen window', async () => {
    const w = await mountWith(['installed.net']);
    fetchMock.mockResolvedValueOnce(mkResponse({ queued: [{ account_id: 'a', task_id: 't' }], days: 400 }));

    w.vm.sweepDays = 400;
    await w.vm.runSweep();
    await flushPromises();

    const call = fetchMock.mock.calls.at(-1);
    expect(call[0]).toBe('/api/admin/outlook/vendor-bills/sweep');
    expect(JSON.parse(call[1].body)).toEqual({ days: 400 });
  });

  it('does not offer the sweep while nothing is allowlisted', async () => {
    // The endpoint 400s on an empty allowlist; don't hand the user that error.
    const w = await mountWith([]);
    const btn = w.find('[data-test="vendor-bill-sweep"]');
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('disabled')).toBeDefined();
  });

  // The sweep reads the SAVED allowlist. An edit left unsaved on screen would
  // quietly not apply, and the confirm dialog that used to carry this warning
  // never renders (useDestructiveConfirm resolves its service outside setup and
  // silently auto-accepts), so the guard has to be inline and structural.
  it('blocks the sweep while the allowlist has unsaved edits', async () => {
    const w = await mountWith(['installed.net']);
    expect(w.find('[data-test="vendor-bill-sweep"]').attributes('disabled')).toBeUndefined();

    w.vm.$.setupState.settings.vendor_bill_sender_allowlist.push('newvendor.com');
    await flushPromises();

    expect(w.find('[data-test="vendor-bill-sweep"]').attributes('disabled')).toBeDefined();
    expect(w.find('[data-test="vendor-bill-dirty"]').exists()).toBe(true);
  });

  // PrimeVue's AutoComplete commits a chip only on Enter. Without a blur
  // handler, typing a sender and clicking Save sends the OLD list and reports
  // success — the exact "why didn't it save?" this page exists to remove.
  it('keeps a sender that was typed but never Enter-ed', async () => {
    const w = await mountWith(['installed.net']);
    const input = w.find('[data-test="vendor-bill-allowlist"] input');
    expect(input.exists()).toBe(true);

    input.element.value = 'billing@newvendor.com';
    await input.trigger('blur');
    await flushPromises();

    expect(w.vm.$.setupState.settings.vendor_bill_sender_allowlist)
      .toEqual(['installed.net', 'billing@newvendor.com']);
  });

  it('does not duplicate a sender already chipped', async () => {
    const w = await mountWith(['installed.net']);
    const input = w.find('[data-test="vendor-bill-allowlist"] input');
    input.element.value = 'INSTALLED.NET';
    await input.trigger('blur');
    await flushPromises();
    expect(w.vm.$.setupState.settings.vendor_bill_sender_allowlist).toEqual(['installed.net']);
  });

  it('ignores an empty box on blur', async () => {
    const w = await mountWith(['installed.net']);
    const input = w.find('[data-test="vendor-bill-allowlist"] input');
    input.element.value = '   ';
    await input.trigger('blur');
    await flushPromises();
    expect(w.vm.$.setupState.settings.vendor_bill_sender_allowlist).toEqual(['installed.net']);
  });

  // Every tab's Save posts the same payload. Resending an untouched allowlist
  // would let one malformed stored entry 422 the Tagging/Visibility tabs.
  it('omits the allowlist from the payload when it has not changed', async () => {
    const w = await mountWith(['installed.net']);
    fetchMock.mockResolvedValueOnce(mkResponse(_settings({ allowlist: ['installed.net'] })));

    await w.vm.saveSettings();
    await flushPromises();

    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body);
    expect(body).not.toHaveProperty('vendor_bill_sender_allowlist');
    expect(body).toHaveProperty('backfill_days');
  });

  it('re-enables the sweep once the edit is saved', async () => {
    const w = await mountWith(['installed.net']);
    w.vm.$.setupState.settings.vendor_bill_sender_allowlist.push('newvendor.com');
    await flushPromises();
    expect(w.find('[data-test="vendor-bill-sweep"]').attributes('disabled')).toBeDefined();

    fetchMock.mockResolvedValueOnce(
      mkResponse(_settings({ allowlist: ['installed.net', 'newvendor.com'] })),
    );
    await w.vm.saveSettings();
    await flushPromises();

    expect(w.find('[data-test="vendor-bill-dirty"]').exists()).toBe(false);
    expect(w.find('[data-test="vendor-bill-sweep"]').attributes('disabled')).toBeUndefined();
  });
});
