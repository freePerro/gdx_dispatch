/**
 * LoginView — MH-0 recovery swap regression suite.
 *
 * Locks: when the login backend returns "Unknown tenant", the credential
 * form is HIDDEN and the PlatformRecovery panel is rendered. Also locks
 * the explicit "Wrong workspace?" escape link.
 *
 * Audit reference: ai-queue/brainstorm/mobile_ux_audit_2026-05-19.md P0 #1.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

// Pinia stores need module-level vi.mock — declare BEFORE LoginView import.
const loginMock = vi.fn();
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    login: loginMock,
    tenantChoices: [],
    pendingPlatformCreds: null,
  }),
}));
vi.mock('../../stores/theme', () => ({
  useThemeStore: () => ({ loadBranding: vi.fn() }),
}));
vi.mock('../../lib/auth-urls', () => ({
  getPostLoginRedirect: () => '/dashboard',
}));

const routerPush = vi.fn();
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
  useRoute: () => ({ query: {} }),
}));

import LoginView from '../LoginView.vue';

const RouterLinkStub = {
  props: ['to'],
  template: '<a :href="typeof to === \'string\' ? to : \'#\'" :data-to="to"><slot /></a>',
};

function mountLogin() {
  return mount(LoginView, {
    global: { stubs: { 'router-link': RouterLinkStub } },
  });
}

describe('LoginView — MH-0 recovery swap', () => {
  beforeEach(() => {
    loginMock.mockReset();
    routerPush.mockReset();
  });

  it('renders the credential form on mount (no recovery panel)', () => {
    const w = mountLogin();
    expect(w.find('[data-testid="login-form"]').exists()).toBe(true);
    expect(w.find('[data-testid="platform-recovery"]').exists()).toBe(false);
  });

  it('swaps to the recovery panel when login throws "Unknown tenant"', async () => {
    loginMock.mockRejectedValueOnce(new Error('Unknown tenant'));
    const w = mountLogin();
    await w.get('[data-testid="login-email"]').setValue('a@b.c');
    await w.get('[data-testid="login-password"]').setValue('pw');
    await w.get('[data-testid="login-form"]').trigger('submit.prevent');
    await flushPromises();
    expect(w.find('[data-testid="platform-recovery"]').exists()).toBe(true);
    // Form is gone — must not leave a dead-end credential surface behind.
    expect(w.find('[data-testid="login-form"]').exists()).toBe(false);
    // And we don't double-display "Unknown tenant" as an inline error.
    expect(w.find('[data-testid="login-error"]').exists()).toBe(false);
  });

  it('also swaps on the related "no tenant context" 400 message', async () => {
    loginMock.mockRejectedValueOnce(
      new Error('No tenant context — call this endpoint via your tenant subdomain'),
    );
    const w = mountLogin();
    await w.get('[data-testid="login-email"]').setValue('a@b.c');
    await w.get('[data-testid="login-password"]').setValue('pw');
    await w.get('[data-testid="login-form"]').trigger('submit.prevent');
    await flushPromises();
    expect(w.find('[data-testid="platform-recovery"]').exists()).toBe(true);
  });

  it('does NOT swap to recovery for ordinary credential errors', async () => {
    loginMock.mockRejectedValueOnce(new Error('Invalid email or password'));
    const w = mountLogin();
    await w.get('[data-testid="login-email"]').setValue('a@b.c');
    await w.get('[data-testid="login-password"]').setValue('pw');
    await w.get('[data-testid="login-form"]').trigger('submit.prevent');
    await flushPromises();
    expect(w.find('[data-testid="platform-recovery"]').exists()).toBe(false);
    expect(w.find('[data-testid="login-form"]').exists()).toBe(true);
    expect(w.get('[data-testid="login-error"]').text()).toContain('Invalid email or password');
  });

  it('explicit "Wrong workspace?" link swaps to recovery without a failed login', async () => {
    const w = mountLogin();
    const escape = w.get('[data-testid="wrong-workspace"]');
    await escape.trigger('click');
    expect(w.find('[data-testid="platform-recovery"]').exists()).toBe(true);
  });
});
