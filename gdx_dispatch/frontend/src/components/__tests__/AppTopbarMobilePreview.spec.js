/**
 * AppTopbar — the desktop "mobile field view (preview)" button toggles
 * (2026-10-18). It used to push /mobile unconditionally, so a second click
 * while previewing went nowhere and the only way back to desktop was the
 * nav drawer. On a /mobile route it is now pressed and returns to the page
 * the preview started from. Techs keep the one-way button: the router sends
 * a tech from every desktop surface back to /mobile.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import { reactive, ref } from 'vue';

const { push, route } = vi.hoisted(() => ({ push: vi.fn(), route: { current: null } }));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: vi.fn().mockResolvedValue({ count: 0 }) }),
  createApiClient: () => ({ get: vi.fn().mockResolvedValue({ count: 0 }) }),
}));
vi.mock('../../composables/useTenantModules', async () => {
  const { computed } = await import('vue');
  return {
    useTenantModules: () => ({
      isEnabled: () => false,
      enabledModules: computed(() => ({})),
      allEnabledModules: computed(() => []),
    }),
  };
});
vi.mock('../../composables/useViewMode', () => ({
  useViewMode: () => ({ isMobileViewport: ref(false), resetPreference: () => {} }),
}));
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, resolve: () => ({ matched: [] }) }),
  useRoute: () => route.current,
}));

// eslint-disable-next-line import/first
import AppTopbar from '../AppTopbar.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';

function goTo(fullPath) {
  route.current.fullPath = fullPath;
  route.current.path = fullPath.split('?')[0];
}

function mountBar(role = 'office') {
  const pinia = createPinia();
  setActivePinia(pinia);
  useAuthStore().user = { name: 'Test', role };
  return mount(AppTopbar, {
    global: {
      plugins: [PrimeVue, pinia],
      directives: { tooltip: {} },
      stubs: { AIAssistantPanel: true, HelpButton: true },
    },
  });
}

const launch = (w) => w.get('[data-test="mobile-launch"]');

describe('AppTopbar — mobile preview toggle', () => {
  beforeEach(() => {
    sessionStorage.clear();
    push.mockReset();
    route.current = reactive({ path: '/', fullPath: '/', query: {} });
  });

  it('opens the preview, then a second click returns to the starting page', async () => {
    goTo('/customers?tab=open');
    const w = mountBar();
    expect(launch(w).attributes('aria-pressed')).toBe('false');

    await launch(w).trigger('click');
    expect(push).toHaveBeenLastCalledWith('/mobile');

    goTo('/mobile/jobs'); // moving around inside the preview keeps it pressed
    await flushPromises();
    expect(launch(w).attributes('aria-pressed')).toBe('true');
    expect(launch(w).attributes('aria-label')).toBe('Back to desktop view');

    await launch(w).trigger('click');
    expect(push).toHaveBeenLastCalledWith('/customers?tab=open');
    w.unmount();
  });

  it('falls back to /dashboard when the preview was not opened from the button', async () => {
    goTo('/mobile');
    const w = mountBar();
    await launch(w).trigger('click');
    expect(push).toHaveBeenLastCalledWith('/dashboard');
    w.unmount();
  });

  it('a technician keeps the one-way button', async () => {
    goTo('/mobile');
    const w = mountBar('technician');
    expect(launch(w).attributes('aria-pressed')).toBe('false');
    await launch(w).trigger('click');
    expect(push).toHaveBeenLastCalledWith('/mobile');
    w.unmount();
  });
});
