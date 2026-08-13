/**
 * SimpleFIN card — connect flow, status/quota render, settings save,
 * Sync Now cap gating, re-link dialog trigger.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';

import SimpleFINCard from '../SimpleFINCard.vue';

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const DISCONNECTED = {
  connected: false, auth_state: null, stale: false, stale_after_hours: 48,
  quota: { used: 0, cap: 20, remaining: 20, date: '2026-08-13' },
  schedule: {
    frequency: 'manual', fetch_window_start: null, fetch_window_end: null,
    daily_fetch_cap: 20, daily_fetch_cap_max: 20,
  },
  backfill_done: true, accounts: [], timezone: 'America/Chicago',
};

const CONNECTED = {
  ...DISCONNECTED,
  connected: true, auth_state: 'healthy',
  last_synced_at: '2026-08-13T12:00:00+00:00',
  quota: { used: 7, cap: 20, remaining: 13, date: '2026-08-13' },
  schedule: {
    frequency: 'daily', fetch_window_start: '07:00', fetch_window_end: '19:00',
    daily_fetch_cap: 20, daily_fetch_cap_max: 20,
    last_run_at: null, next_run_at: null, last_run_status: null, last_run_error: null,
    backfill_days: 365,
  },
  accounts: [{
    id: 'acct-uuid-1', external_account_id: 'A1', name: 'Business Checking 2204',
    account_number_masked: '•2204', sync_enabled: true, is_inactive: false,
    initial_backfill_done: true, backfill_synced_through: '2026-08-13',
  }],
};

function mountCard() {
  return mount(SimpleFINCard, {
    global: { plugins: [PrimeVue, ToastService, createPinia()] },
  });
}

describe('SimpleFINCard', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    localStorage.setItem('token', 'test-jwt');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('renders the connect flow when not connected', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(DISCONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Not Connected');
    expect(wrapper.find('[data-testid="sfin-token-input"]').exists()).toBe(true);
    const btn = wrapper.find('[data-testid="sfin-connect-btn"]');
    expect(btn.exists()).toBe(true);
    expect(btn.attributes('disabled')).toBeDefined(); // empty token = disabled
  });

  it('posts the setup token and opens the re-link dialog on proposals', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(DISCONNECTED))                       // initial load
      .mockResolvedValueOnce(mkResponse({                                     // connect
        connected: true, bridge_host: 'bridge.example',
        preview: {
          incoming: [{ id: 'NEW-9', name: 'Checking', balance: '1.00' }],
          orphaned: [{ account_id: 'acct-uuid-1', name: 'Checking', external_account_id: 'OLD-1' }],
          new: [{ id: 'NEW-9', name: 'Checking' }],
          proposals: [{ account_id: 'acct-uuid-1', account_name: 'Checking', new_external_id: 'NEW-9', confidence: 1 }],
        },
        warning: null,
      }))
      .mockResolvedValueOnce(mkResponse(CONNECTED));                          // reload
    const wrapper = mountCard();
    await flushPromises();
    await wrapper.find('[data-testid="sfin-token-input"]').setValue('dG9rZW4tdG9rZW4=');
    await wrapper.find('[data-testid="sfin-connect-btn"]').trigger('click');
    await flushPromises();

    const connectCall = fetchMock.mock.calls[1];
    expect(connectCall[0]).toContain('/api/bank-feeds/simplefin/connect');
    expect(JSON.parse(connectCall[1].body)).toEqual({ setup_token: 'dG9rZW4tdG9rZW4=' });
    // Dialog content is teleported; assert on reactive state instead.
    expect(wrapper.vm.relinkVisible).toBe(true);
    expect(wrapper.vm.relinkProposals).toHaveLength(1);
  });

  it('shows quota, last sync, and saves schedule settings', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(CONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-quota"]').text()).toContain('7/20');
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Connected');

    fetchMock
      .mockResolvedValueOnce(mkResponse({}))          // PUT settings
      .mockResolvedValueOnce(mkResponse(CONNECTED));  // reload
    await wrapper.find('[data-testid="sfin-window-start"]').setValue('06:00');
    await wrapper.find('[data-testid="sfin-window-end"]').setValue('20:00');
    await wrapper.find('[data-testid="sfin-save-settings"]').trigger('click');
    await flushPromises();
    const putCall = fetchMock.mock.calls[1];
    expect(putCall[0]).toContain('/api/bank-feeds/simplefin/settings');
    expect(putCall[1].method).toBe('PUT');
    const body = JSON.parse(putCall[1].body);
    expect(body.fetch_window_start).toBe('06:00');
    expect(body.fetch_window_end).toBe('20:00');
    expect(body.daily_fetch_cap).toBe(20);
  });

  it('disables Sync Now when the daily cap is reached', async () => {
    const capped = {
      ...CONNECTED,
      quota: { used: 20, cap: 20, remaining: 0, date: '2026-08-13' },
    };
    fetchMock.mockResolvedValueOnce(mkResponse(capped));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-sync-now"]').attributes('disabled')).toBeDefined();
    expect(wrapper.text()).toContain('Daily fetch cap reached');
  });

  it('flags a stale connection', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, stale: true }));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Stale');
    expect(wrapper.find('[data-testid="sfin-stale-banner"]').exists()).toBe(true);
  });

  it('shows the reconnect banner when the bridge needs re-auth', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ...CONNECTED, auth_state: 'needs_reconnect' }));
    const wrapper = mountCard();
    await flushPromises();
    expect(wrapper.find('[data-testid="sfin-status-tag"]').text()).toBe('Reconnect required');
    expect(wrapper.find('[data-testid="sfin-reconnect-banner"]').exists()).toBe(true);
  });

  it('requires a second click to disconnect', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse(CONNECTED));
    const wrapper = mountCard();
    await flushPromises();
    const btn = wrapper.find('[data-testid="sfin-disconnect"]');
    await btn.trigger('click');
    expect(fetchMock).toHaveBeenCalledTimes(1); // still only the initial load
    expect(btn.text()).toContain('Really disconnect?');
    fetchMock
      .mockResolvedValueOnce(mkResponse({ disconnected: 1 }))
      .mockResolvedValueOnce(mkResponse(DISCONNECTED));
    await btn.trigger('click');
    await flushPromises();
    expect(fetchMock.mock.calls[1][0]).toContain('/api/bank-feeds/simplefin/disconnect');
  });
});
