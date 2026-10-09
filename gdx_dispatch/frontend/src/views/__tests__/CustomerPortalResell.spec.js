/**
 * Customer portal — contractor resale quotes (PR B): the My Branding and My
 * Quotes tabs and the "Resell this" button exist only for an account that
 * /portal/context marks reseller-eligible (contractor or wholesale).
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const stubs = {
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" data-testid="detail-dialog"><slot /><slot name="footer" /></div>',
  },
  Image: { template: '<img />' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  PortalBrandingTab: { template: '<div data-testid="branding-tab-stub" />' },
  PortalMyQuotesTab: { template: '<div data-testid="my-quotes-tab-stub" />' },
  PortalResellDialog: { props: ['visible', 'estimate'], template: '<div v-if="visible" data-testid="resell-dialog-stub">{{ estimate && estimate.id }}</div>' },
};

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-7', label: 'New door', status: 'accepted', total: 2600,
  hide_line_prices: false, line_category: 'off', images: [],
  lines: [{ id: 'e1', description: '16x7 door', quantity: 1, unit_price: 2600, line_total: 2600 }],
  totals: { subtotal: 2600, total: 2600 },
};

async function mountPortal(reseller, extraStubs = {}) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const u = String(url);
    if (u.endsWith('/portal/estimates/est-1')) return { ok: true, status: 200, json: async () => ESTIMATE };
    if (u.endsWith('/portal/estimates')) return { ok: true, status: 200, json: async () => [ESTIMATE] };
    if (u.includes('/portal/context')) return { ok: true, status: 200, json: async () => ({ company: { name: 'GDX' }, reseller }) };
    return { ok: true, status: 200, json: async () => [] };
  }));
  sessionStorage.setItem('gdx_portal_jwt', 'portal-token');
  const w = mount(CustomerPortalView, { global: { stubs: { ...stubs, ...extraStubs } } });
  await flushPromises();
  return w;
}

async function openEstimate(w) {
  await w.find('[data-testid="estimate-card"]').trigger('click');
  await flushPromises();
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('customer portal — resale quotes', () => {
  it('a retail account sees no reseller tabs and no Resell this', async () => {
    const w = await mountPortal({ eligible: false, disclaimer_accepted: false, set_up: false });
    expect(w.find('[data-testid="branding-tab-btn"]').exists()).toBe(false);
    expect(w.find('[data-testid="my-quotes-tab-btn"]').exists()).toBe(false);
    expect(w.find('[data-testid="branding-tab-stub"]').exists()).toBe(false);
    await openEstimate(w);
    expect(w.find('[data-testid="estimate-pdf-btn"]').exists()).toBe(true);
    expect(w.find('[data-testid="estimate-resell-btn"]').exists()).toBe(false);
  });

  it('an older server with no reseller flags shows none of it', async () => {
    const w = await mountPortal(undefined);
    expect(w.find('[data-testid="branding-tab-btn"]').exists()).toBe(false);
  });

  it('a contractor account gets both tabs, and Resell this opens the dialog for that estimate', async () => {
    const w = await mountPortal({ eligible: true, disclaimer_accepted: false, set_up: false });
    expect(w.get('[data-testid="branding-tab-btn"]').text()).toBe('My Branding');
    expect(w.get('[data-testid="my-quotes-tab-btn"]').text()).toBe('My Quotes');
    expect(w.find('[data-testid="branding-tab-stub"]').exists()).toBe(true);
    expect(w.find('[data-testid="my-quotes-tab-stub"]').exists()).toBe(true);
    await openEstimate(w);
    await w.get('[data-testid="estimate-resell-btn"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-testid="resell-dialog-stub"]').text()).toBe('est-1');
    expect(w.find('[data-testid="estimate-pdf-btn"]').exists()).toBe(false);
  });

  it('the page mounts one confirm dialog, with the tabs that confirm all mounted', async () => {
    // Every TabPanel is mounted at once; a ConfirmDialog per tab stacked two
    // copies of each confirm, and "Keep it" closed only the top one.
    const w = await mountPortal(
      { eligible: true, disclaimer_accepted: true, set_up: true },
      { PortalMyQuotesTab: false, ConfirmDialog: { template: '<div data-testid="confirm-dialog-mount" />' } },
    );
    expect(w.find('[data-testid="my-quotes-tab"]').exists()).toBe(true);
    expect(w.find('[data-testid="qr-file-input"]').exists()).toBe(true);
    expect(w.findAll('[data-testid="confirm-dialog-mount"]')).toHaveLength(1);
  });
});
