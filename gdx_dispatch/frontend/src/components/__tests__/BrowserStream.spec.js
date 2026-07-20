import { describe, expect, it, vi, beforeEach } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

// Remembered-login state the fake API serves; mutated per test.
const apiState = { saved: false, username: '' };
const apiGet = vi.fn(async (url) => {
  if (url.startsWith('/api/plugins/_browser/credentials')) return { ...apiState };
  return [];
});
const apiPost = vi.fn(async () => ({ saved: true }));
const apiDel = vi.fn(async () => ({ saved: false }));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, del: apiDel }),
}));
vi.mock('../../composables/useBrowserStream', () => ({
  useBrowserStream: () => ({
    frameSrc: { value: null },
    connected: { value: false },
    error: { value: null },
    connect: vi.fn(),
    mouse: vi.fn(), wheel: vi.fn(), key: vi.fn(), paste: vi.fn(),
    imeInput: vi.fn(), seedKeyboard: vi.fn(),
    compositionStart: vi.fn(), compositionEnd: vi.fn(),
    capturePage: vi.fn(), disconnect: vi.fn(),
  }),
}));

// eslint-disable-next-line import/first
import BrowserStream from '../BrowserStream.vue';

// Dialog stub that renders its slots so the form is testable.
const DialogStub = {
  props: ['visible'],
  template: '<div v-if="visible"><slot /><slot name="footer" /></div>',
};
const stubs = {
  Button: { props: ['label', 'loading', 'disabled'], template: '<button :disabled="disabled">{{ label }}</button>' },
  Select: true,
  InputText: true,
  Password: true,
  Dialog: DialogStub,
};

function mountStream() {
  return mount(BrowserStream, {
    props: { pluginKey: 'chipricing', url: 'https://orderentry.chiohd.com/' },
    global: { stubs },
  });
}

describe('BrowserStream.vue remembered login', () => {
  beforeEach(() => {
    apiState.saved = false;
    apiState.username = '';
    vi.clearAllMocks();
  });

  it('checks credential status on mount and offers "Remember login"', async () => {
    const w = mountStream();
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/plugins/_browser/credentials?key=chipricing');
    expect(w.find('[data-testid="browser-creds-btn"]').text()).toContain('Remember login');
    expect(w.find('[data-testid="browser-creds-dialog"]').exists()).toBe(false);
  });

  it('shows "Login remembered" when credentials exist', async () => {
    apiState.saved = true;
    apiState.username = 'doug@x.com';
    const w = mountStream();
    await flushPromises();
    expect(w.find('[data-testid="browser-creds-btn"]').text()).toContain('Login remembered');
  });

  it('saves credentials with the plugin key and closes the dialog', async () => {
    const w = mountStream();
    await flushPromises();
    await w.find('[data-testid="browser-creds-btn"]').trigger('click');
    w.vm.credsUsername = 'doug@x.com';
    w.vm.credsPassword = 'pw-1';
    await w.vm.onSaveCreds();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/plugins/_browser/credentials',
      { key: 'chipricing', username: 'doug@x.com', password: 'pw-1' },
      expect.anything(),
    );
    expect(w.vm.credsSaved).toBe(true);
    expect(w.vm.credsPassword).toBe(''); // never kept in component state
    expect(w.vm.credsOpen).toBe(false);
  });

  it('forget deletes and resets to unsaved', async () => {
    apiState.saved = true;
    apiState.username = 'doug@x.com';
    const w = mountStream();
    await flushPromises();
    await w.vm.onForgetCreds();
    expect(apiDel).toHaveBeenCalledWith('/api/plugins/_browser/credentials?key=chipricing');
    expect(w.vm.credsSaved).toBe(false);
    expect(w.vm.credsUsername).toBe('');
  });
});
