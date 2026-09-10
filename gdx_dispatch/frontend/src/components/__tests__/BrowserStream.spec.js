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
// Mutable so a test can drive the recorder heartbeat.
const recRef = { value: null };

vi.mock('../../composables/useBrowserStream', () => ({
  useBrowserStream: () => ({
    frameSrc: { value: null },
    connected: { value: false },
    error: { value: null },
    rec: recRef,
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
    props: { pluginKey: 'chipricing', url: 'https://portal.example.invalid/' },
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

  it('a 403 means "no remembered login for you" — the button offers to save one', async () => {
    // Non-owner, or no consent: the store really has nothing for this user, and
    // the save would 403 too. "Remember login" is the honest label.
    apiGet.mockRejectedValueOnce(Object.assign(new Error('forbidden'), { status: 403 }));
    const w = mountStream();
    await flushPromises();
    expect(w.find('[data-testid="browser-creds-btn"]').text()).toContain('Remember login');
    expect(w.vm.credsUnknown).toBe(false);
  });

  it('a 5xx means we could not ASK — it must not claim no login is remembered', async () => {
    // Since #596 the plugin browser service can refuse this server's own
    // internal token, surfacing here as 502. Reporting that as "no sign-in
    // remembered" invites the owner to re-enter a credential that is already
    // stored and was never the problem.
    apiGet.mockRejectedValueOnce(Object.assign(new Error('bad gateway'), { status: 502 }));
    const w = mountStream();
    await flushPromises();
    const label = w.find('[data-testid="browser-creds-btn"]').text();
    expect(label).toContain('unknown');
    expect(label).not.toContain('Remember login');
    expect(label).not.toContain('Login remembered');
    expect(w.vm.credsUnknown).toBe(true);
  });

  it('a successful save settles the question a 5xx left open', async () => {
    // credsUnknown was assigned only inside loadCredsStatus, so after a 502 the
    // button kept reading "Login status unknown" even after a save that
    // demonstrably reached the store — a label asserting a fact that had since
    // become false, which is the class this branch exists to remove.
    apiGet.mockRejectedValueOnce(Object.assign(new Error('bad gateway'), { status: 502 }));
    const w = mountStream();
    await flushPromises();
    expect(w.vm.credsUnknown).toBe(true);

    await w.find('[data-testid="browser-creds-btn"]').trigger('click');
    w.vm.credsUsername = 'doug@x.com';
    w.vm.credsPassword = 'pw';
    await w.vm.onSaveCreds();
    await flushPromises();

    expect(w.vm.credsUnknown).toBe(false);
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

describe('recording badge', () => {
  afterEach(() => { recRef.value = null; });

  it('shows nothing until the server sends a heartbeat', async () => {
    const w = mount(BrowserStream, { props: { url: 'https://portal.example.invalid/' } });
    await w.vm.$nextTick();
    expect(w.find('[data-testid="browser-rec"]').exists()).toBe(false);
  });

  it('reports recording, with counts, when the server says so', async () => {
    recRef.value = { recording: true, degraded: false, events: 12, bytes: 4096, captures: 2, dropped: 0 };
    const w = mount(BrowserStream, { props: { url: 'https://portal.example.invalid/' } });
    await w.vm.$nextTick();
    const badge = w.find('[data-testid="browser-rec"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toContain('2 captured');
    expect(badge.classes()).toContain('is-on');
  });

  it('goes red when the recorder degrades — the counterfactual that makes the badge evidence', async () => {
    recRef.value = { recording: false, degraded: true, reason: 'permission denied', events: 3, bytes: 0 };
    const w = mount(BrowserStream, { props: { url: 'https://portal.example.invalid/' } });
    await w.vm.$nextTick();
    const badge = w.find('[data-testid="browser-rec"]');
    expect(badge.text()).toBe('Recording failed');
    expect(badge.classes()).toContain('is-bad');
    expect(badge.attributes('title')).toContain('permission denied');
  });
});
