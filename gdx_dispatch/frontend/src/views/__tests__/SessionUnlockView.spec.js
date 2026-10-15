/**
 * GDXA-1187: the unlock screen mounts mobile-tech's MobilePinUnlockDialog and
 * moves on once it clears the lock; "Forgot PIN" signs out to the login.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';

vi.mock('@capacitor/core', async (importOriginal) => {
  const real = await importOriginal();
  return { ...real, Capacitor: { ...real.Capacitor, isNativePlatform: () => false } };
});

// eslint-disable-next-line import/first
import SessionUnlockView from '../SessionUnlockView.vue';
// eslint-disable-next-line import/first
import { isQuickPinEnabled, isSessionLocked, lockSession, setQuickPin } from '../../lib/nativeAuth';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';

function makeRouter() {
  const Blank = { template: '<div />' };
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/unlock', component: SessionUnlockView },
      { path: '/login', component: Blank },
      { path: '/mobile', component: Blank },
    ],
  });
}

describe('SessionUnlockView', () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    setActivePinia(createPinia());
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) })));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function mountLocked() {
    await setQuickPin('1234');
    lockSession();
    const router = makeRouter();
    const replace = vi.spyOn(router, 'replace');
    await router.push('/unlock?redirect=/mobile');
    const wrapper = mount(SessionUnlockView, { global: { plugins: [router] } });
    await flushPromises();
    return { wrapper, replace };
  }

  it('shows the PIN dialog and, on the right PIN, unlocks and re-runs the guard', async () => {
    const { wrapper, replace } = await mountLocked();
    expect(wrapper.find('[data-test="mobile-pin-dialog"]').exists()).toBe(true);
    for (const d of '1234') await wrapper.find(`[data-test="key-${d}"]`).trigger('click');
    await flushPromises();
    expect(isSessionLocked()).toBe(false);
    expect(replace).toHaveBeenCalledWith(expect.objectContaining({ path: '/unlock', force: true }));
  });

  it('Forgot PIN turns the PIN off, signs out and goes to the password login', async () => {
    const { wrapper, replace } = await mountLocked();
    const auth = useAuthStore();
    const logout = vi.spyOn(auth, 'logout');
    await wrapper.find('[data-test="btn-forgot-pin"]').trigger('click');
    await flushPromises();
    expect(logout).toHaveBeenCalledWith({ chosen: true });
    expect(isSessionLocked()).toBe(false);
    expect(isQuickPinEnabled()).toBe(false);
    expect(replace).toHaveBeenCalledWith('/login');
  });
});
