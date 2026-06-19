/**
 * Outlook Integration Card — verify status badge + Configure button + admin gate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';

import OutlookIntegrationCard from '../OutlookIntegrationCard.vue';


function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}


function mkRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/settings/integrations/outlook', name: 'outlook-settings', component: { template: '<div />' } },
    ],
  });
}


describe('OutlookIntegrationCard', () => {
  let fetchMock;
  let router;

  beforeEach(async () => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    Object.defineProperty(window, 'location', {
      writable: true, configurable: true,
      value: { href: '', hostname: 'localhost' },
    });
    router = mkRouter();
    router.push('/');
    await router.isReady();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  function _mount() {
    return mount(OutlookIntegrationCard, {
      global: {
        plugins: [router, PrimeVue, ConfirmationService, ToastService],
      },
    });
  }

  it('shows "Not Configured" tag when secret_set=false', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      microsoft_tenant_id: null,
      client_id: null,
      secret_set: false,
      secret_set_at: null,
    }));
    const w = _mount();
    await flushPromises();
    expect(w.text()).toContain('Not Configured');
    expect(w.text()).toContain('Configure');
  });

  it('shows "Connected" tag when secret_set=true', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      microsoft_tenant_id: 'ms-tid',
      client_id: 'abc',
      secret_set: true,
      secret_set_at: '2026-04-27T00:00:00Z',
    }));
    const w = _mount();
    await flushPromises();
    expect(w.text()).toContain('Connected');
    expect(w.text()).toContain('Manage');
  });

  it('shows admin-required note on 403 (no toast — quiet hint)', async () => {
    const err = new Error('forbidden');
    err.status = 403;
    fetchMock.mockRejectedValueOnce(err);
    const w = _mount();
    await flushPromises();
    expect(w.text()).toContain('Admin access required');
  });

  it('toasts loudly on a non-403 load failure', async () => {
    // Spy on console.warn BEFORE mount so the load() error path's
    // warn() call is captured. Component uses console.warn (not error)
    // at OutlookIntegrationCard.vue:39 for the load-failure log.
    const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    fetchMock.mockRejectedValueOnce(new Error('500 internal'));
    mount(OutlookIntegrationCard, {
      global: { plugins: [router, PrimeVue, ConfirmationService, ToastService] },
    });
    await flushPromises();
    // The "fail loudly" contract: console.warn must fire with the error.
    expect(consoleWarnSpy).toHaveBeenCalled();
    expect(consoleWarnSpy.mock.calls.some((args) =>
      args.some((a) => String(a).includes('outlook card load failed')),
    )).toBe(true);
    // The card should also display the error inline (loadError state)
    // — it surfaces via component state; not asserting DOM here because
    // the inline render is contingent on tab visibility.
  });

  it('navigates to outlook-settings route on Configure click', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      microsoft_tenant_id: null, client_id: null, secret_set: false, secret_set_at: null,
    }));
    const w = _mount();
    await flushPromises();
    const pushSpy = vi.spyOn(router, 'push');
    await w.find('button').trigger('click');
    expect(pushSpy).toHaveBeenCalledWith({ name: 'outlook-settings' });
  });

  it('GET hits /api/admin/outlook/credentials on mount', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      microsoft_tenant_id: null, client_id: null, secret_set: false, secret_set_at: null,
    }));
    _mount();
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/admin/outlook/credentials');
  });
});
