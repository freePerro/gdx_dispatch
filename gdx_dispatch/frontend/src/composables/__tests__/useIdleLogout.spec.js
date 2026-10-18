import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { defineComponent, h } from 'vue';
import { flushPromises, mount } from '@vue/test-utils';
import { getIdleTimeoutMin, setIdleTimeoutMin, useIdleLogout } from '../useIdleLogout';
import { createAppRouter } from '../../router/index';
import { useAuthStore } from '../../stores/auth';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

describe('useIdleLogout storage', () => {
  beforeEach(() => localStorage.clear());

  it('returns 0 when unset, blank, non-numeric, or non-positive', () => {
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', '');
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', 'abc');
    expect(getIdleTimeoutMin()).toBe(0);
    localStorage.setItem('gdx_idle_timeout_min', '-5');
    expect(getIdleTimeoutMin()).toBe(0);
  });

  it('round-trips a positive value, floored and never negative', () => {
    setIdleTimeoutMin(15);
    expect(getIdleTimeoutMin()).toBe(15);
    setIdleTimeoutMin(15.9);
    expect(getIdleTimeoutMin()).toBe(15);
    setIdleTimeoutMin(-3);
    expect(getIdleTimeoutMin()).toBe(0);
  });

  it('dispatches the config-changed event so a mounted watcher re-arms', () => {
    const spy = vi.fn();
    window.addEventListener('gdx:idle-config-changed', spy);
    setIdleTimeoutMin(10);
    expect(spy).toHaveBeenCalledOnce();
    window.removeEventListener('gdx:idle-config-changed', spy);
  });
});

/**
 * The idle timer's logout is not chosen, so the router's gdx:auth-logout
 * listener sends the person to log in with a way back. Before GDXA-1492
 * onTimeout also pushed a bare '/login', which superseded that navigation
 * and dropped ?redirect. Real router (and its listener), real store.
 */
describe('useIdleLogout timeout', () => {
  let auth;

  function jsonResponse(status, body) {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => null },
      json: async () => body,
      text: async () => JSON.stringify(body),
    };
  }

  function mountIdle(router) {
    return mount(defineComponent({
      setup() { useIdleLogout(); return () => h('div'); },
    }), { global: { plugins: [router] } });
  }

  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    setActivePinia(createPinia());
    auth = useAuthStore();
    auth.accessToken = 'opaque-test-token';
    auth.user = { name: 'Office', role: 'admin' };
    toastAdd.mockClear();
    vi.stubGlobal('fetch', vi.fn(async (url) => {
      const u = String(url);
      if (u.startsWith('/api/session-policy')) return jsonResponse(200, { idle_timeout_minutes: 1 });
      if (u.startsWith('/api/users/me/permissions')) return jsonResponse(200, { permissions: ['*'] });
      return jsonResponse(200, {});
    }));
    setIdleTimeoutMin(1);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('lands on /login with a way back to the page and reason=expired', async () => {
    const router = createAppRouter();
    await router.push('/dashboard?tab=week');
    expect(router.currentRoute.value.path).toBe('/dashboard');

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const wrapper = mountIdle(router);
    await flushPromises();

    vi.advanceTimersByTime(60 * 1000);
    await flushPromises();

    expect(auth.isAuthenticated).toBe(false);
    expect(router.currentRoute.value.path).toBe('/login');
    expect(router.currentRoute.value.query).toMatchObject({
      redirect: '/dashboard?tab=week',
      reason: 'expired',
    });
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Signed out' }));
    wrapper.unmount();
  });

  it('does not send a person on a public page to /login', async () => {
    const router = createAppRouter();
    await router.push('/forgot-password');
    expect(router.currentRoute.value.meta.public).toBeTruthy();

    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] });
    const wrapper = mountIdle(router);
    await flushPromises();

    vi.advanceTimersByTime(60 * 1000);
    await flushPromises();

    expect(auth.isAuthenticated).toBe(false);
    expect(router.currentRoute.value.path).toBe('/forgot-password');
    wrapper.unmount();
  });
});
