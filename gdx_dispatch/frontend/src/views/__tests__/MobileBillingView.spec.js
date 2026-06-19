/**
 * MobileBillingView KPI strip tests — pins the S112 mobile parity fix.
 *
 * The strip shows Outstanding / Overdue / Paid (mo) at the top of the
 * mobile billing view. Server-prefer/client-fallback contract (S113):
 *   - When /api/invoices/summary returns numbers, those win
 *   - When the endpoint is unavailable, fall back to client computation
 *     over the loaded invoice list (drafts excluded from outstanding).
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import MobileBillingView from '../MobileBillingView.vue';

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ path: '/mobile/billing', fullPath: '/mobile/billing' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGetMock, post: apiPostMock, patch: apiPatchMock }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: () => Promise.resolve(true) }),
}));

const stubs = {
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
            id: 'i1', status: 'sent', balance_due: 200, total: 200,
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
    // Outstanding = 200 (drafts excluded). Overdue also = 200 (the sent
    // invoice is past due_date 2026-04-01 ≪ today 2026-05-09).
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
