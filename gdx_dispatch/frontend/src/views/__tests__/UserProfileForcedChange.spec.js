/**
 * Where a forced password change lands (2026-10-19 install walk).
 *
 * The seeded first admin must change the one-time password on /profile.
 * The change used to succeed with an inline note and leave them there, with
 * the dashboard tour then starting over an empty app. Now a held session goes
 * on: an owner or admin of a company that still needs setup to the wizard, everyone else to the dashboard. A change made by choice stays put.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const { apiGet, apiPost, push, toastAdd, authState } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  push: vi.fn(),
  toastAdd: vi.fn(),
  authState: { mustChangePassword: false, user: { role: 'owner' } },
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: vi.fn() }),
}));
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

// eslint-disable-next-line import/first
import UserProfileView from '../UserProfileView.vue';

let onboarding;

beforeEach(() => {
  vi.clearAllMocks();
  onboarding = { completed_at: null, setup_needed: true };
  apiGet.mockImplementation(async (url) => {
    if (url === '/api/users/me') return { email: 'a@example.com', name: 'A' };
    if (url === '/api/onboarding/state') return onboarding;
    throw new Error(`unexpected GET ${url}`);
  });
  // The real client clears the hold when the change succeeds.
  apiPost.mockImplementation(async () => {
    authState.mustChangePassword = false;
    return null;
  });
});

async function changePassword({ held, role }) {
  authState.mustChangePassword = held;
  authState.user = { role };
  const w = mount(UserProfileView, { global: { plugins: [PrimeVue] } });
  await flushPromises();
  for (const [id, value] of [['pw-current', 'old-pass-1'], ['pw-new', 'new-pass-12'], ['pw-confirm', 'new-pass-12']]) {
    await w.find(`#${id} input, [data-testid="${id}"] input`).setValue(value);
  }
  await w.find('[data-testid="pw-submit"]').trigger('click');
  await flushPromises();
  return w;
}

describe('after a forced password change', () => {
  it('sends an owner whose setup is unfinished to the wizard, with a toast', async () => {
    await changePassword({ held: true, role: 'owner' });
    expect(apiPost).toHaveBeenCalledWith('/api/users/me/change-password', {
      current_password: 'old-pass-1',
      new_password: 'new-pass-12',
    });
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
    expect(push).toHaveBeenCalledWith('/onboarding');
  });

  it('sends an admin of a finished setup to the dashboard', async () => {
    onboarding = { completed_at: '2026-10-19T12:00:00', setup_needed: false };
    await changePassword({ held: true, role: 'admin' });
    expect(push).toHaveBeenCalledWith('/dashboard');
  });

  it('sends an owner of a company running from before the wizard to the dashboard', async () => {
    // Production on 2026-10-19: no onboarding row, so no completed_at, but
    // 434 customers. The server says setup is not needed.
    onboarding = { completed_at: null, setup_needed: false };
    await changePassword({ held: true, role: 'owner' });
    expect(push).toHaveBeenCalledWith('/dashboard');
  });

  it('sends other roles to the dashboard without asking about setup', async () => {
    await changePassword({ held: true, role: 'tech' });
    expect(apiGet).not.toHaveBeenCalledWith('/api/onboarding/state');
    expect(push).toHaveBeenCalledWith('/dashboard');
  });

  it('goes to the dashboard when the setup state cannot be read', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url === '/api/users/me') return {};
      throw new Error('500');
    });
    await changePassword({ held: true, role: 'owner' });
    expect(push).toHaveBeenCalledWith('/dashboard');
  });

  it('stays on the profile when the change was by choice', async () => {
    await changePassword({ held: false, role: 'owner' });
    expect(apiPost).toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(toastAdd).not.toHaveBeenCalled();
  });
});
