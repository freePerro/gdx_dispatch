/**
 * QuickbooksView with the QuickBooks module off. Uses the REAL useApi with
 * fetch stubbed, because the claim under test is about what the API layer
 * does on a 403 (its "Permission denied" toast), which a mocked
 * useApiWithToast makes true by construction.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {} }),
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
}));

import QuickbooksView from '../QuickbooksView.vue';

function mkResponse(body, status) {
  return {
    ok: status < 400, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const PANELS = [
  'qb-schedule-select-wrap', 'qb-accounts-panel', 'qb-banking-panel', 'qb-reconciliation-panel',
];

function mountView() {
  return mount(QuickbooksView, {
    global: {
      plugins: [PrimeVue],
      // The child panels fetch on their own; stubbed, their presence is what
      // says whether they would have fetched.
      stubs: {
        QbAccountsPanel: true,
        QbBankingPanel: true,
        QbSyncScheduleSelect: true,
        QbReconciliationPanel: true,
      },
    },
  });
}

describe('QuickbooksView, module off (real useApi)', () => {
  let fetchMock;

  beforeEach(() => {
    setActivePinia(createPinia());
    toastAdd.mockReset();
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    sessionStorage.setItem('gdx_access_token', 'test-jwt');
  });

  afterEach(() => {
    vi.restoreAllMocks();
    sessionStorage.clear();
  });

  it('a 403 disables Connect and Sync, says the module is off, and raises no toast', async () => {
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes('/api/qb/')) {
        return mkResponse({ detail: "Module 'quickbooks' is not enabled" }, 403);
      }
      return mkResponse({}, 200);
    });
    const wrapper = mountView();
    await flushPromises();

    const qbCalls = fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes('/api/qb/'));
    expect(qbCalls.some((u) => u.includes('/api/qb/dashboard'))).toBe(true);
    expect(qbCalls.some((u) => u.includes('/api/qb/events'))).toBe(true);
    expect(wrapper.find('[data-testid="quickbooks-connect-btn"]').element.disabled).toBe(true);
    expect(wrapper.find('[data-testid="quickbooks-sync-btn"]').element.disabled).toBe(true);
    const note = wrapper.find('[data-testid="qb-access-denied"]');
    expect(note.text()).toContain('module is turned off');
    expect(note.text()).toContain('Settings → Modules');
    expect(note.text()).toContain("Module 'quickbooks' is not enabled");
    expect(toastAdd).not.toHaveBeenCalled();
    // Each panel would 403 and toast on mount; none is mounted.
    for (const id of PANELS) expect(wrapper.find(`[data-testid="${id}"]`).exists()).toBe(false);
  });

  it('a password-change 403 is not reported as the module being off', async () => {
    fetchMock.mockImplementation(async () =>
      mkResponse({ detail: 'password_change_required' }, 403));
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="qb-access-denied"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="quickbooks-connect-btn"]').element.disabled).toBe(false);
  });

  it('a 5xx keeps the actions live and reports the failure once', async () => {
    fetchMock.mockImplementation(async (url) => {
      if (String(url).includes('/api/qb/')) return mkResponse({ detail: 'boom' }, 500);
      return mkResponse({}, 200);
    });
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="qb-access-denied"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="quickbooks-connect-btn"]').element.disabled).toBe(false);
    expect(toastAdd).toHaveBeenCalledTimes(1);
    expect(toastAdd.mock.calls[0][0].summary).toBe('QuickBooks load failed');
    // Not a refusal: the panels still mount and try their own loads.
    for (const id of PANELS) expect(wrapper.find(`[data-testid="${id}"]`).exists()).toBe(true);
  });
});
