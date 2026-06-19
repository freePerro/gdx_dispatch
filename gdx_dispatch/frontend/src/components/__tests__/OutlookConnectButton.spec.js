/**
 * Slice outlook-s10 — verify Connect/Connected button states + click handlers.
 *
 * Mocks fetch directly (matching the repo pattern in useApi.spec.js); does
 * not stub useApi itself. Pinia is required because useApi pulls from the
 * auth store.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';

import OutlookConnectButton from '../OutlookConnectButton.vue';


function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}


const globalConfig = {
  plugins: [PrimeVue, ConfirmationService, ToastService],
};


describe('OutlookConnectButton', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    Object.defineProperty(window, 'location', {
      writable: true,
      configurable: true,
      value: { href: '', hostname: 'localhost' },
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders Connect button when not connected', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      connected: false,
      upn: null,
      display_name: null,
      connected_at: null,
      last_sync_at: null,
      last_error: null,
    }));
    const w = mount(OutlookConnectButton, { global: globalConfig });
    await flushPromises();
    expect(w.text()).toContain('Connect Outlook');
  });

  it('renders connected state with display_name + upn', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      connected: true,
      upn: 'doug@gdx',
      display_name: 'Doug B',
      connected_at: '2026-04-27T10:00:00Z',
      last_sync_at: null,
      last_error: null,
    }));
    const w = mount(OutlookConnectButton, { global: globalConfig });
    await flushPromises();
    expect(w.text()).toContain('Doug B');
    expect(w.text()).toContain('doug@gdx');
    expect(w.text()).toContain('Disconnect');
  });

  it('shows last_error when present', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({
      connected: true,
      upn: 'doug@gdx',
      display_name: null,
      connected_at: null,
      last_sync_at: null,
      last_error: 'token expired',
    }));
    const w = mount(OutlookConnectButton, { global: globalConfig });
    await flushPromises();
    expect(w.text()).toContain('token expired');
  });

  it('Connect click POSTs /api/oauth/outlook/start and navigates to returned URL', async () => {
    // GET /account on mount, then POST /start on click.
    fetchMock
      .mockResolvedValueOnce(mkResponse({
        connected: false,
        upn: null,
        display_name: null,
        connected_at: null,
        last_sync_at: null,
        last_error: null,
      }))
      .mockResolvedValueOnce(mkResponse({
        authorize_url: 'https://login.microsoftonline.com/ms-tid/oauth2/v2.0/authorize?client_id=abc',
      }));
    const w = mount(OutlookConnectButton, { global: globalConfig });
    await flushPromises();
    await w.find('button').trigger('click');
    await flushPromises();
    expect(fetchMock.mock.calls[1][0]).toBe('/api/oauth/outlook/start');
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST');
    expect(window.location.href).toBe(
      'https://login.microsoftonline.com/ms-tid/oauth2/v2.0/authorize?client_id=abc',
    );
  });

  it('logs loudly on a load failure (fail-loudly contract)', async () => {
    fetchMock.mockRejectedValueOnce(new Error('network down'));
    const consoleErrSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    mount(OutlookConnectButton, { global: globalConfig });
    await flushPromises();
    expect(consoleErrSpy).toHaveBeenCalled();
  });
});
