import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { setActivePinia, createPinia } from 'pinia';
import PhoneComIntegrationCard from '../src/components/PhoneComIntegrationCard.vue';

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();

vi.mock('../src/composables/useApi', () => ({
  useApi: () => ({
    get: getMock,
    post: postMock,
    patch: patchMock,
    del: delMock,
    request: vi.fn(),
    put: vi.fn(),
  }),
}));

const _UNSET_STATE = {
  token_set: false,
  voip_id: null,
  default_extension_id: null,
  default_caller_id: null,
  last_validated_at: null,
  last_error: null,
  account_features: null,
  webhook_status: { registered: false, callback_id: null, listener_id: null },
};

const _CONFIGURED_STATE = {
  token_set: true,
  voip_id: 1000000,
  default_extension_id: '100',
  default_caller_id: '+18005550199',
  last_validated_at: '2026-04-27T18:00:00Z',
  last_error: null,
  account_features: { 'call-recording-on': false },
  webhook_status: { registered: true, callback_id: 555, listener_id: 777 },
};

const _MODULE_OFF = { modules: [{ key: 'phone_com', enabled: false }] };
const _MODULE_ON = { modules: [{ key: 'phone_com', enabled: true }] };


describe('PhoneComIntegrationCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    // Wave C added fetchCatalogs (numbers + extensions) on mount. Route those
    // to empty stubs so the existing mockResolvedValueOnce sequences for
    // /api/settings/* aren't consumed by the new endpoints.
    getMock.mockImplementation((url) => {
      if (url === '/api/phone-com/numbers') return Promise.resolve({ items: [] });
      if (url === '/api/phone-com/extensions') return Promise.resolve({ items: [] });
      return undefined;
    });
    if (!global.confirm) {
      global.confirm = vi.fn(() => true);
    } else {
      global.confirm = vi.fn(() => true);
    }
  });

  // ── unset state ──────────────────────────────────────────────────────

  it('renders unset state when no token configured', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_OFF)         // /api/settings/modules
      .mockResolvedValueOnce(_UNSET_STATE);       // /api/settings/integrations/phone-com
    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    expect(wrapper.text()).toContain('Phone.com Voice & SMS');
    expect(wrapper.text()).toContain('No token yet');
    expect(wrapper.text()).toContain('Create permanent token');
    expect(wrapper.text()).toContain('account-owner');
    expect(wrapper.find('[data-test="pc-token-input"]').exists()).toBe(true);
    // No re-test/disconnect/sync until token is set.
    expect(wrapper.find('[data-test="pc-retest"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pc-disconnect"]').exists()).toBe(false);
  });

  // ── configured state surfaces metadata ───────────────────────────────

  it('renders configured state with token-on-file + webhook-registered', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_CONFIGURED_STATE);
    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    expect(wrapper.text()).toContain('Token on file');
    expect(wrapper.text()).toContain('registered');
    expect(wrapper.text()).toContain('call-recording: off');
    expect(wrapper.find('[data-test="pc-retest"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pc-disconnect"]').exists()).toBe(true);

    // voip_id + defaults pre-filled into the inputs.
    expect(wrapper.find('[data-test="pc-voip-input"]').element.value).toBe('1000000');
    expect(wrapper.find('[data-test="pc-default-ext"]').element.value).toBe('100');
    expect(wrapper.find('[data-test="pc-default-caller"]').element.value).toBe('+18005550199');
  });

  // ── token NEVER renders ─────────────────────────────────────────────

  it('never renders the token value, only token_set bool', async () => {
    const SECRET = 'phc-token-MUST-NOT-LEAK-ABCDEF';
    // Backend will never return the token, but defend against accidental
    // future regressions: render the response and confirm the secret
    // doesn't appear in the text or any input value.
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce({
        ..._CONFIGURED_STATE,
        // Defense-in-depth: even if backend leaked, the card MUST NOT bind it.
        token: SECRET,
      });
    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    expect(wrapper.text()).not.toContain(SECRET);
    expect(wrapper.find('[data-test="pc-token-input"]').element.value).toBe('');
  });

  // ── Save & Test happy path ──────────────────────────────────────────

  it('PATCHes Save & Test and surfaces success status', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_UNSET_STATE);
    patchMock.mockResolvedValueOnce({
      ..._CONFIGURED_STATE,
      test_result: { ok: true, voip_id: 1000000, account_name: 'Example Owner', latency_ms: 87 },
      webhook_status: { registered: true, callback_id: 555, listener_id: 777 },
    });
    // Re-fetch after PATCH
    getMock.mockResolvedValueOnce(_CONFIGURED_STATE);

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-token-input"]').setValue('phc-good-token');
    await wrapper.find('[data-test="pc-voip-input"]').setValue('1000000');
    await wrapper.find('[data-test="pc-save-test"]').trigger('click');
    await flushPromises();

    expect(patchMock).toHaveBeenCalledWith(
      '/api/settings/integrations/phone-com',
      { token: 'phc-good-token', voip_id: 1000000 },
    );
    const status = wrapper.find('[data-test="pc-save-status"]');
    expect(status.exists()).toBe(true);
    expect(status.text()).toContain('Example Owner');
    expect(status.text()).toContain('webhook registered');
    // Token field cleared after success.
    expect(wrapper.find('[data-test="pc-token-input"]').element.value).toBe('');
  });

  // ── Save & Test failure surfaces error ──────────────────────────────

  it('surfaces 401-class errors from PATCH validation', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_UNSET_STATE);
    patchMock.mockResolvedValueOnce({
      ..._UNSET_STATE,
      test_result: { ok: false, error: '401: invalid_x_api_key', voip_id: null,
                     account_name: null, latency_ms: 80 },
      webhook_status: { registered: false, callback_id: null, listener_id: null },
    });
    getMock.mockResolvedValueOnce(_UNSET_STATE);

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-token-input"]').setValue('phc-bad-token-12345');
    await wrapper.find('[data-test="pc-save-test"]').trigger('click');
    await flushPromises();

    const status = wrapper.find('[data-test="pc-save-status"]');
    expect(status.text()).toContain('401');
  });

  // ── Re-test ─────────────────────────────────────────────────────────

  it('Re-test calls POST /test', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_CONFIGURED_STATE)
      // Wave C: fetchCatalogs runs when token_set, consume those before refetch.
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [] });
    postMock.mockResolvedValueOnce({
      ok: true, voip_id: 1000000, account_name: 'Example Owner', latency_ms: 42,
    });
    getMock.mockResolvedValueOnce(_CONFIGURED_STATE);

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-retest"]').trigger('click');
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith('/api/settings/integrations/phone-com/test');
    expect(wrapper.find('[data-test="pc-save-status"]').text()).toContain('42ms');
  });

  // ── Sync now ────────────────────────────────────────────────────────

  it('Sync now calls POST /sync-now', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_CONFIGURED_STATE);
    postMock.mockResolvedValueOnce({
      ok: true, calls_synced: 12, messages_synced: 4, voicemails_synced: 1,
    });

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-sync-now"]').trigger('click');
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith(
      '/api/settings/integrations/phone-com/sync-now',
    );
    const status = wrapper.find('[data-test="pc-save-status"]').text();
    expect(status).toContain('12 calls');
    expect(status).toContain('4 SMS');
  });

  // ── Disconnect ──────────────────────────────────────────────────────

  it('Disconnect calls DELETE /token after confirm', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_CONFIGURED_STATE)
      // Wave C: fetchCatalogs runs when token_set.
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [] });
    delMock.mockResolvedValueOnce({ cleared: true, webhook_disconnect: {} });
    getMock.mockResolvedValueOnce(_UNSET_STATE);

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-disconnect"]').trigger('click');
    await flushPromises();

    expect(delMock).toHaveBeenCalledWith(
      '/api/settings/integrations/phone-com/token',
    );
    expect(wrapper.find('[data-test="pc-save-status"]').text()).toContain('Disconnected');
  });

  // ── module toggle ──────────────────────────────────────────────────

  it('module toggle hits /api/settings/modules/phone_com/enable', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_OFF)
      .mockResolvedValueOnce(_UNSET_STATE);
    postMock.mockResolvedValueOnce({});

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="phone-com-module-toggle"]').setValue(true);
    await flushPromises();

    expect(postMock).toHaveBeenCalledWith('/api/settings/modules/phone_com/enable');
  });

  // ── nothing-to-save guard ──────────────────────────────────────────

  it('Save & Test does nothing when no field changed', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON)
      .mockResolvedValueOnce(_CONFIGURED_STATE);
    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    await wrapper.find('[data-test="pc-save-test"]').trigger('click');
    await flushPromises();

    expect(patchMock).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="pc-save-status"]').text()).toContain('Nothing to save');
  });

  // ── admin-only handling for non-admins ─────────────────────────────

  it('shows admin-only message on 403 from settings GET', async () => {
    getMock
      .mockResolvedValueOnce(_MODULE_ON);
    getMock.mockRejectedValueOnce({ status: 403, message: 'admin only' });

    const wrapper = mount(PhoneComIntegrationCard);
    await flushPromises();

    expect(wrapper.text()).toContain('Admin access required');
    expect(wrapper.find('[data-test="pc-token-input"]').exists()).toBe(false);
  });
});
