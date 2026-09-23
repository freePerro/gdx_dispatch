/**
 * AppBottomNav — pin the More-drawer height override.
 *
 * 2026-05-10 (Doug): "when you tap on more you only see a little bit of
 * the screen." Root cause: PrimeVue Drawer position="bottom" defaults to
 * `height: 10rem` (~160px) per @primeuix/styles/drawer. On a phone that's
 * ~20% of the viewport. AppBottomNav now ships an explicit override that
 * sizes the drawer to leave the bottom-nav visible and a little safety
 * margin.
 *
 * If a future PrimeVue upgrade changes the selector or someone removes
 * the override, this test fails — and Doug's complaint resurfaces.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'AppBottomNav.vue'),
  'utf8',
);

describe('AppBottomNav More-drawer styles', () => {
  // PrimeVue's `class="more-drawer"` prop is merged into ptmi('root'),
  // which lands on the `.p-drawer` panel itself (NOT on a parent of it).
  // So the override has to target `.more-drawer.p-drawer` (concatenated
  // class selector), not `.more-drawer .p-drawer` (descendant — wouldn't
  // match anything).
  it('targets .more-drawer.p-drawer with a height override', () => {
    expect(SRC).toMatch(
      /\.more-drawer\.p-drawer\s*\{[^}]*height\s*:/,
    );
  });

  it('sized to leave the bottom nav visible (uses --bottom-nav-height)', () => {
    expect(SRC).toMatch(/--bottom-nav-height/);
  });

  // The drawer is teleported to <body>, so a scoped Vue rule with
  // [data-v-hash] never matches the rendered DOM. The height override
  // has to live in a NON-scoped <style> block so the compiled selector
  // is global. If a future refactor wraps it in `<style scoped>`, this
  // test fails — and Doug's complaint resurfaces.
  it('lives in a non-scoped <style> block (so it survives teleport)', () => {
    // Find the height rule, then walk back to the nearest <style ...> tag
    // and verify it does NOT carry the `scoped` attribute.
    const heightRuleIdx = SRC.search(
      /\.more-drawer\.p-drawer\s*\{[^}]*height\s*:/,
    );
    expect(heightRuleIdx).toBeGreaterThan(-1);

    const before = SRC.slice(0, heightRuleIdx);
    const lastStyleOpen = before.lastIndexOf('<style');
    expect(lastStyleOpen).toBeGreaterThan(-1);
    const styleTag = SRC.slice(
      lastStyleOpen,
      SRC.indexOf('>', lastStyleOpen) + 1,
    );
    expect(styleTag).not.toMatch(/\bscoped\b/);
  });
});

/**
 * Email tab for office roles (Doug, 2026-09-22: "Inbox should be email and
 * office roles should have an easy access point to it from mobile").
 *
 * /mobile/inbox existed since the first release but sat behind More → scroll.
 * These mount the real component: the tab is there for office roles and
 * routes to the mobile inbox, techs keep the drawer entry, the badge mirrors
 * the desktop sidebar pin, the poll starts and stops with the nav, and a
 * tenant with the inbox module off gets no dead tab.
 */
