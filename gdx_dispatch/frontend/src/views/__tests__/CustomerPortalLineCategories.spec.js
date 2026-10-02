/**
 * Customer portal estimate detail — line categories follow the estimate PDF's
 * template setting (detail.line_category: 'off' | 'column' | 'grouped'), the
 * same rule as the public approval page.
 *
 * Renders the REAL PrimeVue DataTable (no stub), so the grouped heading rows
 * and the Category header are what PrimeVue actually draws.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const LINES = [
  { id: 'l1', description: 'Spring', category: 'Parts', quantity: 1, unit_price: 1, line_total: 1 },
  { id: 'l2', description: 'Door panel', category: 'Door', quantity: 1, unit_price: 1, line_total: 1 },
  { id: 'l3', description: 'Haul away', category: null, quantity: 1, unit_price: 1, line_total: 1 },
  { id: 'l4', description: 'Cable', category: 'Parts', quantity: 1, unit_price: 1, line_total: 1 },
];

const stubs = {
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" data-testid="detail-dialog"><slot /><slot name="footer" /></div>',
  },
  Image: { template: '<img />' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
};

async function openDetail(lineCategory) {
  const detail = {
    id: 'est-1', estimate_number: 'EST-1', status: 'sent', total: 4, hide_line_prices: false,
    line_category: lineCategory, lines: LINES, images: [],
    totals: { subtotal: 4, discount: 0, tax: 0, tax_rate_pct: 0, total: 4 },
  };
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const u = String(url);
    if (u.endsWith('/portal/estimates/est-1')) return { ok: true, status: 200, json: async () => detail };
    if (u.includes('/portal/context')) return { ok: true, status: 200, json: async () => ({ company: { name: 'GDX' } }) };
    return { ok: true, status: 200, json: async () => [] };
  }));
  sessionStorage.setItem('gdx_portal_jwt', 'portal-token');
  const w = mount(CustomerPortalView, { global: { stubs } });
  await flushPromises();
  await w.vm.openEstimate('est-1');
  await flushPromises();
  return w.find('[data-testid="estimate-lines-table"]');
}

const headers = (t) => t.findAll('thead th').map((th) => th.text());
// First cell of each ordinary (non-heading) row: the item description.
const items = (t) => t.findAll('tbody tr').filter((tr) => !tr.find('td[colspan]').exists()).map((tr) => tr.find('td').text().trim());

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });

describe('customer portal — estimate line categories', () => {
  it('off: no Category header, no heading rows, lines in their own order', async () => {
    const t = await openDetail('off');
    expect(headers(t)).toEqual(['Item', 'Qty', 'Price', 'Total']);
    expect(t.find('tbody td[colspan]').exists()).toBe(false);
    expect(items(t)).toEqual(['Spring', 'Door panel', 'Haul away', 'Cable']);
  });

  it('column: a Category header before Item', async () => {
    const t = await openDetail('column');
    expect(headers(t)).toEqual(['Category', 'Item', 'Qty', 'Price', 'Total']);
  });

  it('grouped: a heading row per category in first-appearance order, the grouping column never drawn', async () => {
    const t = await openDetail('grouped');
    expect(headers(t)).toEqual(['Item', 'Qty', 'Price', 'Total']);
    const headingCells = t.findAll('tbody tr td[colspan]');
    expect(headingCells.map((td) => td.text())).toEqual(['Parts', 'Door', '']);
    // Spans the four drawn columns — PrimeVue sizes it from the hidden _category column.
    expect(headingCells.every((td) => td.attributes('colspan') === '4')).toBe(true);
    expect(items(t)).toEqual(['Spring', 'Cable', 'Door panel', 'Haul away']);
  });
});
