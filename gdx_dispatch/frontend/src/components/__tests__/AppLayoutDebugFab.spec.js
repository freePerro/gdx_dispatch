/**
 * MH-1 — debug 🐛 bug-report FAB visibility gate regression suite.
 *
 * Audit P1 #11: the FAB rendered unconditionally on every authenticated
 * mobile screen, overlapping content + nav. Gate added in AppLayout.vue:
 *   showDebugFab = (env VITE_SHOW_DEBUG_FAB === '1') || role === 'owner'
 *
 * Locks:
 *   - tech role + env unset → no FAB
 *   - admin role + env unset → no FAB (auditor28-class regression)
 *   - owner role + env unset → FAB visible
 *   - any role + VITE_SHOW_DEBUG_FAB=1 → FAB visible
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';

// Module mocks BEFORE AppLayout import.
const authState = { role: 'tech', user: { role: 'tech' } };
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => authState,
}));
vi.mock('../../composables/useTour', () => ({
  useTour: () => ({ autoLaunchForUser: vi.fn() }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/jobs', matched: [] }),
}));

// Heavy children stubbed to keep the test focused on the FAB v-if.
const Stub = { template: '<div />' };
const stubs = {
  AppSidebar: Stub,
  AppTopbar: Stub,
  AppBottomNav: Stub,
  HelpDrawer: Stub,
  ConfirmDialog: Stub,
  NotificationsDrawer: Stub,
  Drawer: { props: ['visible'], template: '<div><slot /></div>' },
  Breadcrumb: Stub,
  // The FAB itself uses a distinct data-testid we assert on.
  BugReportButton: {
    template: '<button data-testid="bug-report-btn">🐛</button>',
  },
};

import AppLayout from '../AppLayout.vue';

function mountLayout() {
  return mount(AppLayout, {
    global: { stubs },
  });
}

describe('AppLayout — MH-1 debug FAB gate', () => {
  // Alias so no statement begins with `import.meta` — CodeQL's JS extractor
  // misparses a statement-leading `import.meta...=` as an import declaration.
  const env = import.meta.env;
  const ORIGINAL_ENV = env.VITE_SHOW_DEBUG_FAB;

  beforeEach(() => {
    // Reset auth state per test.
    authState.role = 'tech';
    authState.user = { role: 'tech' };
  });

  afterEach(() => {
    env.VITE_SHOW_DEBUG_FAB = ORIGINAL_ENV;
  });

  it('hides the FAB for a field tech with env unset', () => {
    env.VITE_SHOW_DEBUG_FAB = '';
    authState.role = 'tech';
    authState.user = { role: 'tech' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(false);
  });

  it('hides the FAB for an admin (e.g. auditor28) with env unset', () => {
    env.VITE_SHOW_DEBUG_FAB = '';
    authState.role = 'admin';
    authState.user = { role: 'admin' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(false);
  });

  it('shows the FAB for the owner role with env unset', () => {
    env.VITE_SHOW_DEBUG_FAB = '';
    authState.role = 'owner';
    authState.user = { role: 'owner' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(true);
  });

  it('shows the FAB for ANY role when VITE_SHOW_DEBUG_FAB=1', () => {
    env.VITE_SHOW_DEBUG_FAB = '1';
    authState.role = 'tech';
    authState.user = { role: 'tech' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(true);
  });

  it('treats unrelated env values as off', () => {
    env.VITE_SHOW_DEBUG_FAB = 'true'; // not '1'
    authState.role = 'admin';
    authState.user = { role: 'admin' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(false);
  });

  it('case-insensitive role check (Owner / OWNER)', () => {
    env.VITE_SHOW_DEBUG_FAB = '';
    authState.role = 'OWNER';
    authState.user = { role: 'OWNER' };
    const w = mountLayout();
    expect(w.find('[data-testid="bug-report-btn"]').exists()).toBe(true);
  });
});
