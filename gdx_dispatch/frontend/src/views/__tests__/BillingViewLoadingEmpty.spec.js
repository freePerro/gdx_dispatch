/**
 * BillingView — the empty state waits for the invoice list (GDXA-940).
 *
 * The first paint used to happen with loading=false and invoices=[], so the
 * page read "No invoices yet" and "All (0)" before /api/invoices had even been
 * asked; the DataTable's #empty slot also sat under the mask for as long as
 * the three queue fetches took after it.
 *
 * Pinned, against a mounted view:
 *  1. While /api/invoices is pending: no empty text, no "(0)" tab counts.
 *  2. After an empty answer: "No invoices yet" and "All (0)".
 *  3. After a failed answer: an error state, never "No invoices yet".
 *  4. The table mask drops once the list is in, while a queue fetch is still
 *     pending; the queue card fills in when that fetch lands.
 *  5. A ?status= deep link selects its tab before the table unmasks.
 *  6. The summary cards show no $0.00 before either the summary or the list
 *     is in.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import BillingView from '../BillingView.vue';

const routeQuery = {};
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ path: '/billing', fullPath: '/billing', query: routeQuery }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGetMock, post: vi.fn(), patch: vi.fn(), put: vi.fn(), del: vi.fn(),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({
    confirmAsync: () => Promise.resolve(true),
    confirmDestructive: () => Promise.resolve(true),
  }),
}));

// The real DataTable renders #empty whenever its value is empty, mask or no
// mask; this stub does the same and exposes the mask as an attribute.
const DataTableStub = {
  props: ['value', 'loading'],
  template: `<div data-testid="billing-datatable" :data-loading="String(!!loading)">
    <slot v-if="!value || !value.length" name="empty" />
  </div>`,
};

const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  DataTable: DataTableStub,
  Column: true,
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Card: { template: '<div><slot name="title" /><slot name="content" /></div>' },
  Dialog: true,
  DatePicker: true,
  Select: true,
  InputNumber: true,
  InputText: true,
  Tag: true,
  Toast: true,
  ProgressBar: true,
  OutstandingAgingDialog: true,
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// Routes every GET; `/api/invoices` and the ready-for-billing queue are
// handed in so a test can hold them open.
function routeGets({ invoices, rfb, summary }) {
  apiGetMock.mockImplementation((url) => {
    if (url === '/api/invoices') return invoices;
    if (url === '/api/invoices/summary') return summary ?? Promise.resolve({});
    if (url === '/api/jobs/ready-for-billing') return rfb ?? Promise.resolve([]);
    if (url.startsWith('/api/customers')) return Promise.resolve([]);
    if (url === '/api/jobs') return Promise.resolve([]);
    if (url === '/api/invoices/closeout-discrepancies') return Promise.resolve({ items: [] });
    return Promise.resolve({});
  });
}

function mountView() {
  return mount(BillingView, { global: { stubs } });
}

describe('BillingView — empty state waits for the invoice list (GDXA-940)', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    localStorage.clear();
    for (const k of Object.keys(routeQuery)) delete routeQuery[k];
  });

  it('shows neither "No invoices yet" nor "(0)" counts while /api/invoices is pending, then both after an empty answer', async () => {
    const inv = deferred();
    routeGets({ invoices: inv.promise });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="billing-datatable"]').attributes('data-loading')).toBe('true');
    expect(w.text()).not.toContain('No invoices yet');
    expect(w.find('[data-testid="billing-list-empty"]').exists()).toBe(false);
    expect(w.text()).not.toContain('All (0)');
    // The queue cards are fetched alongside the list, not after it.
    expect(apiGetMock).toHaveBeenCalledWith('/api/jobs/ready-for-billing');
    // A summary without the figure (the default mock answers {}) is no figure.
    expect(w.find('[data-testid="billing-total-outstanding"]').text()).not.toContain('$0.00');

    inv.resolve([]);
    await flushPromises();

    expect(w.find('[data-testid="billing-datatable"]').attributes('data-loading')).toBe('false');
    expect(w.text()).toContain('No invoices yet');
    expect(w.text()).toContain('All (0)');
  });

  it('a failed /api/invoices shows an error state, never "No invoices yet"', async () => {
    routeGets({ invoices: Promise.reject(Object.assign(new Error('boom'), { status: 500 })) });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="billing-list-error"]').exists()).toBe(true);
    expect(w.text()).not.toContain('No invoices yet');
    expect(w.text()).not.toContain('All (0)');
  });

  it('drops the table mask once the list is in, while a queue fetch is still pending', async () => {
    const rfb = deferred();
    routeGets({
      invoices: Promise.resolve([{ id: 'a1', invoice_number: 'INV-1', status: 'Sent', total: 10 }]),
      rfb: rfb.promise,
    });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="billing-datatable"]').attributes('data-loading')).toBe('false');
    expect(w.text()).toContain('All (1)');
    expect(w.find('[data-testid="ready-for-billing"]').exists()).toBe(false);

    rfb.resolve([{ id: 'j1', customer_name: 'Queue Customer', title: 'Queue job' }]);
    await flushPromises();
    expect(w.find('[data-testid="ready-for-billing"]').exists()).toBe(true);
  });

  it('a ?status= deep link selects its tab before the table unmasks', async () => {
    routeQuery.status = 'Overdue';
    const rfb = deferred();
    routeGets({
      invoices: Promise.resolve([
        { id: 'a1', invoice_number: 'INV-1', status: 'Sent', total: 10 },
        { id: 'a2', invoice_number: 'INV-2', status: 'Overdue', total: 20 },
      ]),
      rfb: rfb.promise,
    });
    const w = mountView();
    await flushPromises();

    // Unmasked, queues still pending: the filtered view is already the one shown.
    expect(w.find('[data-testid="billing-datatable"]').attributes('data-loading')).toBe('false');
    expect(w.vm.$.setupState.activeStatus).toBe('Overdue');
    rfb.resolve([]);
    await flushPromises();
  });

  it('summary cards show no $0.00 before the summary or the list is in', async () => {
    const inv = deferred();
    const summary = deferred();
    routeGets({ invoices: inv.promise, summary: summary.promise });
    const w = mountView();
    await flushPromises();

    const outstanding = () => w.find('[data-testid="billing-total-outstanding"]').text();
    const drafts = () => w.find('[data-testid="billing-draft-invoices"]').text();
    expect(outstanding()).not.toContain('$0.00');
    expect(outstanding()).toContain('—');
    expect(drafts()).not.toMatch(/\b0\b/);

    summary.resolve({ total_outstanding: 1234.5, overdue: 0, paid_this_month: 0, draft_count: 2, draft_total: 50 });
    await flushPromises();
    expect(outstanding()).toContain('1,234.50');
    expect(drafts()).toContain('2');
    inv.resolve([]);
    await flushPromises();
  });
});
