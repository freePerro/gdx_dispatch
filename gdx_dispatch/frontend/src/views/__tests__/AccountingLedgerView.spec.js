/**
 * AccountingLedgerView (GL S11) — pins:
 *  1. Loads the trial balance on mount and renders the zero-proof tag
 *     (green when Σ == 0, red with the residual when not).
 *  2. Switching to the P&L tab lazily fetches with the selected basis.
 *  3. The journal tab renders entries with a source drill (invoice link)
 *     and per-line detail on expansion.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  openAuthedFile: vi.fn(),
}));

import AccountingLedgerView from '../AccountingLedgerView.vue';

const TB = {
  as_of: '2026-07-31',
  rows: [
    { account_id: 'a1', code: '1200', name: 'Accounts Receivable', type: 'asset', debit_cents: 33000, credit_cents: 0 },
    { account_id: 'a2', code: '4000', name: 'Service & Repair Revenue', type: 'revenue', debit_cents: 0, credit_cents: 33000 },
  ],
  totals: { debit_cents: 33000, credit_cents: 33000, zero_proof_cents: 0 },
};

const PNL = {
  basis: 'cash',
  start: '2026-07-01',
  end: '2026-07-31',
  revenue: [{ account_id: 'a2', code: '4000', name: 'Service & Repair Revenue', amount_cents: 50000 }],
  expenses: [{ account_id: 'a3', code: '6100', name: 'Fuel', amount_cents: 6000 }],
  totals: { revenue_cents: 50000, expense_cents: 6000, net_income_cents: 44000 },
};

const JOURNAL = {
  total: 1,
  limit: 50,
  offset: 0,
  entries: [
    {
      id: 'e1',
      entry_no: 7,
      effective_at: '2026-07-08',
      posted_at: '2026-07-08T12:00:00Z',
      status: 'posted',
      reverses_entry_id: null,
      source: { source_type: 'invoice', source_id: 'inv1', invoice_id: 'inv1', invoice_number: 'INV-42' },
      lines: [
        { account_id: 'a1', account_code: '1200', account_name: 'AR', amount_cents: 53000, memo: 'issued' },
        { account_id: 'a2', account_code: '4000', account_name: 'Revenue', amount_cents: -53000, memo: '' },
      ],
    },
  ],
};

const stubs = {
  'router-link': { template: '<a class="rl"><slot /></a>' },
  DatePicker: { template: '<input />' },
  Paginator: { template: '<div />' },
};

function route(url) {
  if (url.includes('trial-balance')) return TB;
  if (url.includes('/pnl')) return PNL;
  if (url.includes('/journal')) return JOURNAL;
  if (url.includes('balance-sheet')) {
    return {
      as_of: '2026-07-31',
      assets: [], liabilities: [], equity: [],
      totals: { asset_cents: 0, liability_cents: 0, equity_cents: 0, zero_proof_cents: 0 },
    };
  }
  throw new Error(`no fake for ${url}`);
}

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation(async (url) => route(url));
});

describe('AccountingLedgerView', () => {
  it('loads the trial balance on mount and shows the zero-proof', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('/api/accounting/reports/trial-balance'));
    const proof = wrapper.find('[data-testid="tb-zero-proof"]');
    expect(proof.exists()).toBe(true);
    expect(proof.text()).toMatch(/Balanced/);
    expect(wrapper.find('[data-testid="tb-totals"]').text()).toContain('$330.00');
  });

  it('flags an out-of-balance ledger in red', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.includes('trial-balance')) {
        return { ...TB, totals: { ...TB.totals, zero_proof_cents: -100 } };
      }
      return route(url);
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="tb-zero-proof"]').text()).toMatch(/OUT OF BALANCE/);
  });

  it('lazily loads the P&L with the selected basis on tab switch', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.pnlBasis = 'cash';
    wrapper.vm.activeTab = 'pnl';
    await flushPromises();
    const pnlCall = apiGet.mock.calls.find(([url]) => url.includes('/pnl'));
    expect(pnlCall[0]).toContain('basis=cash');
    expect(wrapper.find('[data-testid="pnl-net-income"]').text()).toContain('$440.00');
    expect(wrapper.find('[data-testid="pnl-revenue-total"]').text()).toContain('$500.00');
  });

  it('renders journal entries with source drill and line detail', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.activeTab = 'journal';
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith(expect.stringContaining('/api/accounting/journal'));
    const table = wrapper.find('[data-testid="journal-table"]');
    expect(table.exists()).toBe(true);
    expect(table.text()).toContain('INV-42');
    wrapper.vm.expandedEntries = { e1: true };
    await flushPromises();
    const lines = wrapper.find('[data-testid="journal-lines-7"]');
    expect(lines.exists()).toBe(true);
    expect(lines.text()).toContain('1200');
    expect(lines.text()).toContain('$530.00');
  });
});
