/**
 * The shell while a password change is held (2026-10-19 install walk).
 *
 * The seeded admin's first stop is /profile, held there until the one-time
 * password changes. The shell around the form loaded branding and mounted the
 * sidebar, topbar and banner, whose loads the server refuses for a held
 * session: ten 403s, each a browser console error. Now branding waits for the
 * hold to clear, and the shell remounts when it does, so its loads run again
 * without a page reload.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';

vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/profile', matched: [] }) }));
vi.mock('../../composables/useTour', () => ({ useTour: () => ({ autoLaunchForUser: vi.fn() }) }));
vi.mock('../../composables/useApi', () => ({ useApi: () => ({ get: vi.fn(async () => ({ completed_at: 'x' })) }) }));

// eslint-disable-next-line import/first
import ThemeProvider from '../ThemeProvider.vue';
// eslint-disable-next-line import/first
import AppLayout from '../AppLayout.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { useThemeStore } from '../../stores/theme';

let auth;

beforeEach(() => {
  sessionStorage.clear();
  sessionStorage.setItem('gdx_access_token', 'opaque-test-token');
  setActivePinia(createPinia());
  auth = useAuthStore();
  auth.accessToken = 'opaque-test-token';
  auth.user = { role: 'owner' };
  auth.mustChangePassword = true;
  // The release reloads permissions; nothing here is about them.
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) })));
});

describe('branding during a password hold', () => {
  const brandingCalls = () => fetch.mock.calls.filter(([url]) => url === '/api/settings/branding').length;

  it('is not asked for while held, by login or the provider, and loads once released', async () => {
    // LoginView's finishLogin calls this straight after a held sign-in.
    await useThemeStore().loadBranding();
    mount(ThemeProvider);
    await flushPromises();
    expect(brandingCalls()).toBe(0);

    auth.setMustChangePassword(false);
    await flushPromises();
    expect(brandingCalls()).toBe(1);
  });

  it('loads at once for a session that is not held', async () => {
    auth.mustChangePassword = false;
    mount(ThemeProvider);
    await flushPromises();
    expect(brandingCalls()).toBe(1);
  });
});

describe('the shell when the hold clears', () => {
  it('remounts the sidebar, topbar and banner so their loads run again', async () => {
    const mounts = { AppSidebar: 0, AppTopbar: 0, WhatsNewBanner: 0 };
    const counting = (name) => ({ template: '<div />', mounted() { mounts[name] += 1; } });
    const Stub = { template: '<div />' };
    mount(AppLayout, {
      global: {
        stubs: {
          AppSidebar: counting('AppSidebar'),
          AppTopbar: counting('AppTopbar'),
          WhatsNewBanner: counting('WhatsNewBanner'),
          AppBottomNav: Stub,
          HelpDrawer: Stub,
          ConfirmDialog: Stub,
          NotificationsDrawer: Stub,
          BugReportButton: Stub,
          Message: Stub,
          Drawer: { template: '<div><slot /></div>' },
        },
      },
    });
    expect(mounts).toEqual({ AppSidebar: 1, AppTopbar: 1, WhatsNewBanner: 1 });

    auth.setMustChangePassword(false);
    await nextTick();
    expect(mounts).toEqual({ AppSidebar: 2, AppTopbar: 2, WhatsNewBanner: 2 });
  });
});
