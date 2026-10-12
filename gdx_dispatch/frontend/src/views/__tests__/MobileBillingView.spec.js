/**
 * MobileBillingView KPI strip tests — pins the S112 mobile parity fix.
 *
 * The strip shows Outstanding / Overdue / Paid (mo) at the top of the
 * mobile billing view. Server-prefer/client-fallback contract (S113):
 *   - When /api/invoices/summary returns numbers, those win
 *   - When the endpoint is unavailable, fall back to client computation
 *     over the loaded invoice list (drafts excluded from outstanding).
 */
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import MobileBillingView from '../MobileBillingView.vue';

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ path: '/mobile/billing', fullPath: '/mobile/billing' }),
}));
const toastAddMock = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAddMock }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
const apiPostQueuedMock = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: apiGetMock, post: apiPostMock, patch: apiPatchMock,
    postQueued: apiPostQueuedMock,
  }),
}));
// The view now splits by permission: office tiers read the whole receivables
// book, technicians read only their own jobs. Default these specs to the OFFICE
// tier so the KPI contract below keeps testing what it always tested; the
// technician path has its own test at the bottom.
const hasPermissionMock = vi.fn(() => true);
const permissionsLoadedRef = { value: true };
const loadPermissionsMock = vi.fn(async () => {});
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: hasPermissionMock,
    permissions: { value: [] },
    permissionsLoaded: permissionsLoadedRef,
    reloadPermissions: vi.fn(),
  }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ loadPermissions: loadPermissionsMock }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: () => Promise.resolve(true) }),
}));

const stubs = {
  // The customer name on these screens is a router-link to /mobile/customers/:id.
  // These specs mock vue-router wholesale, so RouterLink is not registered.
  RouterLink: RouterLinkStub,
  AppLayout: { template: '<div><slot /></div>' },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  SelectButton: { template: '<div />' },
};

describe('MobileBillingView KPI strip', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiPatchMock.mockReset();
    apiPostQueuedMock.mockReset();
    apiPostQueuedMock.mockResolvedValue({});
    hasPermissionMock.mockReset();
    hasPermissionMock.mockReturnValue(true);
    permissionsLoadedRef.value = true;
    loadPermissionsMock.mockReset();
    loadPermissionsMock.mockImplementation(async () => {});
    toastAddMock.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders three KPI tiles using server billingSummary when available', async () => {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/invoices/summary') {
        return Promise.resolve({
          total_outstanding: 78148.82,
          overdue: 78148.82,
          paid_this_month: 125.0,
          ready_for_billing: 8,
        });
      }
      if (url.startsWith('/api/invoices')) return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const w = mount(MobileBillingView, { global: { stubs } });
    await flushPromises();

    const text = w.text();
    // Labels render in original-case in the DOM (CSS text-transform:uppercase
    // is presentation only; w.text() returns source casing).
    expect(text).toContain('Outstanding');
    expect(text).toContain('Overdue');
    expect(text).toContain('Paid (mo)');
    // Server numbers wins.
    expect(text).toContain('78148.82');
    expect(text).toContain('125.00');
  });

  it('falls back to client-side computation when summary endpoint fails', async () => {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/invoices/summary') return Promise.reject(new Error('500'));
      if (url.startsWith('/api/invoices')) {
        return Promise.resolve([
          // Sent invoice — counts as outstanding
          {
            id: 'i1', status: 'sent', effective_status: 'overdue',
            balance_due: 200, total: 200,
            due_date: '2026-04-01', invoice_number: 'A-1',
          },
          // Draft — excluded from outstanding (S111 contract)
          {
            id: 'i2', status: 'draft', balance_due: 9999, total: 9999,
            invoice_number: 'A-2',
          },
        ]);
      }
      return Promise.resolve([]);
    });

    const w = mount(MobileBillingView, { global: { stubs } });
    await flushPromises();
    // Read the KPI strip text only (the invoice list also renders draft
    // amounts, which would falsely fail a body-wide negative assertion).
    const stripText = w.find('[data-test="mb-kpis"]').text();
    // Outstanding = 200 (drafts excluded). Overdue also = 200 (the server
    // marked the sent invoice overdue).
    expect(stripText).toContain('200.00');
    expect(stripText).not.toContain('9999');
  });

  it('marks Outstanding + Overdue with .alert when overdue > 0', async () => {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/invoices/summary') {
        return Promise.resolve({
          total_outstanding: 500,
          overdue: 200,
          paid_this_month: 0,
          ready_for_billing: 0,
        });
      }
      if (url.startsWith('/api/invoices')) return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const w = mount(MobileBillingView, { global: { stubs } });
    await flushPromises();

    const alertKpis = w.findAll('.kpi.alert');
    // Outstanding + Overdue both alert when overdue > 0 (Paid (mo) does NOT)
    expect(alertKpis.length).toBe(2);
  });
});

