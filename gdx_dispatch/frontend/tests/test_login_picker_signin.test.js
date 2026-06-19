import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount, flushPromises } from '@vue/test-utils';
import LoginPicker from '../src/views/LoginPicker.vue';
import { useAuthStore } from '../src/stores/auth';

/*
 * S121 rewrote LoginPicker.vue into an inline-credentials form
 * (Doug 2026-05-10: "this is not a page you can put credentials in"). The
 * old SS-13 Slice E "Sign In link bounces to /login" contract is dead.
 * These tests pin the new contract:
 *   - Unauthenticated arrival renders an inline form (email + password)
 *     with a Sign Up escape hatch.
 *   - Multi-tenant authenticated arrival renders the workspace list.
 *   - Zero-tenant authenticated arrival renders the /signup empty state.
 */

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('LoginPicker — inline credentials form (S121 contract)', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it('renders inline email + password form when not authenticated', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const wrapper = mount(LoginPicker);
    await flushPromises();

    expect(wrapper.find('[data-testid="login-form"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="login-email"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="login-password"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="login-submit"]').exists()).toBe(true);
    // Sign-up escape hatch always visible on the unauth branch.
    expect(wrapper.find('[data-testid="login-picker-signup-link"]').exists()).toBe(true);
    // No tenant list when unauthenticated.
    expect(wrapper.find('[data-testid="login-picker-list"]').exists()).toBe(false);
    // fetch() is NOT called when unauthenticated — no /api/me/tenants probe
    // (the old design fired this and used 401/403 as the "show sign-in link"
    // signal; the new design uses auth.isAuthenticated boolean instead).
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('renders inline form (not a link) when previously-stored token is expired', async () => {
    // Pre-seed an expired token. Auth store treats sessionStorage as the
    // only source of truth for isAuthenticated, so even with a token the
    // page should still render the form path if the auth store hasn't
    // restored the session (test isolation).
    sessionStorage.setItem('gdx_access_token', 'expired-token');
    const auth = useAuthStore();
    // Force unauthenticated state (token-write didn't go through auth.login).
    auth.accessToken = null;

    const wrapper = mount(LoginPicker);
    await flushPromises();

    expect(wrapper.find('[data-testid="login-form"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="login-password"]').exists()).toBe(true);
  });

  it('renders the tenant list when authenticated and API returns multiple tenants', async () => {
    const auth = useAuthStore();
    auth.accessToken = 'good-token';
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse(200, [
          { slug: 'acme', name: 'Acme', role: 'owner' },
          { slug: 'beta', name: 'Beta', role: 'admin' },
        ]),
      ),
    );

    const wrapper = mount(LoginPicker);
    await flushPromises();

    expect(wrapper.find('[data-testid="login-picker-list"]').exists()).toBe(true);
    // No inline form on the authenticated branch.
    expect(wrapper.find('[data-testid="login-form"]').exists()).toBe(false);
  });

  it('renders empty-state link to /signup when authenticated and zero tenants', async () => {
    const auth = useAuthStore();
    auth.accessToken = 'good-token';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, [])));

    const wrapper = mount(LoginPicker);
    await flushPromises();

    const empty = wrapper.find('[data-testid="login-picker-empty"]');
    expect(empty.exists()).toBe(true);
    expect(empty.html()).toContain('/signup');
    expect(wrapper.find('[data-testid="login-form"]').exists()).toBe(false);
  });
});
