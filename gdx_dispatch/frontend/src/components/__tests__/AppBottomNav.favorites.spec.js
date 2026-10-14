/**
 * More-drawer favorites (Doug, 2026-10-13: "favorites that make it easier to
 * access parts of it"). Mounts the real component: a star on each drawer
 * entry pins it to a Favorites section at the top, the pin survives a remount
 * (localStorage, per device), a favorite the user can no longer see drops out,
 * and filtering hides the section so search results stay in one place.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const { apiGet, modules, modulesLoaded, syncSpy } = await vi.hoisted(async () => {
  const { ref } = await import('vue');
  return {
    apiGet: vi.fn(),
    modules: { value: [] },
    modulesLoaded: ref(true),
    syncSpy: vi.fn(),
  };
});

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ allEnabledModules: modules, loaded: modulesLoaded, isEnabled: () => true }),
}));
vi.mock('../../lib/mobileFavorites', async (importOriginal) => {
  const real = await importOriginal();
  return { ...real, syncAppShortcuts: (entries) => { syncSpy(entries); return real.syncAppShortcuts(entries); } };
});
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/mobile/jobs', query: {}, fullPath: '/mobile/jobs' }),
  useRouter: () => ({
    push: vi.fn().mockResolvedValue(undefined),
    replace: vi.fn().mockResolvedValue(undefined),
    resolve: (path) => ({ matched: [{ path }] }),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

// eslint-disable-next-line import/first
import AppBottomNav from '../AppBottomNav.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { _resetForTests, FAVORITES_KEY } from '../../lib/mobileFavorites';

const DrawerStub = {
  props: ['visible'],
  template: '<div data-testid="more-drawer"><slot v-if="visible" /></div>',
};

const MODULES = [
  { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing', type: 'Money' },
  { key: 'sms', label: 'Text Messages', icon: 'pi pi-comments', to: '/phone-com/messages', type: 'Operations' },
  { key: 'reports', label: 'Reports', icon: 'pi pi-chart-bar', to: '/reports', type: 'Admin' },
];

const jwtFor = (role) => `h.${btoa(JSON.stringify({ role })).replace(/=+$/, '')}.s`;

async function openDrawer({ role = 'owner', permissionsLoaded = true } = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.accessToken = jwtFor(role);
  auth.user = { role };
  auth.permissions = new Set(permissionsLoaded ? ['nav.office'] : []);
  auth.permissionsLoaded = permissionsLoaded;
  const wrapper = mount(AppBottomNav, {
    global: {
      plugins: [PrimeVue, pinia],
      stubs: {
        Drawer: DrawerStub,
        InputText: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<input data-testid="more-search" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        QuickCaptureSheet: true,
        LeadIntakeForm: true,
        MobileServerConfigDialog: true,
        OfficeAlertsToggle: true,
        RouterLink: RouterLinkStub,
      },
    },
  });
  await flushPromises();
  await wrapper.find('[data-testid="tab-more"]').trigger('click');
  return wrapper;
}

const favTargets = (wrapper) =>
  wrapper.find('[data-testid="more-section-favorites"]').exists()
    ? wrapper
      .find('[data-testid="more-section-favorites"]')
      .findAllComponents(RouterLinkStub)
      .map((l) => l.props('to'))
    : [];

describe('AppBottomNav — More-drawer favorites', () => {
  beforeEach(() => {
    localStorage.clear();
    _resetForTests();
    apiGet.mockReset().mockResolvedValue({ count: 0 });
    modules.value = MODULES;
    modulesLoaded.value = true;
    syncSpy.mockReset();
  });
  afterEach(() => {
    localStorage.clear();
  });

  it('no favorites: no section, and a hint says how to pin one', async () => {
    const wrapper = await openDrawer();
    expect(favTargets(wrapper)).toEqual([]);
    expect(wrapper.find('[data-testid="more-favorites-hint"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('starring pins the phone-shaped route at the top, in starring order, and persists', async () => {
    const wrapper = await openDrawer();
    await wrapper.find('[data-testid="more-star-sms"]').trigger('click');
    await wrapper.find('[data-testid="more-star-billing"]').trigger('click');
    expect(favTargets(wrapper)).toEqual(['/mobile/sms', '/mobile/billing']);
    expect(wrapper.find('[data-testid="more-star-sms"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.find('[data-testid="more-favorites-hint"]').exists()).toBe(false);
    expect(JSON.parse(localStorage.getItem(FAVORITES_KEY))).toEqual(['/mobile/sms', '/mobile/billing']);
    wrapper.unmount();

    _resetForTests(); // a fresh app start reads the same storage
    const again = await openDrawer();
    expect(favTargets(again)).toEqual(['/mobile/sms', '/mobile/billing']);
    again.unmount();
  });

  it('unstarring from the Favorites section removes it', async () => {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(['/reports']));
    _resetForTests();
    const wrapper = await openDrawer();
    expect(favTargets(wrapper)).toEqual(['/reports']);
    await wrapper.find('[data-testid="more-fav-star-reports"]').trigger('click');
    expect(favTargets(wrapper)).toEqual([]);
    expect(JSON.parse(localStorage.getItem(FAVORITES_KEY))).toEqual([]);
    wrapper.unmount();
  });

  it('a favorite for a module this user cannot see is not shown', async () => {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(['/payroll', '/reports']));
    _resetForTests();
    const wrapper = await openDrawer();
    expect(favTargets(wrapper)).toEqual(['/reports']);
    wrapper.unmount();
  });

  it('filtering hides the Favorites section', async () => {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(['/reports']));
    _resetForTests();
    const wrapper = await openDrawer();
    await wrapper.find('[data-testid="more-search"]').setValue('bill');
    expect(wrapper.find('[data-testid="more-section-favorites"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('launcher shortcuts wait for modules and permissions, so a cold start does not shrink them', async () => {
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(['/reports']));
    _resetForTests();
    modulesLoaded.value = false;
    const wrapper = await openDrawer({ role: 'dispatcher', permissionsLoaded: false });
    expect(syncSpy).not.toHaveBeenCalled();

    modulesLoaded.value = true;
    await flushPromises();
    expect(syncSpy).not.toHaveBeenCalled();

    const auth = useAuthStore();
    auth.permissions = new Set(['nav.office']);
    auth.permissionsLoaded = true;
    await flushPromises();
    expect(syncSpy).toHaveBeenCalledTimes(1);
    expect(syncSpy.mock.calls[0][0].map((m) => m.to)).toEqual(['/reports']);
    wrapper.unmount();
  });
});