describe('MobileBillingView — Mark paid records a real payment (2026-07-21)', () => {
  // This block has no beforeEach of its own, so the KPI block's mock state
  // leaks in — including a resolved apiGetMock and whatever hasPermission was
  // last set to. Reset explicitly rather than inheriting someone else's setup.
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    apiPatchMock.mockReset();
    apiPostQueuedMock.mockReset();
    apiPostQueuedMock.mockResolvedValue({});
    hasPermissionMock.mockReset();
    hasPermissionMock.mockReturnValue(true);
    permissionsLoadedRef.value = true;
    loadPermissionsMock.mockReset();
    loadPermissionsMock.mockImplementation(async () => {});
    toastAddMock.mockReset();
  });

  it('records a payment through the shared capture form, never a status PATCH', async () => {
    // The old implementation PATCHed {status:'Paid'} (422 — the schema forbids
    // status), then became a full-balance-only POST that stamped the UTC day.
    // It is now the shared PaymentCaptureForm: partial amounts, check #, cash
    // confirmation, tenant-zone date, and queued so no-signal cannot lose it.
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const src = readFileSync(join(__dirname, '..', 'MobileBillingView.vue'), 'utf8');

    expect(src).not.toMatch(/async function markPaid/);
    const start = src.indexOf('async function recordPayment');
    expect(start).toBeGreaterThan(-1);
    const body = src.slice(start, src.indexOf('\nonMounted', start));
    expect(body).not.toMatch(/api\.patch/);
    expect(body).toMatch(/api\.postQueued\(`\/api\/invoices\/\$\{detail\.value\.id\}\/payments`/);
    // The form owns the date now — the view must not re-derive one.
    expect(body).not.toMatch(/toISOString/);
  });

  it('waits for permissions before choosing a list (cold-load race)', async () => {
    // The audit finding this guards: dropping requiresPermission from the route
    // also dropped the router guard's `await loadPermissions()`. With
    // permissions unresolved at mount, hasPermission() answers false for
    // EVERYONE — so an admin would be served the technician list, get [], and
    // sit looking at "No open invoices" with nothing scheduled to re-fetch.
    hasPermissionMock.mockReturnValue(false);          // not resolved yet
    permissionsLoadedRef.value = false;
    loadPermissionsMock.mockImplementation(async () => {
      permissionsLoadedRef.value = true;
      hasPermissionMock.mockReturnValue(true);          // resolves to an office user
    });
    apiGetMock.mockResolvedValue([]);

    mount(MobileBillingView, { global: { stubs } });
    await flushPromises();

    expect(loadPermissionsMock).toHaveBeenCalled();
    const urls = apiGetMock.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/invoices');
    expect(urls).not.toContain('/api/mobile/invoices/open');
  });

  it('reports a duplicate 409 as already-recorded, never as a failure', async () => {
    // The money defect: a duplicate 409 means the payment IS on the invoice.
    // Rendering it as "Payment failed" is what makes an operator re-enter money
    // the server just protected, once the dedupe window closes.
    apiGetMock.mockResolvedValue([]);
    const dup = new Error('an identical cash payment of 100.00 was recorded moments ago');
    dup.status = 409;
    dup.code = 'duplicate_payment';
    apiPostQueuedMock.mockRejectedValue(dup);

    const w = mount(MobileBillingView, { global: { stubs } });
    await flushPromises();
    w.vm.detail = { id: 'inv-1', status: 'sent', balance_due: 100, total: 100 };
    await flushPromises();
    await w.vm.recordPayment({ amount: 100, method: 'Cash', date: '2026-08-13', reference: null });
    await flushPromises();

    const sev = toastAddMock.mock.calls.map((c) => c[0].severity);
    expect(sev).toContain('info');
    expect(sev).not.toContain('error');
  });

  it('a technician reads the tech-scoped list and never the office KPI endpoint', async () => {
    // A technician has NO invoices permission, so /api/invoices and
    // /api/invoices/summary both 403. Asking anyway is a guaranteed error toast
    // on a screen that should just work.
    hasPermissionMock.mockReturnValue(false);
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/mobile/invoices/open') {
        return Promise.resolve({ invoices: [{ id: 'i1', invoice_number: 'INV-1', total: 100, balance_due: 100, status: 'sent' }] });
      }
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });

    mount(MobileBillingView, { global: { stubs } });
    await flushPromises();

    const urls = apiGetMock.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/mobile/invoices/open');
    expect(urls).not.toContain('/api/invoices');
    expect(urls).not.toContain('/api/invoices/summary');
  });

  it('Text sends through the endpoint the viewer is allowed to use', async () => {
    // A technician has no invoices.send, so the office /api/invoices/{id}/send-sms
    // would 403 — the tech texts through /api/mobile/invoices (own jobs,
    // office-verified only). Office tiers keep the office endpoint.
    for (const [office, base] of [[true, '/api/invoices'], [false, '/api/mobile/invoices']]) {
      hasPermissionMock.mockReturnValue(office);
      apiGetMock.mockResolvedValue(office ? [] : { invoices: [] });
      apiPostMock.mockResolvedValue({ to: null, body: '', blocked: null });
      const w = mount(MobileBillingView, { global: { stubs } });
      await flushPromises();
      w.vm.detail = { id: 'inv-1', status: 'sent', balance_due: 100, total: 100 };
      await flushPromises();
      expect(w.findComponent({ name: 'SmsLinkDialog' }).props('base')).toBe(base);
      w.unmount();
    }
  });
});

