/**
 * VendorStatementsView — the vendor account position.
 *
 * The page leads with what's actually owed rather than a list of documents,
 * because a statement is a snapshot of open items: an unpaid invoice reappears
 * on every statement until it clears, so the document list answers "what
 * arrived", not "what do I owe".
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';
import Tooltip from 'primevue/tooltip';

import VendorStatementsView from '../VendorStatementsView.vue';

function mkResponse(body) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function _account(overrides = {}) {
  return {
    vendor_name: 'Example Door Supply',
    vendor_code: 'ACME01',
    as_of: '2026-07-19',
    statement_id: 'stmt-1',
    statement_count: 11,
    open_balance: '36855.52',
    open_line_count: 14,
    original_total: '39310.82',
    paid_to_date: '2455.30',
    oldest_line_date: '2026-01-23',
    days_oldest_open: 186,
    aging: { '120+': '6909.84', '30-59': '14311.27', '0-29': '15634.41' },
    lines: [
      {
        invoice_no: 'INV-OLD', line_date: '2026-01-23', amount: '3674.00',
        paid: '2455.30', balance: '1218.70', aging_bucket: '120+',
        vendor_job_no: '900123', po_ref: 'PO-9', description: '8x7 door',
        classification: 'job', matched_job_id: null,
        first_seen_on: '2026-02-11', statements_seen: 11,
      },
      {
        invoice_no: 'INV-NEW', line_date: '2026-07-08', amount: '500.00',
        paid: '0.00', balance: '500.00', aging_bucket: '0-29',
        vendor_job_no: '900999', po_ref: null, description: 'springs',
        classification: 'inventory', matched_job_id: null,
        first_seen_on: '2026-07-19', statements_seen: 1,
      },
    ],
    change: {
      previous_statement_date: '2026-06-26',
      new_invoice_count: 4, new_invoice_total: '13056.99',
      cleared_count: 6, cleared_total: '29544.70',
      paid_down_count: 1, paid_down_total: '2455.30',
      implied_payment_total: '32000.00',
    },
    ...overrides,
  };
}

const _statement = {
  id: 'stmt-1', vendor_name: 'Example Door Supply', vendor_code: 'ACME01',
  statement_date: '2026-07-19', document_id: 'doc-1', parser_name: 'midwest_v1',
  parser_version: 1, raw_total: '36855.52', line_count: 14, status: 'parsed',
  source: 'email', uploaded_by: null, created_at: '2026-07-28T00:00:00Z',
};

const globalConfig = {
  plugins: [PrimeVue, ConfirmationService, ToastService],
  directives: { tooltip: Tooltip },
  stubs: { AppLayout: { template: '<div><slot /></div>' } },
};

function _onOrder(overrides = {}) {
  return {
    vendor_name: 'Example Door Supply',
    vendor_code: 'ACME01',
    awaiting_bill_count: 1,
    awaiting_bill_total: '3707.74',
    items: [
      {
        order_id: 'ord-1', matched_job_id: null,
        order_number: '20635854', order_date: '2026-07-23', ship_to: 'A Jobsite',
        customer_po: 'A Jobsite', lot_no: null, estimated_total: '3707.74',
        line_count: 1, status: 'awaiting_bill', billed_total: null, variance: null,
        lines: [{
          line_no: 0, description: 'Garage Door Material and Labor',
          notes: 'CHI 4283 12x10 Black Long Panel', quantity: '2',
          unit: 'EA', unit_cost: '1853.8700', line_total: '3707.74',
        }],
      },
      {
        order_id: 'ord-2', matched_job_id: null,
        order_number: '20476417', order_date: '2026-06-22', ship_to: 'Another Site',
        customer_po: '98022', lot_no: null, estimated_total: '2469.42',
        line_count: 1, status: 'billed', billed_total: '2577.42', variance: '108.00',
        lines: [],
      },
    ],
    ...overrides,
  };
}

async function mountWith({
  accounts = [_account()], statements = [_statement], onOrder = [_onOrder()],
} = {}) {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(mkResponse(statements))
    .mockResolvedValueOnce(mkResponse(accounts))
    .mockResolvedValueOnce(mkResponse(onOrder));
  global.fetch = fetchMock;
  const w = mount(VendorStatementsView, { global: globalConfig });
  await flushPromises();
  return { w, fetchMock };
}

describe('VendorStatementsView — account position', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    global.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
    Object.defineProperty(window, 'location', {
      writable: true, configurable: true,
      value: { href: '', hostname: 'localhost' },
    });
  });

  it('loads both the statement list and the account position', async () => {
    const { fetchMock } = await mountWith();
    const urls = fetchMock.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/vendor-statements');
    expect(urls).toContain('/api/vendor-statements/accounts');
  });

  it('leads with what is owed, not the sum of statements', async () => {
    // 11 statements totalling ~$557k naively; the position is $36,855.52.
    const { w } = await mountWith();
    const text = w.text();
    expect(text).toContain('36,855.52');
    expect(text).not.toContain('556,880');
    expect(text).toContain('open on 14 invoices');
  });

  it('surfaces the aged money', async () => {
    const { w } = await mountWith();
    const text = w.text();
    expect(text).toContain('120+ days');
    expect(text).toContain('6,909.84');
    expect(text).toContain('186 days');
  });

  it('reports the implied payment and marks it derived', async () => {
    const { w } = await mountWith();
    const change = w.find('[data-testid="account-change"]');
    expect(change.exists()).toBe(true);
    expect(change.text()).toContain('32,000.00');
    // It is inference, not a recorded payment — it must say so on its face.
    expect(change.text()).toContain('derived');
  });

  it('says so plainly when nothing moved', async () => {
    const { w } = await mountWith({
      accounts: [_account({
        change: {
          previous_statement_date: '2026-06-26',
          new_invoice_count: 0, new_invoice_total: '0.00',
          cleared_count: 0, cleared_total: '0.00',
          paid_down_count: 0, paid_down_total: '0.00',
          implied_payment_total: '0.00',
        },
      })],
    });
    expect(w.find('[data-testid="account-change"]').text()).toContain('nothing moved');
  });

  it('omits the change line for a first statement', async () => {
    const { w } = await mountWith({ accounts: [_account({ change: null })] });
    expect(w.find('[data-testid="account-change"]').exists()).toBe(false);
  });

  it('keeps open invoices collapsed until asked', async () => {
    const { w } = await mountWith();
    expect(w.find('[data-testid="open-items-table"]').exists()).toBe(false);
    await w.find('[data-testid="toggle-open-Example Door Supply"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="open-items-table"]').exists()).toBe(true);
  });

  it('shows how long an invoice has been carried', async () => {
    // The "duplicates" across statements read as age, which is the useful sense.
    const { w } = await mountWith();
    await w.find('[data-testid="toggle-open-Example Door Supply"]').trigger('click');
    await flushPromises();
    const table = w.find('[data-testid="open-items-table"]').text();
    expect(table).toContain('11×');
    expect(table).toContain('first time');
  });

  it('shows partial payment against an open invoice', async () => {
    const { w } = await mountWith();
    await w.find('[data-testid="toggle-open-Example Door Supply"]').trigger('click');
    await flushPromises();
    const table = w.find('[data-testid="open-items-table"]').text();
    expect(table).toContain('3,674.00');   // original
    expect(table).toContain('2,455.30');   // paid
    expect(table).toContain('1,218.70');   // still open
  });

  it('renders exactly the invoices the count promises', async () => {
    // The button says "Show N open invoices"; the table must contain N rows.
    // A line the supplier still lists at a nil balance is settled, not open.
    const { w } = await mountWith({
      accounts: [_account({
        open_line_count: 1,
        lines: [
          {
            invoice_no: 'OPEN', line_date: '2026-06-01', amount: '500.00',
            paid: '0.00', balance: '500.00', aging_bucket: '0-29',
            vendor_job_no: '900001', po_ref: null, description: 'open item',
            classification: 'job', matched_job_id: null,
            first_seen_on: '2026-07-19', statements_seen: 1,
          },
          {
            invoice_no: 'SETTLED', line_date: '2026-05-01', amount: '400.00',
            paid: '400.00', balance: '0.00', aging_bucket: '0-29',
            vendor_job_no: '900002', po_ref: null, description: 'settled',
            classification: 'job', matched_job_id: null,
            first_seen_on: '2026-06-26', statements_seen: 2,
          },
        ],
      })],
    });

    expect(w.text()).toContain('Show 1 open invoices');
    await w.find('[data-testid="toggle-open-Example Door Supply"]').trigger('click');
    await flushPromises();

    const table = w.find('[data-testid="open-items-table"]');
    expect(table.text()).toContain('OPEN');
    expect(table.text()).not.toContain('SETTLED');
  });

  it('shows the account code so a merged or split account is visible', async () => {
    const { w } = await mountWith();
    expect(w.text()).toContain('ACME01');
  });

  it('still renders the statement history below the account', async () => {
    // The history DataTable used to be `v-else` on the loading spinner —
    // inserting the account section between them silently orphaned it and the
    // whole history vanished. Caught in a browser screenshot, not by a test.
    const { w } = await mountWith();
    expect(w.text()).toContain('Statement history');
    expect(w.find('[data-testid="vendor-statements-table"]').exists()).toBe(true);
  });

  // ── on order ──────────────────────────────────────────────────────
  it('reports committed spend separately from the balance owed', async () => {
    // A supplier quote is not a debt; adding them would overstate what's due.
    const { w } = await mountWith();
    const note = w.find('[data-testid="account-on-order"]');
    expect(note.exists()).toBe(true);
    expect(note.text()).toContain('3,707.74');
    expect(note.text()).toContain('not included in the balance above');
    // The headline balance is untouched by the order.
    expect(w.text()).toContain('36,855.52');
  });

  it('says nothing about orders when none are awaiting a bill', async () => {
    const { w } = await mountWith({
      onOrder: [_onOrder({ awaiting_bill_count: 0, awaiting_bill_total: '0.00' })],
    });
    expect(w.find('[data-testid="account-on-order"]').exists()).toBe(false);
  });

  it('shows the ordered doors and the ordered-vs-billed variance', async () => {
    const { w } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();

    const table = w.find('[data-testid="on-order-table"]');
    expect(table.exists()).toBe(true);
    expect(table.text()).toContain('20635854');
    expect(table.text()).toContain('Not billed yet');
    expect(table.text()).toContain('CHI 4283 12x10 Black Long Panel');
    // Ordered $2,469.42, billed $2,577.42 — surfaced, not judged.
    expect(table.text()).toContain('108.00');
  });

  it('does not offer an orders table when there are no orders', async () => {
    const { w } = await mountWith({ onOrder: [] });
    expect(w.find('[data-testid="toggle-orders-Example Door Supply"]').exists()).toBe(false);
  });

  // ── order → job ───────────────────────────────────────────────────
  it('offers to find a job for an unfiled order', async () => {
    const { w } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="suggest-20635854"]').exists()).toBe(true);
  });

  it('shows why a job was suggested, not just a score', async () => {
    // A human confirming needs the reason; a bare confidence number is
    // something to click past rather than check.
    const { w, fetchMock } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();

    fetchMock.mockResolvedValueOnce(mkResponse({
      suggestions: [{
        job_id: 'job-1', score: 0.91, job_number: 'JOB-2026-004', job_title: 'Trende',
        reason: 'ship_to "SFL Trende" ≈ job "Trende"',
        customer_id: 'cust-1', customer_name: 'Trende', lifecycle_stage: 'scheduled',
      }],
      customers_without_jobs: [],
    }));
    await w.find('[data-testid="suggest-20635854"]').trigger('click');
    await flushPromises();

    const text = w.text();
    expect(text).toContain('Trende');
    expect(text).toContain('SFL Trende');
    expect(w.find('[data-testid="confirm-20635854"]').exists()).toBe(true);
  });

  it('says so plainly when the reference matches no customer', async () => {
    const { w, fetchMock } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();

    fetchMock.mockResolvedValueOnce(mkResponse({ suggestions: [], customers_without_jobs: [] }));
    await w.find('[data-testid="suggest-20635854"]').trigger('click');
    await flushPromises();

    expect(w.text()).toContain("doesn't resemble a customer or a job");
  });

  it('posts the chosen job to the confirm endpoint', async () => {
    const { w, fetchMock } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();

    fetchMock.mockResolvedValueOnce(mkResponse({
      suggestions: [{
        job_id: 'job-1', score: 0.91, reason: 'ship_to ≈ job', job_title: 'Trende',
        customer_id: 'cust-1', customer_name: 'Trende',
      }],
      customers_without_jobs: [],
    }));
    await w.find('[data-testid="suggest-20635854"]').trigger('click');
    await flushPromises();

    fetchMock.mockResolvedValueOnce(mkResponse({
      order_number: '20635854', job_id: 'job-1', customer_id: 'cust-1',
      documents: [], newly_filed_count: 2,
    }));
    await w.find('[data-testid="confirm-20635854"]').trigger('click');
    await flushPromises();

    const call = fetchMock.mock.calls.find((c) => String(c[0]).includes('confirm-job'));
    expect(call).toBeTruthy();
    expect(call[0]).toContain('/orders/ord-1/confirm-job');
    expect(JSON.parse(call[1].body)).toEqual({ job_id: 'job-1' });
  });

  it('says the customer matched but has no job, rather than "no match"', async () => {
    // Six real orders matched a customer at up to 0.89 and were shown as
    // "the reference doesn't look like a customer name". Those are opposite
    // instructions: one means the text is junk, the other means create the job.
    const { w, fetchMock } = await mountWith();
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();

    fetchMock.mockResolvedValueOnce(mkResponse({
      suggestions: [],
      customers_without_jobs: [{
        customer_id: 'cust-9', customer_name: 'Chad Bryniarski', score: 0.89,
        reason: 'ship_to "A+ Bryniarski" ≈ customer "Chad Bryniarski"',
      }],
    }));
    await w.find('[data-testid="suggest-20635854"]').trigger('click');
    await flushPromises();

    const hint = w.find('[data-testid="no-job-20635854"]');
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain('Chad Bryniarski');
    expect(hint.text()).toContain('no job on file');
    expect(w.text()).not.toContain("doesn't resemble a customer or a job");
  });

  it('does not offer to find a job for an order already filed', async () => {
    const filed = _onOrder();
    filed.items[0].matched_job_id = 'job-1';
    const { w } = await mountWith({ onOrder: [filed] });
    await w.find('[data-testid="toggle-orders-Example Door Supply"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="suggest-20635854"]').exists()).toBe(false);
    expect(w.text()).toContain('Filed to job');
  });

  it('renders nothing account-shaped when there are no statements yet', async () => {
    const { w } = await mountWith({ accounts: [], statements: [] });
    expect(w.find('[data-testid="vendor-accounts"]').exists()).toBe(false);
  });
});
