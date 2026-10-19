/**
 * The dashboard tour waits for the setup wizard (2026-10-19 install walk).
 *
 * A new owner landed on the dashboard and the role tour started over an
 * empty app before the company was set up. An owner or admin now gets the
 * tour only once the server says setup is not needed; other roles' tours do
 * not wait on the owner.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

const { apiGet, autoLaunchForUser, authState } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  autoLaunchForUser: vi.fn(),
  authState: { role: 'owner', user: { role: 'owner' }, mustChangePassword: false },
}));

vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));
vi.mock('../../composables/useApi', () => ({ useApi: () => ({ get: apiGet }) }));
vi.mock('../../composables/useTour', () => ({ useTour: () => ({ autoLaunchForUser }) }));
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/dashboard', matched: [] }) }));

// eslint-disable-next-line import/first
import AppLayout from '../AppLayout.vue';

const Stub = { template: '<div />' };
const stubs = {
  AppSidebar: Stub,
  AppTopbar: Stub,
  AppBottomNav: Stub,
  HelpDrawer: Stub,
  ConfirmDialog: Stub,
  NotificationsDrawer: Stub,
  WhatsNewBanner: Stub,
  BugReportButton: Stub,
  Drawer: { props: ['visible'], template: '<div><slot /></div>' },
  Breadcrumb: Stub,
};

async function mountAndWait(role, state) {
  authState.role = role;
  authState.user = { role };
  apiGet.mockImplementation(async (url) => {
    if (url !== '/api/onboarding/state') throw new Error(`unexpected GET ${url}`);
    if (state instanceof Error) throw state;
    return state;
  });
  mount(AppLayout, { global: { stubs } });
  await vi.advanceTimersByTimeAsync(1300);
  await flushPromises();
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
});
afterEach(() => vi.useRealTimers());

describe('dashboard tour auto-launch', () => {
  it('waits while an owner has not finished setup', async () => {
    await mountAndWait('owner', { completed_at: null, setup_needed: true });
    expect(autoLaunchForUser).not.toHaveBeenCalled();
  });

  it('starts for an admin once setup is finished', async () => {
    await mountAndWait('admin', { completed_at: '2026-10-19T12:00:00', setup_needed: false });
    expect(autoLaunchForUser).toHaveBeenCalledWith({ role: 'admin' });
  });

  it('starts for an owner of a company running from before the wizard', async () => {
    // No onboarding row (so no completed_at), but the company has customers.
    await mountAndWait('owner', { completed_at: null, setup_needed: false });
    expect(autoLaunchForUser).toHaveBeenCalledWith({ role: 'owner' });
  });

  it('waits when the setup state cannot be read', async () => {
    await mountAndWait('owner', new Error('500'));
    expect(autoLaunchForUser).not.toHaveBeenCalled();
  });

  it('starts for a tech without asking about setup', async () => {
    await mountAndWait('tech', { completed_at: null, setup_needed: true });
    expect(apiGet).not.toHaveBeenCalled();
    expect(autoLaunchForUser).toHaveBeenCalledWith({ role: 'tech' });
  });
});