describe('MobileBillingView detail totals show credits (GDXA-625)', () => {
  // GET /api/invoices/{id} emits credit_total because the balance is
  // total - paid - credits. A totals block that shows Paid and Balance but
  // not Credits reads "Total 1000 / Paid 300 / Balance 0" with $700 missing.
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    hasPermissionMock.mockReset();
    hasPermissionMock.mockReturnValue(true);
    permissionsLoadedRef.value = true;
    loadPermissionsMock.mockReset();
    loadPermissionsMock.mockImplementation(async () => {});
  });

  async function openWith(detail) {
    apiGetMock.mockImplementation((url) => {
      if (url === `/api/invoices/${detail.id}`) return Promise.resolve(detail);
      if (url === '/api/invoices') return Promise.resolve([{ ...detail }]);
      return Promise.resolve({});
    });
    const w = mount(MobileBillingView, { global: { stubs } });
    await flushPromises();
    await w.vm.openDetail({ id: detail.id });
    await flushPromises();
    return w;
  }

  it('renders Total and a Credits row that reconciles to the balance', async () => {
    const w = await openWith({
      id: 'inv-c', status: 'partial', subtotal: 1000, tax_amount: 0,
      total: 1000, amount_paid: 300, credit_total: 700, balance_due: 0,
    });
    expect(w.find('[data-test="mb-total"]').text()).toContain('1000.00');
    const credits = w.find('[data-test="mb-credits"]');
    expect(credits.exists()).toBe(true);
    expect(credits.text()).toContain('Credits');
    expect(credits.text()).toContain('700.00');
    expect(w.find('.total-grand').text()).toContain('0.00');
  });

  it('renders no Credits row when the invoice has no credits', async () => {
    const w = await openWith({
      id: 'inv-n', status: 'sent', subtotal: 500, tax_amount: 0,
      total: 500, amount_paid: 100, credit_total: 0, balance_due: 400,
    });
    expect(w.find('[data-test="mb-total"]').text()).toContain('500.00');
    expect(w.find('[data-test="mb-credits"]').exists()).toBe(false);
    expect(w.find('.total-grand').text()).toContain('400.00');
  });

  it('subtracts credits when the server sent no balance_due', async () => {
    const w = await openWith({
      id: 'inv-l', status: 'sent', total: 1000, amount_paid: 300, credit_total: 200,
    });
    expect(w.find('.total-grand').text()).toContain('500.00');
  });
});

describe('MobileBillingView overdue is the server\'s verdict (GDXA-653)', () => {
  const realTz = process.env.TZ;

  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    hasPermissionMock.mockReset();
    // The technician path: no office summary, so the Overdue KPI is the
    // client-side sum over the list — the fallback that must use the same rule.
    hasPermissionMock.mockReturnValue(false);
    permissionsLoadedRef.value = true;
    process.env.TZ = 'America/Chicago';
    // 8pm Central on Oct 11. `new Date('2026-10-12')` is UTC midnight, i.e.
    // 7pm Central on Oct 11 — an hour EARLIER than now — so the old browser
    // comparison called a bill due tomorrow overdue tonight.
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-11T20:00:00-05:00'));
  });

  afterEach(() => {
    vi.useRealTimers();
    process.env.TZ = realTz;
  });

  function mountWith(invoices) {
    apiGetMock.mockImplementation((url) => {
      if (url === '/api/mobile/invoices/open') return Promise.resolve({ invoices });
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    return mount(MobileBillingView, { global: { stubs } });
  }

  it('an invoice due tomorrow is not overdue at 8pm Central tonight', async () => {
    const w = mountWith([{
      id: 'i1', invoice_number: 'INV-1', total: 300, balance_due: 300,
      status: 'sent', effective_status: 'sent', due_date: '2026-10-12',
    }]);
    await flushPromises();

    expect(w.findAll('.inv-card.overdue')).toHaveLength(0);
    expect(w.find('.overdue-flag').exists()).toBe(false);
    const overdueKpi = w.findAll('.kpi')[1];
    expect(overdueKpi.classes()).not.toContain('alert');
    expect(overdueKpi.text()).toContain('0.00');
  });

  it('flags and sums only what the server called overdue', async () => {
    const w = mountWith([
      {
        id: 'i1', invoice_number: 'INV-1', total: 125, balance_due: 125,
        status: 'sent', effective_status: 'overdue', due_date: '2026-10-01',
      },
      {
        id: 'i2', invoice_number: 'INV-2', total: 300, balance_due: 300,
        status: 'sent', effective_status: 'sent', due_date: '2026-10-12',
      },
    ]);
    await flushPromises();

    expect(w.findAll('.inv-card.overdue')).toHaveLength(1);
    expect(w.findAll('.kpi')[1].text()).toContain('125.00');
  });
});
