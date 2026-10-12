/**
 * Dashboard's raw-data fallback counts overdue invoices from the server's
 * `effective_status`, never from `new Date(due_date) < now` (GDXA-652).
 *
 * A date-only due_date parses as UTC midnight, which is 7pm Central the
 * evening before. The clock below is 8pm Central on Oct 11 (01:00Z Oct 12),
 * so the old browser comparison called an invoice due Oct 12 overdue in
 * every timezone the test could run in.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { reactive } from 'vue';

const mockGet = vi.fn();
const mockAuth = {
  isAdmin: false,
  role: 'admin',
  loadPermissions: vi.fn().mockResolvedValue(new Set()),
  hasPermission: vi.fn().mockReturnValue(false),
};

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-10-11' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../stores/auth', () => ({ useAuthStore: () => mockAuth }));
const mockUnread = reactive({ count: 0, fetchCount: vi.fn().mockResolvedValue(undefined) });
vi.mock('../../stores/emailUnread', () => ({ useEmailUnreadStore: () => mockUnread }));
vi.mock('../../stores/smsUnread', () => ({ useSmsUnreadStore: () => mockUnread }));

import DashboardView from '../DashboardView.vue';

const passthrough = { template: '<div><slot /><slot name="title" /><slot name="content" /></div>' };
const stubs = {
  Card: passthrough,
  Button: true,
  Skeleton: true,
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: true,
  InputText: true,
  Textarea: true,
  Select: true,
  PhoneInput: true,
};

function mountWithInvoices(invoices) {
  mockGet.mockReset();
  mockGet.mockImplementation((url) => {
    const path = url.split('?')[0];
    // Reports endpoints return nothing, so the dashboard falls back to
    // counting from the raw /api/jobs and /api/invoices lists.
    if (path === '/api/jobs') return Promise.resolve([]);
    if (path === '/api/invoices') return Promise.resolve({ items: invoices });
    return Promise.resolve(null);
  });
  return mount(DashboardView, { global: { stubs } });
}

function overdueAttention(w) {
  return w
    .findAll('[data-testid="needs-attention"] .attention-item')
    .map((n) => n.text())
    .filter((t) => /overdue invoice/.test(t));
}

describe('DashboardView overdue invoices (raw-data fallback)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-10-12T01:00:00Z')); // 8pm CDT, Oct 11
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('does not count an invoice due tomorrow as overdue at 8pm Central', async () => {
    const w = mountWithInvoices([
      {
        id: 'inv-1',
        status: 'sent',
        effective_status: 'sent',
        due_date: '2026-10-12',
        balance_due: 500,
        total: 500,
      },
    ]);
    await flushPromises();
    expect(overdueAttention(w)).toEqual([]);
  });

  it('counts what the server calls overdue, and only that', async () => {
    const w = mountWithInvoices([
      // Server says overdue.
      { id: 'a', status: 'sent', effective_status: 'overdue', due_date: '2026-10-01', balance_due: 100, total: 100 },
      // Past due date but nothing owed: the server keeps it "sent".
      { id: 'b', status: 'sent', effective_status: 'sent', due_date: '2026-10-01', balance_due: 0, total: 100 },
      // Past due date but a draft: never overdue.
      { id: 'c', status: 'draft', effective_status: 'draft', due_date: '2026-10-01', balance_due: 100, total: 100 },
    ]);
    await flushPromises();
    expect(overdueAttention(w)).toEqual(['Overdue1 overdue invoices need collection']);
  });
});
