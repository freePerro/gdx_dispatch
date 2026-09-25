/**
 * The class guard: a mobile single-document surface names its customer, and
 * that name is the route to the customer record.
 *
 * Before this, three mobile screens printed the customer as text and stopped:
 * the job detail, the estimate detail dialog and the invoice detail dialog.
 * A tech at a door who needed the gate code or a second phone number had to
 * leave the document, open the Customers tab and search for a name he was
 * already looking at. `/mobile/customers/:id` has existed and been ungated
 * the whole time (router/index.js) — only the route in was missing.
 *
 * Pinned here, per surface:
 *  1. id + name present  -> a router-link to /mobile/customers/:id.
 *  2. id missing         -> plain text, never an anchor. A dead or empty
 *                           anchor is worse than text: it has no accessible
 *                           name and it lies about where it goes.
 *  3. name missing       -> plain text, same reason.
 *
 * "name missing" is not only a cosmetic case: on the estimate and invoice
 * dialogs it is also how a SOFT-DELETED customer arrives, because get_estimate
 * and get_invoice resolve the name for live customers only. /api/customers/{id}
 * 404s on a deleted record, so a link there is a dead end — withholding it on
 * a missing name is what keeps that from happening.
 *
 * The job detail is the exception and has its own case below: that payload
 * deliberately keeps naming a deleted customer (the tech still needs the phone
 * number), so it carries an explicit `deleted` flag instead.
 */
import 'fake-indexeddb/auto';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { ref } from 'vue';
import { createPinia, setActivePinia } from 'pinia';

const getMock = vi.fn();
const postQueuedMock = vi.fn();

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({
    params: { id: 'job-123' },
    query: {},
    path: '/mobile/jobs/job-123',
    fullPath: '/mobile/jobs/job-123',
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: getMock,
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
    patch: vi.fn(),
    postQueued: postQueuedMock,
    patchQueued: vi.fn(),
  }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: () => true,
    permissions: { value: [] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn().mockResolvedValue(true) }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ loadPermissions: vi.fn(async () => {}) }),
}));
vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
  }),
}));

