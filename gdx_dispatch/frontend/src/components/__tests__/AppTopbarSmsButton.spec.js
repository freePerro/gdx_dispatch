/**
 * AppTopbar — the phone's SMS button (2026-10-14).
 *
 * On a phone the sidebar's SMS pin and its unread badge sit in a drawer, so
 * the top bar carries an SMS button beside search and the bell. It shows, and
 * polls unread-count, only on a phone viewport with the Phone.com SMS module
 * in the user's permission-filtered list — and only once that list has loaded,
 * because before then it is the optimistic default (GDXA-453).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const { apiGet, push, state } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  push: vi.fn(),
  state: { mobile: null, modules: null, release: null },
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useTenantModules', async () => {
  const { ref: vueRef, computed } = await import('vue');
  state.modules = vueRef([]);
  return {
    useTenantModules: () => ({
      isEnabled: () => false, // communications off: keep the bell's poll out of the counts
      enabledModules: computed(() => ({})),
      allEnabledModules: computed(() => state.modules.value),
      loadTenantModules: () => new Promise((resolve) => { state.release = resolve; }),
    }),
  };
});
vi.mock('../../composables/useViewMode', async () => {
  const { ref: vueRef } = await import('vue');
  state.mobile = vueRef(true);
  return { useViewMode: () => ({ isMobileViewport: state.mobile, resetPreference: () => {} }) };
});
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, resolve: () => ({ matched: [] }) }),
  useRoute: () => ({ path: '/mobile', query: {}, fullPath: '/mobile' }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, user: { role: 'office' }, claims: {} }),
}));

// eslint-disable-next-line import/first
import AppTopbar from '../AppTopbar.vue';

const SMS = { key: 'phone_com_messages', to: '/phone-com/messages' };

function mountBar() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(AppTopbar, {
    global: {
      plugins: [PrimeVue, pinia],
      directives: { tooltip: {} },
      stubs: { AIAssistantPanel: true, HelpButton: true, Menu: true, Dialog: true },
    },
  });
}

const smsCalls = () =>
  apiGet.mock.calls.filter(([url]) => url === '/api/phone-com/messages/unread-count').length;
const button = (w) => w.find('[data-testid="mobile-sms-btn"]');

describe('AppTopbar — SMS button', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    apiGet.mockReset().mockResolvedValue({ count: 3 });
    push.mockReset();
    state.mobile.value = true;
    state.modules.value = [SMS];
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('waits for the module list, then shows the unread count and opens texts', async () => {
    const w = mountBar();
    await flushPromises();
    expect(button(w).exists()).toBe(false);
    expect(smsCalls()).toBe(0);

    state.release();
    await flushPromises();
    expect(button(w).exists()).toBe(true);
    expect(smsCalls()).toBe(1);
    expect(button(w).text()).toContain('3');
    expect(button(w).attributes('aria-label')).toBe('Text messages, 3 unread');

    await button(w).trigger('click');
    expect(push).toHaveBeenCalledWith('/phone-com/messages');
    w.unmount();
  });

  it('is absent, and never polls, without the SMS module', async () => {
    state.modules.value = [];
    const w = mountBar();
    state.release();
    await flushPromises();
    expect(button(w).exists()).toBe(false);
    expect(smsCalls()).toBe(0);
    w.unmount();
  });

  it('is absent on a desktop viewport, where the sidebar pin carries the badge', async () => {
    state.mobile.value = false;
    const w = mountBar();
    state.release();
    await flushPromises();
    expect(button(w).exists()).toBe(false);
    expect(smsCalls()).toBe(0);
    w.unmount();
  });

  it('stops polling on unmount', async () => {
    const w = mountBar();
    state.release();
    await flushPromises();
    expect(smsCalls()).toBe(1);
    w.unmount();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(smsCalls()).toBe(1);
  });
});
