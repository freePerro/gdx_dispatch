/**
 * AccountingLedgerView (GL S11) — pins:
 *  1. Loads the trial balance on mount and renders the zero-proof tag
 *     (green when Σ == 0, red with the residual when not).
 *  2. Switching to the P&L tab lazily fetches with the selected basis.
 *  3. The journal tab renders entries with a source drill (invoice link)
 *     and per-line detail on expansion.
 *  4. A "provisional" banner sits outside the tabs, so it covers every
 *     report until the bank-statement tie-out lands (GDXA-1183).
 *  5. The trial-balance tab ties open invoices to the AR account and names
 *     every invoice that disagrees, linked to it (GDXA-1181), and every
 *     late pre-cutover payment that posted nowhere (GDXA-1223).
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

const AR = {
  ledger_posting_enabled: true,
  cutover_month: '2026-07-01',
  control_cents: 33000,
  subledger_cents: 33000,
  difference_cents: 0,
  tied: true,
  drift_invoice_count: 0,
  drift: [],
  unattributed_cents: 0,
  unattributed_by_source: [],
  era_settlement_shortfall_cents: 0,
  era_settlement_shortfall_count: 0,
  era_settlement_shortfalls: [],
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
  if (url.includes('/ar-control')) return AR;
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

  it('labels the P&L and balance sheet incomplete with the missing counts (GDXA-356)', async () => {
    const completeness = {
      incomplete: true,
      unconfirmed_vendor_bills: 3,
      unconfirmed_vendor_bills_total_cents: 125000,
      unreconciled_payroll_entries: 0,
      payroll_gross_cents: 0,
      wages_posted_cents: 0,
      no_wages_posted: true,
    };
    apiGet.mockImplementation(async (url) => {
      const body = route(url);
      return url.includes('/pnl') || url.includes('balance-sheet') ? { ...body, completeness } : body;
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.activeTab = 'pnl';
    await flushPromises();
    const note = wrapper.find('[data-testid="pnl-incomplete-note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('3 vendor bill(s) have $1,250.00 of lines not yet confirmed');
    expect(note.text()).toContain('No wages or payroll taxes are posted');
    // the figures themselves are unchanged — a label, not a recomputation
    expect(wrapper.find('[data-testid="pnl-net-income"]').text()).toContain('$440.00');

    wrapper.vm.activeTab = 'balance-sheet';
    await flushPromises();
    expect(wrapper.find('[data-testid="bs-incomplete-note"]').text()).toContain('retained earnings may be overstated');
  });

  it('shows no incomplete notice when nothing is missing', async () => {
    apiGet.mockImplementation(async (url) => {
      const body = route(url);
      return url.includes('/pnl') ? { ...body, completeness: { incomplete: false } } : body;
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.activeTab = 'pnl';
    await flushPromises();
    expect(wrapper.find('[data-testid="pnl-report"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pnl-incomplete-note"]').exists()).toBe(false);
  });

  it('marks the whole ledger provisional, outside the tabs, until the bank tie-out lands (GDXA-1183)', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    const note = wrapper.find('[data-testid="ledger-provisional-note"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('Provisional: not yet tied out to bank statements');
    // PrimeVue mounts inactive tab panels too, so presence alone would pass
    // with the banner inside one tab. It must sit outside the tabs to cover all four.
    const tabs = wrapper.find('[data-testid="ledger-tabs"]');
    expect(tabs.exists()).toBe(true);
    expect(tabs.element.contains(note.element)).toBe(false);
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

  it('ties open invoices to the AR account alongside the trial balance (GDXA-1181)', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/accounting/reports/ar-control');
    expect(wrapper.find('[data-testid="ar-control-tag"]').text()).toBe('Tied');
    expect(wrapper.find('[data-testid="ar-control-totals"]').text()).toContain('$330.00');
    expect(wrapper.find('[data-testid="ar-control-drift"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="ar-control-off"]').exists()).toBe(false);
  });

  it('still shows the trial balance when the tie-out request fails', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.includes('/ar-control')) throw new Error('boom');
      return route(url);
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="tb-totals"]').text()).toContain('$330.00');
    expect(wrapper.find('[data-testid="ar-control"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="ar-control-failed"]').exists()).toBe(true);
  });

  it('names each invoice that disagrees with the ledger, and AR no invoice claims', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.includes('/ar-control')) {
        return {
          ...AR,
          control_cents: 37500,
          difference_cents: 4500,
          tied: false,
          drift_invoice_count: 1,
          drift: [{
            invoice_id: 'inv9', invoice_number: 'INV-9', status: 'sent', deleted: false,
            open_balance_cents: 25000, ledger_ar_cents: 0, drift_cents: -25000,
          }],
          unattributed_cents: 4500,
          unattributed_by_source: [{ source_type: 'manual', amount_cents: 4500 }],
        };
      }
      return route(url);
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="ar-control-tag"]').text()).toBe('NOT TIED: $45.00');
    const table = wrapper.find('[data-testid="ar-control-drift"]');
    expect(table.text()).toContain('INV-9');
    expect(table.text()).toContain('-$250.00');
    expect(wrapper.find('[data-testid="ar-control-unattributed"]').text()).toContain('manual $45.00');
    expect(wrapper.find('[data-testid="ar-control-drift-note"]').text()).toContain('1 invoice(s) disagree');
  });

  it('is not tied, and names the payment, when money posted nowhere (GDXA-1223)', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.includes('/ar-control')) {
        return {
          ...AR,
          control_cents: 0,
          subledger_cents: 0,
          tied: false,
          era_settlement_shortfall_cents: 10000,
          era_settlement_shortfall_count: 1,
          era_settlement_shortfalls: [{
            invoice_id: 'inv7', invoice_number: 'INV-7', payment_id: 'p1',
            payment_date: '2026-06-20', amount_cents: 20000, allocated_cents: 10000,
            uncovered_cents: 10000,
          }],
        };
      }
      return route(url);
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    // No "$0.00" headline: the totals agree, the book still does not tie.
    expect(wrapper.find('[data-testid="ar-control-tag"]').text()).toBe('NOT TIED');
    expect(wrapper.find('[data-testid="ar-control-shortfall-note"]').text()).toContain('$100.00');
    const table = wrapper.find('[data-testid="ar-control-shortfalls"]');
    expect(table.text()).toContain('INV-7');
    expect(table.text()).toContain('$200.00');
    expect(table.text()).toContain('2026-06-20');
    expect(wrapper.find('[data-testid="ar-control-shortfall-cap"]').exists()).toBe(false);
  });

  it('says how many shortfalls are listed when the payload is capped (GDXA-1244)', async () => {
    const row = (n, cents) => ({
      invoice_id: `inv${n}`, invoice_number: `INV-${n}`, payment_id: `p${n}`,
      payment_date: '2026-06-20', amount_cents: cents + 10000, allocated_cents: 10000,
      uncovered_cents: cents,
    });
    apiGet.mockImplementation(async (url) => {
      if (url.includes('/ar-control')) {
        return {
          ...AR,
          tied: false,
          era_settlement_shortfall_cents: 33000,
          era_settlement_shortfall_count: 3,
          era_settlement_shortfalls: [row(1, 15000), row(2, 9000)],
        };
      }
      return route(url);
    });
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="ar-control-shortfall-note"]').text()).toContain('$330.00');
    expect(wrapper.find('[data-testid="ar-control-shortfall-cap"]').text()).toBe(
      'Showing the 2 largest of 3 payments.',
    );
  });

  it('shows no shortfall section when nothing is uncovered', async () => {
    const wrapper = mount(AccountingLedgerView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="ar-control-shortfall-note"]').exists()).toBe(false);
  });
});
