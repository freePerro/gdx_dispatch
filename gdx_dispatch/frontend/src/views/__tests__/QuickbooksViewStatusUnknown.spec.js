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
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn() }),
}));

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

describe('QuickbooksView status load failure', () => {
  beforeEach(() => { apiGet.mockReset(); });

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
});