import { afterEach, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const { apiGet, routerPush, routerReplace, modules, emailModuleOn } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  routerPush: vi.fn().mockResolvedValue(undefined),
  routerReplace: vi.fn().mockResolvedValue(undefined),
  modules: { value: [] },
  emailModuleOn: { value: true },
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({
    allEnabledModules: modules,
    // Keyed on the BACKEND module key. `isEnabled('inbox')` would be a
    // mock-only truth: there is no such module, and the real composable
    // answers `true` for any unknown key (see useTenantModules.emailGate.spec).
    isEnabled: (key) => (key === 'email' ? emailModuleOn.value : true),
  }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/mobile/jobs', query: {}, fullPath: '/mobile/jobs' }),
  useRouter: () => ({
    push: routerPush,
    replace: routerReplace,
    resolve: (path) => ({ matched: [{ path }] }),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

// eslint-disable-next-line import/first
import AppBottomNav from '../AppBottomNav.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { useEmailUnreadStore } from '../../stores/emailUnread';

const DrawerStub = {
  props: ['visible'],
  template: '<div data-testid="more-drawer"><slot v-if="visible" /></div>',
};

const DRAWER_MODULES = [
  { key: 'inbox', label: 'Inbox', icon: 'pi pi-inbox', to: '/inbox', type: 'Operations' },
  { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing', type: 'Money' },
];

// A decodable token: the auth store derives `role` from the JWT payload, and
// a cold load has that claim before /auth/me has hydrated `user`.
const jwtFor = (role) => `h.${btoa(JSON.stringify({ role })).replace(/=+$/, '')}.s`;

function mountNav(role, { signedIn = true, cachedUser = true } = {}) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const auth = useAuthStore();
  auth.accessToken = signedIn ? jwtFor(role) : null;
  auth.user = signedIn && cachedUser ? { role } : null;
  const wrapper = mount(AppBottomNav, {
    global: {
      plugins: [PrimeVue, pinia],
      stubs: {
        Drawer: DrawerStub,
        InputText: true,
        QuickCaptureSheet: true,
        RouterLink: RouterLinkStub,
      },
    },
  });
  return wrapper;
}

const tabLabels = (wrapper) =>
  wrapper.findAll('nav.bottom-nav > .tab-btn .tab-label').map((n) => n.text());

const drawerTargets = (wrapper) =>
  wrapper.findAllComponents(RouterLinkStub).map((l) => l.props('to'));

describe('AppBottomNav — Email tab for office roles', () => {
  beforeEach(() => {
    // Only the interval is faked: flushPromises needs real macrotasks.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    apiGet.mockReset().mockResolvedValue({ count: 0 });
    routerPush.mockClear();
    modules.value = DRAWER_MODULES;
    emailModuleOn.value = true;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('office role: Email is a tab, just before More, and opens the mobile inbox', async () => {
    const wrapper = mountNav('owner');
    await flushPromises();
    expect(tabLabels(wrapper)).toEqual([
      'Jobs', 'Customers', 'Clock', 'Planner', 'Dispatch', 'Email', 'More',
    ]);
    await wrapper.find('[data-testid="tab-inbox"]').trigger('click');
    expect(routerPush).toHaveBeenCalledWith('/mobile/inbox');
    wrapper.unmount();
  });

  it('office role: Inbox leaves the More drawer now that it has a tab', async () => {
    const wrapper = mountNav('dispatcher');
    await flushPromises();
    await wrapper.find('[data-testid="tab-more"]').trigger('click');
    const targets = drawerTargets(wrapper);
    expect(targets).toContain('/mobile/billing'); // drawer rewrites to the mobile companion
    expect(targets).not.toContain('/mobile/inbox');
    expect(targets).not.toContain('/inbox');
    wrapper.unmount();
  });

  it('tech role: no Email tab, and the drawer still points Inbox at the mobile view', async () => {
    const wrapper = mountNav('tech');
    await flushPromises();
    expect(tabLabels(wrapper)).toEqual(['Today', 'Jobs', 'Customers', 'Clock', 'Photos', 'More']);
    await wrapper.find('[data-testid="tab-more"]').trigger('click');
    expect(drawerTargets(wrapper)).toContain('/mobile/inbox');
    // A tech never polls the mailbox for a badge they don't have.
    expect(apiGet).not.toHaveBeenCalledWith('/api/outlook/messages/unread-count');
    wrapper.unmount();
  });

  it('badge: mirrors the unread count, caps at 99+, hidden at zero', async () => {
    apiGet.mockResolvedValue({ count: 3 });
    const wrapper = mountNav('admin');
    await flushPromises();
    const badge = () => wrapper.find('[data-testid="email-unread-badge-mobile"]');
    expect(badge().exists()).toBe(true);
    expect(badge().text()).toBe('3');
    expect(wrapper.find('[data-testid="tab-inbox"]').attributes('aria-label')).toBe('Email, 3 unread');

    const store = useEmailUnreadStore();
    store.count = 120;
    await flushPromises();
    expect(badge().text()).toBe('99+');

    store.count = 0;
    await flushPromises();
    expect(badge().exists()).toBe(false);
    expect(wrapper.find('[data-testid="tab-inbox"]').attributes('aria-label')).toBeUndefined();
    wrapper.unmount();
  });

  it('polls the unread count while mounted and stops when the nav unmounts', async () => {
    const wrapper = mountNav('owner');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(apiGet).toHaveBeenCalledWith('/api/outlook/messages/unread-count');
    vi.advanceTimersByTime(60000);
    await flushPromises();
    expect(apiGet).toHaveBeenCalledTimes(2);
    wrapper.unmount();
    vi.advanceTimersByTime(180000);
    await flushPromises();
    expect(apiGet).toHaveBeenCalledTimes(2);
  });

  it('cold load as a tech with no cached user: tech strip from the first paint, no mailbox poll', async () => {
    // The 2026-08-28 router lesson, re-learned by the audit: `auth.user` is
    // null until /auth/me lands, but the JWT role claim is there at once. A
    // tech must not get the office strip, an Email tab and a mailbox GET for
    // that window.
    const wrapper = mountNav('technician', { cachedUser: false });
    await flushPromises();
    expect(tabLabels(wrapper)).toEqual(['Today', 'Jobs', 'Customers', 'Clock', 'Photos', 'More']);
    expect(apiGet).not.toHaveBeenCalledWith('/api/outlook/messages/unread-count');
    // Hydration changes nothing.
    useAuthStore().user = { role: 'technician' };
    await flushPromises();
    expect(tabLabels(wrapper)).not.toContain('Email');
    expect(apiGet).not.toHaveBeenCalledWith('/api/outlook/messages/unread-count');
    wrapper.unmount();
  });

  it('signed out: no Email tab and no poll (nothing to 401 against)', async () => {
    const wrapper = mountNav('owner', { signedIn: false });
    await flushPromises();
    expect(tabLabels(wrapper)).not.toContain('Email');
    expect(apiGet).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('email module reported off by the composable: no Email tab, no poll', async () => {
    // What this proves: the tab and the poll follow isEnabled('email'). What
    // it does not: the real composable answers `true` until
    // /api/settings/modules lands, so an email-off tenant paints the tab and
    // GETs the mailbox once (silent 403) before it self-corrects — recorded
    // in FOUND_NOT_FILED 2026-09-22, same pattern as AppTopbar's
    // `communications` watch.
    emailModuleOn.value = false;
    modules.value = DRAWER_MODULES.filter((m) => m.key !== 'inbox');
    const wrapper = mountNav('owner');
    await flushPromises();
    expect(tabLabels(wrapper)).toEqual(['Jobs', 'Customers', 'Clock', 'Planner', 'Dispatch', 'More']);
    expect(apiGet).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});
