/**
 * CatalogView tests — pins the S111 pricing-status banner contract.
 *
 * When the catalog items endpoint signals `pricing_status='not_configured'`
 * (tenant hasn't seeded margin tiers for the catalog's product class), the
 * view MUST render a PrimeVue Message warn banner with the actionable
 * message and a router-link to /margin-tiers. Without the banner, admins
 * see "—" in the Retail column with no signal what's wrong.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import CatalogView from '../CatalogView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
const apiDeleteMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGetMock,
    post: apiPostMock,
    patch: apiPatchMock,
    delete: apiDeleteMock,
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: () => Promise.resolve(true) }),
}));

const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: { template: '<div data-test="catalog-table"><slot /></div>' },
  Column: { template: '<div />' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /></div>" },
  Select: { template: '<select />' },
  SelectButton: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: `<div class="select-button"><button v-for="o in options" :key="o.value"
      :data-opt="o.value" @click="$emit('update:modelValue', o.value)">{{ o.label }}</button></div>`,
  },
  FileUpload: { template: '<div />' },
  InputNumber: { template: '<input type="number" />' },
  InputText: { template: '<input />' },
  Textarea: { template: '<textarea />' },
  ProgressSpinner: { template: '<div />' },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Message: {
    props: ['severity'],
    template: '<div class="p-message" :data-severity="severity"><slot /></div>',
  },
};

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/margin-tiers', name: 'margin-tiers', component: { template: '<div />' } },
    ],
  });
}

describe('CatalogView pricing-status banner', () => {
  let router;

  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    router = makeRouter();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders warn banner with link when pricing_status="not_configured"', async () => {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/catalogs') {
        return Promise.resolve([
          { id: 'cat-1', name: 'CHI Doors', product_class: 'door' },
        ]);
      }
      if (url.startsWith('/api/catalogs/cat-1/items')) {
        return Promise.resolve({
          items: [],
          total: 0,
          pricing_status: 'not_configured',
          pricing_status_message:
            "No retail margin tier configured for 'doors'. Set one in Settings → Margin Tiers; otherwise catalog retail prices stay blank.",
        });
      }
      return Promise.resolve([]);
    });

    const w = mount(CatalogView, { global: { stubs, plugins: [router] } });
    await flushPromises();

    const banner = w.find('[data-testid="catalog-pricing-warn"]');
    expect(banner.exists()).toBe(true);
    expect(banner.attributes('data-severity')).toBe('warn');
    expect(banner.text()).toContain('Retail prices unavailable');
    expect(banner.text()).toContain('No retail margin tier configured');
    // Action link points at /margin-tiers
    const link = banner.find('a');
    expect(link.exists()).toBe(true);
    expect(link.attributes('href')).toBe('/margin-tiers');
  });

  it('does NOT render banner when pricing_status="ok"', async () => {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/catalogs') {
        return Promise.resolve([
          { id: 'cat-1', name: 'CHI Doors', product_class: 'door' },
        ]);
      }
      if (url.startsWith('/api/catalogs/cat-1/items')) {
        return Promise.resolve({
          items: [],
          total: 0,
          pricing_status: 'ok',
          pricing_status_message: null,
        });
      }
      return Promise.resolve([]);
    });

    const w = mount(CatalogView, { global: { stubs, plugins: [router] } });
    await flushPromises();

    const banner = w.find('[data-testid="catalog-pricing-warn"]');
    expect(banner.exists()).toBe(false);
  });

  it('does NOT render banner when response is a bare list (legacy shape)', async () => {
    // Backend returning a plain array (no pricing_status field at all)
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/catalogs') {
        return Promise.resolve([
          { id: 'cat-1', name: 'Custom Parts', product_class: 'parts' },
        ]);
      }
      if (url.startsWith('/api/catalogs/cat-1/items')) {
        return Promise.resolve([{ id: 'item-1', sku: 'X', name: 'Y' }]);
      }
      return Promise.resolve([]);
    });

    const w = mount(CatalogView, { global: { stubs, plugins: [router] } });
    await flushPromises();
    expect(w.find('[data-testid="catalog-pricing-warn"]').exists()).toBe(false);
  });
});

describe('CatalogView active/inactive filter', () => {
  let router;

  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    router = makeRouter();
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/catalogs') {
        return Promise.resolve([
          { id: 'cat-on', name: 'Active Cat', product_class: 'parts', active: true },
          { id: 'cat-off', name: 'Old Cat', product_class: 'parts', active: false },
        ]);
      }
      if (url.startsWith('/api/catalogs/')) return Promise.resolve([]);
      return Promise.resolve([]);
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it('shows only active catalogs by default, and all when toggled', async () => {
    const w = mount(CatalogView, { global: { stubs, plugins: [router] } });
    await flushPromises();

    // Default = active: only the active catalog tab is rendered.
    expect(w.find('[data-testid="catalog-cat-on"]').exists()).toBe(true);
    expect(w.find('[data-testid="catalog-cat-off"]').exists()).toBe(false);

    // Switch the filter to "All" → both tabs render.
    await w.find('[data-testid="catalog-active-filter"] [data-opt="all"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="catalog-cat-on"]').exists()).toBe(true);
    expect(w.find('[data-testid="catalog-cat-off"]').exists()).toBe(true);

    // Switch to "Inactive" → only the inactive one.
    await w.find('[data-testid="catalog-active-filter"] [data-opt="inactive"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="catalog-cat-on"]').exists()).toBe(false);
    expect(w.find('[data-testid="catalog-cat-off"]').exists()).toBe(true);
  });
});