const stubs = {
  RouterLink: RouterLinkStub,
  AppLayout: { template: '<div><slot /></div>' },
  Button: {
    props: ['label', 'icon', 'loading', 'severity', 'text', 'rounded', 'outlined', 'size', 'disabled'],
    emits: ['click'],
    template: '<button v-bind="$attrs" @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Tag: { props: ['value', 'severity'], template: '<span>{{ value }}</span>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  SelectButton: { props: ['modelValue', 'options'], template: '<div />' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  MobileJobCloseoutDialog: { props: ['visible', 'jobId'], template: '<div />' },
  MobileInvoiceDialog: { props: ['visible', 'job'], template: '<div />' },
};

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

// ── Job detail ────────────────────────────────────────────────────────────
// /api/mobile/job/{id} nests the customer with both id and name
// (routers/mobile.py — `job["customer"] = customer`).
describe('MobileJobDetailView — the customer name routes to the record', () => {
  async function mountJob(customer) {
    const { default: View } = await import('../MobileJobDetailView.vue');
    getMock.mockImplementation(async () => ({
      job: {
        id: 'job-123',
        title: 'Install',
        dispatch_status: 'assigned',
        customer,
      },
      notes: [],
      photos: [],
    }));
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    return w;
  }

  it('links the name to /mobile/customers/:id when id and name are both present', async () => {
    const w = await mountJob({ id: 'c1', name: 'Acme Doors' });

    const link = w.find('[data-testid="mobile-job-detail-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(w.findComponent(RouterLinkStub).props('to')).toBe('/mobile/customers/c1');
  });

  it('renders plain text, not an anchor, when the customer id is missing', async () => {
    const w = await mountJob({ id: null, name: 'Acme Doors' });

    expect(w.find('[data-testid="mobile-job-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-testid="mobile-job-detail-customer"]').text()).toBe('Acme Doors');
  });

  it('renders the em dash, not an empty anchor, when the name is missing', async () => {
    const w = await mountJob({ id: 'c1', name: null });

    expect(w.find('[data-testid="mobile-job-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-testid="mobile-job-detail-customer"]').text()).toBe('—');
  });

  // The office soft-deletes a customer with no referential check, and this
  // payload keeps naming it on purpose so the tech still has the number. But
  // GET /api/customers/{id} filters deleted_at and 404s, so the name must stay
  // TEXT here — linking it would send the tech to an error card.
  it('keeps the name as text, with no link, when the customer is soft-deleted', async () => {
    const w = await mountJob({ id: 'c1', name: 'Acme Doors', deleted: true });

    expect(w.find('[data-testid="mobile-job-detail-customer-link"]').exists()).toBe(false);
    expect(w.find('[data-testid="mobile-job-detail-customer"]').text()).toBe('Acme Doors');
  });
});

// ── Estimate detail dialog ────────────────────────────────────────────────
describe('MobileEstimatesView — the customer name routes to the record', () => {
  // The name comes from the detail payload, which get_estimate now enriches
  // from Estimate.customer_id for a live customer only. The list row is NEVER
  // a name source: list_estimates falls back to the JOB's customer name
  // without changing customer_id, so a row can pair one customer's name with
  // another's id. The rows below carry a deliberately WRONG name to pin that.
  async function openEstimate({ row, detail }) {
    const { default: View } = await import('../MobileEstimatesView.vue');
    getMock.mockImplementation(async (url) => {
      if (url === '/api/estimates') return [row];
      if (url === `/api/estimates/${row.id}`) return detail;
      return {};
    });
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    await w.find('.est-card').trigger('click');
    await flushPromises();
    return w;
  }

  const ROW = {
    id: 'est-1',
    number: 'E-1001',
    status: 'Draft',
    total: 250,
    customer_id: 'c1',
    // A job-derived name for a DIFFERENT customer than customer_id — the shape
    // list_estimates really produces when name_by_cust misses.
    customer_name: 'Wrong Customer From Job',
  };
  const DETAIL = {
    id: 'est-1', number: 'E-1001', status: 'Draft', total: 250,
    customer_id: 'c1', customer_name: 'Acme Doors', lines: [],
  };

  it('links the name to /mobile/customers/:id, naming the id\'s own customer', async () => {
    const w = await openEstimate({ row: ROW, detail: DETAIL });

    const link = w.find('[data-test="me-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(link.text()).not.toContain('Wrong Customer');
    expect(w.findComponent(RouterLinkStub).props('to')).toBe('/mobile/customers/c1');
  });

  it('renders plain text when the estimate has no customer_id (QB-imported, linked via the job)', async () => {
    const w = await openEstimate({
      row: { ...ROW, customer_id: null },
      detail: { ...DETAIL, customer_id: null, customer_name: 'Acme Doors' },
    });

    // Scoped to the dialog's OWN line — w.text() also contains the list card
    // rendered behind the dialog, which would make this pass either way.
    expect(w.find('[data-test="me-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta .meta-line').text()).toContain('Acme Doors');
  });

  it('renders the em dash, not an empty anchor, when the server sends no name', async () => {
    const w = await openEstimate({
      row: ROW,
      detail: { ...DETAIL, customer_name: undefined },
    });

    expect(w.find('[data-test="me-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta .meta-line').text()).toContain('—');
  });

  // get_estimate still NAMES a soft-deleted customer and flags it, because the
  // same payload feeds the desktop estimate header. The flag is what withholds
  // the link; the name stays readable.
  it('keeps the name as text, with no link, when the customer is soft-deleted', async () => {
    const w = await openEstimate({
      row: ROW,
      detail: { ...DETAIL, customer_deleted: true },
    });

    expect(w.find('[data-test="me-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta .meta-line').text()).toContain('Acme Doors');
  });

  // The office can reassign an estimate (estimates.py ~2939) while a tech's
  // list is still loaded. Name and href both come from the fresh detail, so a
  // stale row cannot make the link name one customer and navigate to another.
  it('never names one customer while linking to another', async () => {
    const w = await openEstimate({
      row: { ...ROW, customer_id: 'c-OLD', customer_name: 'Old Customer' },
      detail: { ...DETAIL, customer_id: 'c-NEW', customer_name: 'New Customer' },
    });

    const link = w.find('[data-test="me-customer-link"]');
    expect(link.text()).toBe('New Customer');
    expect(w.find('.detail-meta .meta-line').text()).not.toContain('Old Customer');
    expect(w.findComponent(RouterLinkStub).props('to')).toBe('/mobile/customers/c-NEW');
  });
});

// ── Invoice detail dialog ─────────────────────────────────────────────────
// GET /api/invoices/{id} sets customer_id and customer_name together,
// including the Job -> Customer fallback for QB-imported invoices.
describe('MobileBillingView — the customer name routes to the record', () => {
  async function openInvoice(detail) {
    const { default: View } = await import('../MobileBillingView.vue');
    getMock.mockImplementation(async (url) => {
      if (url === '/api/invoices/summary') return {};
      if (url === '/api/invoices') return [{ id: 'inv-1', invoice_number: 'INV-1', status: 'Sent', total: 100, customer_name: detail.customer_name }];
      if (url === '/api/invoices/inv-1') return detail;
      return [];
    });
    const w = mount(View, { global: { stubs } });
    await flushPromises();
    await w.find('[data-test="mb-inv-row"]').trigger('click');
    await flushPromises();
    return w;
  }

  const DETAIL = {
    id: 'inv-1',
    invoice_number: 'INV-1',
    status: 'Sent',
    total: 100,
    customer_id: 'c1',
    customer_name: 'Acme Doors',
    lines: [],
  };

  it('links the name to /mobile/customers/:id when id and name are both present', async () => {
    const w = await openInvoice(DETAIL);

    const link = w.find('[data-test="mb-customer-link"]');
    expect(link.exists()).toBe(true);
    expect(link.text()).toBe('Acme Doors');
    expect(w.findComponent(RouterLinkStub).props('to')).toBe('/mobile/customers/c1');
  });

  it('renders plain text, not an anchor, when the invoice has no customer_id', async () => {
    const w = await openInvoice({ ...DETAIL, customer_id: null });

    // Scoped to the dialog's OWN line, as above.
    expect(w.find('[data-test="mb-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta .meta-line').text()).toContain('Acme Doors');
  });

  it('renders the em dash, not an empty anchor, when the name is missing', async () => {
    const w = await openInvoice({ ...DETAIL, customer_name: '' });

    expect(w.find('[data-test="mb-customer-link"]').exists()).toBe(false);
  });

  // get_invoice NAMES a soft-deleted customer and flags it — the desktop
  // invoice page guards its own link on customer_id alone and would print
  // "Unknown" beside a live link to a 404 if the name were blanked instead.
  it('keeps the name as text, with no link, when the customer is soft-deleted', async () => {
    const w = await openInvoice({ ...DETAIL, customer_deleted: true });

    expect(w.find('[data-test="mb-customer-link"]').exists()).toBe(false);
    expect(w.find('.detail-meta .meta-line').text()).toContain('Acme Doors');
  });
});
