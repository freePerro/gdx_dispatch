/**
 * CommandPalette — 2026-07-07 global-search wiring.
 *
 * The palette used to filter PAGE NAMES only while its placeholder promised
 * "Search jobs, customers, invoices..." — /api/search existed but nothing
 * called it. Pins:
 *   - typing ≥2 chars debounces (250ms) then queries /api/search once
 *   - data results render grouped (Customers/Jobs/Invoices/Estimates) with
 *     deep links, above page + quick-action matches
 *   - <2 chars never hits the API
 *   - a section whose module isn't visible to this user is hidden even if
 *     the backend returned rows for it
 *   - ArrowDown/Enter navigate the flattened selection; Enter opens the
 *     selected item via router.push
 *   - API failure degrades to page matches + an error line (no blank panel)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

// Plain `.value` holder (vi.hoisted can't touch imports): the component only
// reads `allEnabledModules.value`, and each test assigns it before mount, so
// no reactivity is needed.
const { apiGet, routerPush, modules } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  routerPush: vi.fn().mockResolvedValue(undefined),
  modules: { value: [] },
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));

vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({
    allEnabledModules: modules,
    isEnabled: () => true,
  }),
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}));

import CommandPalette from '../CommandPalette.vue';

const ALL_MODULES = [
  { key: 'jobs', label: 'Jobs', icon: 'pi pi-briefcase', to: '/jobs' },
  { key: 'customers', label: 'Customers', icon: 'pi pi-users', to: '/customers' },
  { key: 'billing', label: 'Billing', icon: 'pi pi-dollar', to: '/billing' },
  { key: 'estimates', label: 'Estimates', icon: 'pi pi-file-edit', to: '/estimates' },
];

const SEARCH_RESPONSE = {
  customers: [{ id: 'c1', name: 'Henning Lumber', phone: '555-123', email: null }],
  jobs: [{ id: 'j1', number: '1042', title: 'Spring repair', customer_name: 'Henning Lumber' }],
  invoices: [{ id: 'i1', number: 'INV-2201', total: 450, status: 'sent', customer_name: 'Henning Lumber' }],
  estimates: [],
};

const DialogStub = {
  props: ['visible'],
  template: '<div v-if="visible"><slot name="header" /><slot /></div>',
};

function mountPalette() {
  return mount(CommandPalette, {
    props: { modelValue: true },
    global: {
      plugins: [PrimeVue, createPinia()],
      stubs: { Dialog: DialogStub },
    },
  });
}

async function typeQuery(wrapper, text) {
  const input = wrapper.find('[data-testid="palette-input"]');
  await input.setValue(text);
  vi.advanceTimersByTime(300);
  await flushPromises();
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
  modules.value = ALL_MODULES;
  apiGet.mockReset().mockResolvedValue(SEARCH_RESPONSE);
  routerPush.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('CommandPalette data search', () => {
  it('debounces then queries /api/search and renders grouped data results', async () => {
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'henning');

    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(apiGet).toHaveBeenCalledWith('/api/search?q=henning');

    expect(wrapper.find('[data-testid="palette-group-customers"]').text()).toContain('Henning Lumber');
    expect(wrapper.find('[data-testid="palette-item-job-j1"]').text()).toContain('#1042 — Spring repair');
    expect(wrapper.find('[data-testid="palette-item-invoice-i1"]').text()).toContain('#INV-2201');
    // Empty estimates section renders no group.
    expect(wrapper.find('[data-testid="palette-group-estimates"]').exists()).toBe(false);
  });

  it('never hits the API below the 2-char minimum', async () => {
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'b');
    expect(apiGet).not.toHaveBeenCalled();
    // Page browsing still works with a 1-char term ("b" → Jobs, Billing).
    expect(wrapper.find('[data-testid="palette-group-pages"]').exists()).toBe(true);
  });

  it('hides a data section whose module is not visible to this user', async () => {
    modules.value = ALL_MODULES.filter((m) => m.key !== 'billing');
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'henning');

    expect(wrapper.find('[data-testid="palette-group-customers"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="palette-group-invoices"]').exists()).toBe(false);
  });

  it('degrades to page matches with an error line when the API fails', async () => {
    apiGet.mockRejectedValue(new Error('boom'));
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'billing');

    expect(wrapper.find('[data-testid="palette-error"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="palette-item-page-billing"]').exists()).toBe(true);
  });

  it('shows the empty state only when nothing matches at all', async () => {
    apiGet.mockResolvedValue({ customers: [], jobs: [], invoices: [], estimates: [] });
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'zzznothing');
    expect(wrapper.find('[data-testid="palette-empty"]').text()).toContain('zzznothing');
  });
});

describe('CommandPalette keyboard selection', () => {
  it('Enter opens the first result by default; ArrowDown moves the selection', async () => {
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'henning');

    const body = wrapper.find('.palette-body');
    await body.trigger('keydown', { key: 'Enter' });
    // First flattened item is the customer (data groups render first).
    expect(routerPush).toHaveBeenCalledWith('/customers/c1');

    routerPush.mockClear();
    await body.trigger('keydown', { key: 'ArrowDown' });
    await body.trigger('keydown', { key: 'Enter' });
    expect(routerPush).toHaveBeenCalledWith('/jobs/j1');
  });

  it('selection is visible on the active item', async () => {
    const wrapper = mountPalette();
    await typeQuery(wrapper, 'henning');
    expect(wrapper.find('[data-testid="palette-item-customer-c1"]').classes()).toContain('selected');
  });
});
