/**
 * Customer portal invoices (2026-10-07): each invoice is a card that opens a
 * detail dialog — lines, totals, paid to date, balance due — and the Pay
 * button opens the public pay page (pay_url). The portal mints no card
 * payment of its own.
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
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
};

const PAY_URL = 'https://example.test/pay/tok-1';
const LIST = [
  { id: 'inv-1', invoice_number: 'INV-1', billing_type: 'standard', status: 'sent', payment_status: 'unpaid',
    total: 520, balance_due: 320, due_date: '2026-10-14', pay_url: PAY_URL },
  { id: 'inv-2', invoice_number: 'INV-2', billing_type: 'standard', status: 'paid', payment_status: 'paid',
    total: 90, balance_due: 0, due_date: null, pay_url: null },
];

function detailFor(overrides = {}) {
  return {
    ...LIST[0], invoice_date: '2026-10-01', notes: 'Thanks', hide_line_prices: false, line_category: 'off',
    lines: [
      { id: 'l1', description: 'Torsion spring', quantity: 2, unit_price: 150, line_total: 300 },
      { id: 'l2', description: 'Labor', quantity: 2.5, unit_price: 80, line_total: 200 },
    ],
    totals: { subtotal: 500, tax: 20, total: 520, paid_to_date: 150, credits_applied: 50, balance_due: 320 },
    ...overrides,
  };
}

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-7', label: 'New door', status: 'accepted', total: 2600,
  hide_line_prices: false, line_category: 'off', images: [],
  lines: [{ id: 'e1', description: '16x7 door', quantity: 1, unit_price: 2600, line_total: 2600 }],
  totals: { subtotal: 2600, total: 2600 },
};

let pdfResponse = () => ({ ok: true, status: 200, blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }) });

async function mountPortal(detail, company = { name: 'GDX', phone: '555-0100', email: 'office@example.test' }) {
  const fetchMock = vi.fn(async (url) => {
    const u = String(url);
    if (u.endsWith('/pdf')) return pdfResponse(u);
    if (u.endsWith('/portal/invoices/inv-1')) return { ok: true, status: 200, json: async () => detail };
    if (u.endsWith('/portal/estimates/est-1')) return { ok: true, status: 200, json: async () => ESTIMATE };
    if (u.endsWith('/portal/estimates')) return { ok: true, status: 200, json: async () => [ESTIMATE] };
    if (u.endsWith('/portal/invoices')) return { ok: true, status: 200, json: async () => LIST };
    if (u.includes('/portal/context')) return { ok: true, status: 200, json: async () => ({ company }) };
    return { ok: true, status: 200, json: async () => [] };
  });
  vi.stubGlobal('fetch', fetchMock);
  sessionStorage.setItem('gdx_portal_jwt', 'portal-token');
  const w = mount(CustomerPortalView, { global: { stubs } });
  await flushPromises();
  return { w, fetchMock };
}

async function openFirstCard(w) {
  await w.findAll('[data-testid="invoice-card"]')[0].trigger('click');
  await flushPromises();
  // Every Dialog shares the stub's testid, so return the whole wrapper.
  return w;
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('customer portal — invoices', () => {
  it('lists each invoice as a card; only the one with a pay_url gets a Pay button', async () => {
    const { w } = await mountPortal(detailFor());
    const cards = w.findAll('[data-testid="invoice-card"]');
    expect(cards).toHaveLength(2);
    expect(cards[0].text()).toContain('INV-1');
    expect(cards[0].text()).toContain('$320.00');
    expect(cards[1].text()).toContain('Paid in full');
    const pay = w.findAll('[data-testid="invoice-pay-btn"]');
    expect(pay).toHaveLength(1);
    expect(pay[0].text()).toBe('Pay $320.00');
  });

  it('the card Pay button opens the pay page without opening the detail', async () => {
    const { w, fetchMock } = await mountPortal(detailFor());
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    await w.find('[data-testid="invoice-pay-btn"]').trigger('click');
    await flushPromises();
    expect(open).toHaveBeenCalledWith(PAY_URL, '_blank', 'noopener');
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/portal/invoices/inv-1'))).toBe(false);
    open.mockRestore();
  });

  it('clicking a card shows lines, the settlement rows and the balance, and Pay opens pay_url', async () => {
    const { w } = await mountPortal(detailFor());
    const dlg = await openFirstCard(w);
    const table = dlg.find('[data-testid="invoice-lines-table"]');
    expect(table.findAll('thead th').map((th) => th.text())).toEqual(['Item', 'Qty', 'Price', 'Total']);
    expect(table.text()).toContain('Torsion spring');
    const totals = dlg.find('[data-testid="invoice-totals"]').text();
    for (const s of ['Subtotal', '$500.00', 'Tax', '$20.00', 'Total', '$520.00', 'Paid to date', '-$150.00', 'Credits applied', '-$50.00']) {
      expect(totals).toContain(s);
    }
    expect(dlg.find('[data-testid="invoice-balance-due"]').text()).toContain('$320.00');
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    await dlg.find('[data-testid="invoice-detail-pay-btn"]').trigger('click');
    expect(open).toHaveBeenCalledWith(PAY_URL, '_blank', 'noopener');
    open.mockRestore();
  });

  it('total-only: no Price/Total columns and no Subtotal/Tax rows', async () => {
    const lines = [{ id: 'l1', description: 'Torsion spring', quantity: 2 }];
    const { w } = await mountPortal(detailFor({
      hide_line_prices: true, lines,
      totals: { total: 520, paid_to_date: 0, credits_applied: 0, balance_due: 520 },
    }));
    const dlg = await openFirstCard(w);
    expect(dlg.findAll('[data-testid="invoice-lines-table"] thead th').map((th) => th.text())).toEqual(['Item', 'Qty']);
    const totals = dlg.find('[data-testid="invoice-totals"]').text();
    expect(totals).not.toContain('Subtotal');
    expect(totals).not.toContain('Tax');
    expect(totals).not.toContain('Paid to date');
  });

  it('owed with no pay_url: says how to pay instead of a dead end', async () => {
    const { w } = await mountPortal(detailFor({ pay_url: null }));
    const dlg = await openFirstCard(w);
    expect(dlg.find('[data-testid="invoice-detail-pay-btn"]').exists()).toBe(false);
    const note = dlg.find('[data-testid="invoice-pay-offline"]').text();
    expect(note).toContain('555-0100');
    expect(note).toContain('office@example.test');
  });

  it('the header buttons keep their names when a phone hides the labels', async () => {
    const { w } = await mountPortal(detailFor());
    expect(w.find('[data-testid="set-password-btn"]').attributes('aria-label')).toBe('Password');
    expect(w.find('[data-testid="sign-out-btn"]').attributes('aria-label')).toBe('Sign out');
  });

  describe('Download PDF', () => {
    let clicked;
    beforeEach(() => {
      clicked = [];
      URL.createObjectURL = vi.fn(() => 'blob:pdf-1');
      URL.revokeObjectURL = vi.fn();
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
        clicked.push({ href: this.getAttribute('href'), download: this.download });
      });
    });
    afterEach(() => {
      vi.restoreAllMocks();
      pdfResponse = () => ({ ok: true, status: 200, blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }) });
    });

    it('the invoice detail downloads its PDF with the portal token', async () => {
      const { w, fetchMock } = await mountPortal(detailFor());
      await openFirstCard(w);
      await w.find('[data-testid="invoice-pdf-btn"]').trigger('click');
      await flushPromises();
      const call = fetchMock.mock.calls.find(([u]) => String(u) === '/portal/invoices/inv-1/pdf');
      expect(call[1].headers.Authorization).toBe('Bearer portal-token');
      expect(clicked).toEqual([{ href: 'blob:pdf-1', download: 'invoice-INV-1.pdf' }]);
    });

    it('the estimate detail downloads its PDF', async () => {
      const { w, fetchMock } = await mountPortal(detailFor());
      await w.find('[data-testid="estimate-card"]').trigger('click');
      await flushPromises();
      await w.find('[data-testid="estimate-pdf-btn"]').trigger('click');
      await flushPromises();
      expect(fetchMock.mock.calls.some(([u]) => String(u) === '/portal/estimates/est-1/pdf')).toBe(true);
      expect(clicked).toEqual([{ href: 'blob:pdf-1', download: 'estimate-EST-7.pdf' }]);
    });

    it('a failed PDF load starts no download', async () => {
      pdfResponse = () => ({ ok: false, status: 500, blob: async () => new Blob([]) });
      const { w } = await mountPortal(detailFor());
      await openFirstCard(w);
      await w.find('[data-testid="invoice-pdf-btn"]').trigger('click');
      await flushPromises();
      expect(clicked).toEqual([]);
    });
  });
});
