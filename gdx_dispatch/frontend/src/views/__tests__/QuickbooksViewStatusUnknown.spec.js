/**
 * QuickbooksView — a failed status load reads "Status unknown", never
 * "Disconnected", and never keeps the previous snapshot's banners beside it.
 * Mounts the view itself so the setter in loadQuickbooks is what is tested;
 * QbOverviewPanel renders for real.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: vi.fn() }),
}));
const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
let routeQuery = {};
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: routeQuery }),
  useRouter: () => ({ replace: vi.fn() }),
}));
let authState = { isAdmin: true };
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));

import QuickbooksView from '../QuickbooksView.vue';

const STALE = {
  connected: true,
  realm_id: 'REALM-1',
  last_error: 'OAuth token expired',
  error_count: 3,
  needs_reconnect: true,
  auth_state: 'needs_reconnect',
  entity_counts: { customer: 4 },
  delete_sync_enabled: true,
};

function mountView() {
  return mount(QuickbooksView, {
    global: {
      plugins: [PrimeVue],
      stubs: {
        QbAccountsPanel: true,
        QbBankingPanel: true,
        QbSyncScheduleSelect: true,
        QbReconciliationPanel: true,
      },
    },
  });
}

// Routed by URL, because /dashboard falls back to /status inside the view.
function serve({ statusOk }) {
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/qb/events')) return { events: [] };
    if (!statusOk) throw new Error('500 Internal Server Error');
    return STALE;
  });
}

function httpError(status, message, code) {
  const err = new Error(message);
  err.status = status;
  err.code = code;
  return err;
}

const EVENT = { timestamp: '2026-10-01T12:00:00Z', type: 'customers', count: 7, status: 'success' };

describe('QuickbooksView status load failure', () => {
  beforeEach(() => {
    apiGet.mockReset();
    toastAdd.mockReset();
    routeQuery = {};
    authState = { isAdmin: true };
  });

  it('reads Status unknown on a first load that fails', async () => {
    serve({ statusOk: false });
    const wrapper = mountView();
    await flushPromises();
    const tag = wrapper.find('[data-testid="qb-overview-connection"]');
    expect(tag.text()).toBe('Status unknown');
    expect(wrapper.text()).not.toContain('Disconnected');
  });

  it('drops the previous snapshot when a reload fails', async () => {
    serve({ statusOk: true });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.find('[data-testid="qb-overview-reconnect-banner"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="qb-overview-realm"]').exists()).toBe(true);

    serve({ statusOk: false });
    await wrapper.vm.loadQuickbooks();
    await flushPromises();

    expect(wrapper.find('[data-testid="qb-overview-connection"]').text()).toBe('Status unknown');
    expect(wrapper.find('[data-testid="qb-overview-reconnect-banner"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="qb-overview-error-banner"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="qb-overview-realm"]').exists()).toBe(false);
    // The placeholder must not speak either: "Disabled" for delete sync and
    // "No entities synced yet" are claims nobody loaded.
    expect(wrapper.find('[data-testid="qb-overview-delete-sync"]').text()).toBe('Unknown');
    expect(wrapper.find('[data-testid="qb-overview-last-sync"]').text()).toBe('Unknown');
    expect(wrapper.find('[data-testid="qb-overview-counts-unknown"]').exists()).toBe(true);
    expect(wrapper.text()).not.toContain('No entities synced yet');
  });

  it('keeps the events that loaded when both status calls fail', async () => {
    routeQuery = { tab: 'log' };
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/qb/events')) return { events: [EVENT] };
      throw new Error('500 Internal Server Error');
    });
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="qb-overview-connection"]').text()).toBe('Status unknown');
    const table = wrapper.find('[data-testid="quickbooks-table"]');
    expect(table.text()).toContain('customers');
    expect(table.text()).not.toContain('No sync events yet');
  });

  it('clears the log and says it is unavailable when an events reload fails', async () => {
    routeQuery = { tab: 'log' };
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/qb/events')) return { events: [EVENT] };
      return STALE;
    });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.find('[data-testid="quickbooks-table"]').text()).toContain('customers');

    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/qb/events')) throw new Error('502 Bad Gateway');
      return STALE;
    });
    await wrapper.vm.loadQuickbooks();
    await flushPromises();

    const table = wrapper.find('[data-testid="quickbooks-table"]');
    expect(table.text()).not.toContain('customers');
    expect(table.find('[data-testid="qb-events-unavailable"]').text()).toContain('502 Bad Gateway');
    expect(table.text()).not.toContain('No sync events yet');
  });

  it('a 5xx leaves Connect and Sync live', async () => {
    serve({ statusOk: false });
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="qb-access-denied"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="quickbooks-connect-btn"]').element.disabled).toBe(false);
    expect(wrapper.find('[data-testid="quickbooks-sync-btn"]').element.disabled).toBe(false);
    // The view's own "QuickBooks load failed"; the API layer is mocked here.
    expect(toastAdd).toHaveBeenCalledTimes(1);
  });

  it('offers Disconnect only to admin/owner, as /disconnect requires', async () => {
    serve({ statusOk: true });
    authState = { isAdmin: false };
    const office = mountView();
    await flushPromises();
    expect(office.find('[data-testid="quickbooks-disconnect-btn"]').exists()).toBe(false);
    expect(office.find('[data-testid="quickbooks-connect-btn"]').exists()).toBe(true);

    authState = { isAdmin: true };
    const admin = mountView();
    await flushPromises();
    expect(admin.find('[data-testid="quickbooks-disconnect-btn"]').exists()).toBe(true);
  });
});
