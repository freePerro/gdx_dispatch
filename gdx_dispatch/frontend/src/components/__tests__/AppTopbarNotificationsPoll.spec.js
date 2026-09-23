/**
 * AppTopbar — the notifications poll follows the bar's lifecycle (2026-09-22).
 *
 * The bar used to start the poll from a watch and never release it: the shell
 * unmounts on logout (App.vue `noShell`), so the poll outlived the session and
 * a re-login stacked a second subscription the module-off watcher could no
 * longer stop. This mounts the real component (shallow — PrimeVue chrome
 * stubbed, the <script setup> runs for real) and drives the three transitions:
 * module on → poll; module off → released; unmount → released.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import { ref } from 'vue';

const { apiGet, communicationsOn } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  communicationsOn: { ref: null },
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useTenantModules', async () => {
  const { ref: vueRef, computed } = await import('vue');
  communicationsOn.ref = vueRef(true);
  return {
    useTenantModules: () => ({
      isEnabled: (key) => (key === 'communications' ? communicationsOn.ref.value : true),
      enabledModules: computed(() => ({})),
      allEnabledModules: computed(() => []),
    }),
  };
});
vi.mock('../../composables/useViewMode', () => ({
  useViewMode: () => ({ isMobileViewport: ref(false), resetPreference: () => {} }),
}));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), resolve: () => ({ matched: [] }) }),
  useRoute: () => ({ path: '/dashboard', query: {}, fullPath: '/dashboard' }),
}));

// eslint-disable-next-line import/first
import AppTopbar from '../AppTopbar.vue';

function mountBar() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(AppTopbar, {
    shallow: true,
    global: {
      plugins: [PrimeVue, pinia],
      directives: { tooltip: {} },
    },
  });
}

const countCalls = () =>
  apiGet.mock.calls.filter(([url]) => url === '/api/notifications/count').length;

describe('AppTopbar — notifications poll lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    apiGet.mockReset().mockResolvedValue({ count: 0 });
    communicationsOn.ref.value = true;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls while mounted with the module on, and releases on unmount', async () => {
    const wrapper = mountBar();
    await flushPromises();
    expect(countCalls()).toBe(1);
    vi.advanceTimersByTime(60000);
    await flushPromises();
    expect(countCalls()).toBe(2);

    wrapper.unmount(); // logout unmounts the shell
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(countCalls()).toBe(2);
  });

  it('logout → login: the second bar can still stop its poll when the module goes off', async () => {
    const first = mountBar();
    await flushPromises();
    first.unmount();

    const second = mountBar();
    await flushPromises();
    const afterMount = countCalls();
    communicationsOn.ref.value = false; // admin turns communications off
    await flushPromises();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(countCalls()).toBe(afterMount);
    second.unmount();
  });
});
