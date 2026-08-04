/**
 * Dashboard "Needs Attention" × the exception queues added 2026-08-04.
 *
 * Each queue follows the ready-for-billing pattern: a best-effort fetch whose
 * failure (403 for an ungranted role, network) must silently drop the entry —
 * never toast, never take the dashboard down. These tests pin that contract
 * per queue, plus the copy (count + singular/plural).
 */
import { flushPromises, mount } from '@vue/test-utils';
import { describe, expect, it, vi } from 'vitest';
import { reactive } from 'vue';

const mockGet = vi.fn();
const mockAuth = {
  isAdmin: true,
  role: 'admin',
  loadPermissions: vi.fn().mockResolvedValue(new Set()),
  // Per-test override point: the parts queue checks inventory.read before
  // fetching (it is not default-granted to dispatcher/sales/accounting).
  hasPermission: vi.fn().mockReturnValue(true),
};

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-08-04' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mockAuth,
}));
// The unread stores are the sidebar-badge singletons; the dashboard reads the
// same counts. reactive() so the attentionItems computed tracks count changes
// exactly like the real pinia store. Reset in mountDashboard.
const mockEmailUnread = reactive({ count: 0, fetchCount: vi.fn().mockResolvedValue(undefined) });
const mockSmsUnread = reactive({ count: 0, fetchCount: vi.fn().mockResolvedValue(undefined) });
vi.mock('../../stores/emailUnread', () => ({
  useEmailUnreadStore: () => mockEmailUnread,
}));
vi.mock('../../stores/smsUnread', () => ({
  useSmsUnreadStore: () => mockSmsUnread,
}));

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

// One return-visit row as /api/jobs/return-visits-unscheduled serializes it.
const RETURN_VISIT = {
  id: 'a1b2c3d4-0000-0000-0000-000000000001',
  title: 'Return visit: Broken torsion spring',
  description: 'Spring on order — needs a second trip to install.',
  customer_name: 'Ana Winters',
  job_number: 'JOB-2026-101',
  parent_job_id: 'a1b2c3d4-0000-0000-0000-000000000002',
  created_at: '2026-08-01T15:00:00Z',
};

function mountDashboard(routes, { reject = [] } = {}) {
  mockGet.mockReset();
  mockEmailUnread.count = 0;
  mockSmsUnread.count = 0;
  mockGet.mockImplementation((url) => {
    const path = url.split('?')[0];
    if (reject.includes(path)) return Promise.reject(new Error('403'));
    if (path in routes) return Promise.resolve(routes[path]);
    return Promise.resolve(null);
  });
  return mount(DashboardView, { global: { stubs } });
}

function attentionTexts(w) {
  return w
    .findAll('[data-testid="needs-attention"] .attention-item')
    .map((n) => n.text());
}

describe('dashboard attention queue: return visits', () => {
  it('renders the count with a plural when return visits await scheduling', async () => {
    const w = mountDashboard({
      '/api/jobs/return-visits-unscheduled': [RETURN_VISIT, { ...RETURN_VISIT, id: 'x2' }],
    });
    await flushPromises();
    const texts = attentionTexts(w);
    expect(
      texts.some((t) => t.includes('2 return visits from completed jobs awaiting scheduling')),
    ).toBe(true);
  });

  it('uses the singular for one', async () => {
    const w = mountDashboard({ '/api/jobs/return-visits-unscheduled': [RETURN_VISIT] });
    await flushPromises();
    expect(
      attentionTexts(w).some((t) => t.includes('1 return visit from completed jobs awaiting scheduling')),
    ).toBe(true);
  });

  it('renders nothing when the queue is empty', async () => {
    const w = mountDashboard({ '/api/jobs/return-visits-unscheduled': [] });
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('return visit'))).toBe(false);
  });

  it('a failing fetch (403 role) drops the entry without taking the dashboard down', async () => {
    const w = mountDashboard(
      {},
      { reject: ['/api/jobs/return-visits-unscheduled'] },
    );
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('return visit'))).toBe(false);
    expect(w.find('[data-testid="recent-activity-list"]').exists()).toBe(true);
  });
});

describe('dashboard attention queue: unread comms', () => {
  it('shows unread email and SMS entries from the sidebar-badge stores', async () => {
    const w = mountDashboard({});
    mockEmailUnread.count = 3;
    mockSmsUnread.count = 1;
    await flushPromises();
    const texts = attentionTexts(w);
    expect(texts.some((t) => t.includes('3 unread emails in the inbox'))).toBe(true);
    expect(texts.some((t) => t.includes('1 unread text message'))).toBe(true);
  });

  it('renders neither entry at zero', async () => {
    const w = mountDashboard({});
    await flushPromises();
    const texts = attentionTexts(w);
    expect(texts.some((t) => t.includes('unread'))).toBe(false);
  });

  it('refreshes both counts on dashboard load', async () => {
    mockEmailUnread.fetchCount.mockClear();
    mockSmsUnread.fetchCount.mockClear();
    mountDashboard({});
    await flushPromises();
    expect(mockEmailUnread.fetchCount).toHaveBeenCalledTimes(1);
    expect(mockSmsUnread.fetchCount).toHaveBeenCalledTimes(1);
  });

  it('hides the SMS row without nav.office — the count is tenant-wide and the sidebar hides the pin behind that permission', async () => {
    mockAuth.hasPermission.mockImplementation((key) => key !== 'nav.office');
    try {
      const w = mountDashboard({});
      mockSmsUnread.count = 4;
      mockEmailUnread.count = 2;
      await flushPromises();
      const texts = attentionTexts(w);
      expect(texts.some((t) => t.includes('text message'))).toBe(false);
      // Email stays: its count is per-viewer filtered server-side.
      expect(texts.some((t) => t.includes('2 unread emails'))).toBe(true);
    } finally {
      mockAuth.hasPermission.mockReturnValue(true);
    }
  });

  it('info rows sort below danger/warn — unread mail is a steady state, a critical part is a stuck tech', async () => {
    const w = mountDashboard({
      '/api/parts-needed/pending': [PART({ urgency: 'critical' })],
      '/api/jobs/return-visits-unscheduled': [RETURN_VISIT],
    });
    mockEmailUnread.count = 5;
    await flushPromises();
    const texts = attentionTexts(w);
    const idxParts = texts.findIndex((t) => t.includes('awaiting order'));
    const idxRv = texts.findIndex((t) => t.includes('return visit'));
    const idxEmail = texts.findIndex((t) => t.includes('unread emails'));
    expect(idxParts).toBeGreaterThanOrEqual(0);
    expect(idxRv).toBeGreaterThanOrEqual(0);
    expect(idxEmail).toBeGreaterThan(idxParts);
    expect(idxEmail).toBeGreaterThan(idxRv);
  });
});

describe('dashboard attention queue: website leads', () => {
  it('counts status=new landing leads and asks for the first call', async () => {
    const w = mountDashboard({
      '/api/landing-leads': [{ id: 'l1', status: 'new' }, { id: 'l2', status: 'new' }],
    });
    await flushPromises();
    expect(
      attentionTexts(w).some((t) => t.includes('2 new website leads waiting for a first call')),
    ).toBe(true);
    const call = mockGet.mock.calls.find(([url]) => url.startsWith('/api/landing-leads'));
    expect(call[0]).toContain('status=new');
  });

  it('renders the capped count as a floor ("100+") — the fetch stops at 100 rows', async () => {
    const w = mountDashboard({
      '/api/landing-leads': Array.from({ length: 100 }, (_, i) => ({ id: `l${i}`, status: 'new' })),
    });
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('100+ new website leads'))).toBe(true);
  });

  it('does not fetch without leads.read, and survives a failing fetch', async () => {
    mockAuth.hasPermission.mockImplementation((key) => key !== 'leads.read');
    try {
      const w1 = mountDashboard({ '/api/landing-leads': [{ id: 'l1' }] });
      await flushPromises();
      expect(attentionTexts(w1).some((t) => t.includes('website lead'))).toBe(false);
      expect(mockGet.mock.calls.some(([url]) => url.startsWith('/api/landing-leads'))).toBe(false);
    } finally {
      mockAuth.hasPermission.mockReturnValue(true);
    }
    const w2 = mountDashboard({}, { reject: ['/api/landing-leads'] });
    await flushPromises();
    expect(attentionTexts(w2).some((t) => t.includes('website lead'))).toBe(false);
    expect(w2.find('[data-testid="recent-activity-list"]').exists()).toBe(true);
  });
});

// Minimal cash-risk payload so the Cash & Risk card (home of the A/P tile)
// renders. Shape mirrors /api/reports/cash-risk.
const CASH_RISK = {
  ar_aging: {
    buckets: {
      current: { label: 'Current (0-30)', count: 0, total: 0 },
    },
    total_outstanding: 0,
  },
  gross_margin: { margin_pct: null, total_sell: 0, total_cost: 0, net_profit: 0, estimates_with_manual_lines: 0, window_days: 30 },
  warranty_callbacks: { rate: null, filed: 0, completed_jobs: 0, window_days: 30 },
};

// Rows as /api/vendor-invoices serializes them (InvoiceSummaryOut; Decimal
// totals arrive as strings).
const BILL = (over = {}) => ({
  id: `vb-${Math.random()}`,
  vendor_name_raw: 'Midland Door Solutions',
  invoice_number: 'INV-99',
  total: '100.00',
  status: 'open',
  due_date: null,
  reviewed_at: null,
  ...over,
});

describe('dashboard attention queue: vendor bills', () => {
  it('counts bills awaiting review — and only OPEN ones (status=open is load-bearing)', async () => {
    const w = mountDashboard({
      '/api/vendor-invoices': [BILL(), BILL()],
      '/api/vendor-invoices/payables': [],
    });
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('2 vendor bills awaiting review'))).toBe(true);
    // Without status=open, paid-but-never-line-confirmed bills count forever
    // ("Mark paid" doesn't stamp reviewed_at) and the badge is permanent.
    const call = mockGet.mock.calls.find(([url]) => url.startsWith('/api/vendor-invoices?'));
    expect(call[0]).toContain('needs_review=true');
    expect(call[0]).toContain('status=open');
  });

  it('sums open payables into the Cash & Risk A/P tile; next due is computed, not order-dependent', async () => {
    const w = mountDashboard({
      '/api/reports/cash-risk': CASH_RISK,
      '/api/vendor-invoices': [],
      '/api/vendor-invoices/payables': [
        // Deliberately NOT soonest-first — the client must not trust order.
        BILL({ total: '49.99', due_date: '2026-08-20' }),
        BILL({ total: '150.00', due_date: '2026-08-10' }),
        BILL({ total: '0.01' }),
      ],
    });
    await flushPromises();
    const tile = w.get('[data-testid="ap-open"]');
    expect(tile.text()).toBe('$200');
    expect(w.text()).toContain('3 open bills');
    expect(w.text()).toContain('next due 2026-08-10');
  });

  it('does not fetch without vendor_invoices.read', async () => {
    mockAuth.hasPermission.mockImplementation((key) => key !== 'vendor_invoices.read');
    try {
      const w = mountDashboard({
        '/api/reports/cash-risk': CASH_RISK,
        '/api/vendor-invoices': [BILL()],
        '/api/vendor-invoices/payables': [BILL()],
      });
      await flushPromises();
      expect(attentionTexts(w).some((t) => t.includes('vendor bill'))).toBe(false);
      expect(w.find('[data-testid="ap-open"]').exists()).toBe(false);
      expect(mockGet.mock.calls.some(([url]) => url.startsWith('/api/vendor-invoices'))).toBe(false);
    } finally {
      mockAuth.hasPermission.mockReturnValue(true);
    }
  });

  it('a failing fetch hides both surfaces without taking the dashboard down', async () => {
    const w = mountDashboard(
      { '/api/reports/cash-risk': CASH_RISK },
      { reject: ['/api/vendor-invoices', '/api/vendor-invoices/payables'] },
    );
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('vendor bill'))).toBe(false);
    expect(w.find('[data-testid="ap-open"]').exists()).toBe(false);
    expect(w.find('[data-testid="recent-activity-list"]').exists()).toBe(true);
  });
});

// Rows as /api/parts-needed/pending serializes them (status needed|ordered,
// urgency normal|urgent|critical).
const PART = (over = {}) => ({
  id: `part-${Math.random()}`,
  part_name: 'Torsion spring 0.250x2x33',
  status: 'needed',
  urgency: 'normal',
  ...over,
});

describe('dashboard Cash & Risk: collected tile', () => {
  it('renders 30d collected with count and today subtotal', async () => {
    const w = mountDashboard({
      '/api/reports/cash-risk': {
        ...CASH_RISK,
        collected: { total: 4650.5, count: 7, today_total: 400, window_days: 30 },
      },
    });
    await flushPromises();
    expect(w.get('[data-testid="collected-30d"]').text()).toBe('$4,651');
    expect(w.text()).toContain('7 payments');
    expect(w.text()).toContain('$400 today');
  });

  it('an older server payload without `collected` renders $0, not a crash', async () => {
    const w = mountDashboard({ '/api/reports/cash-risk': CASH_RISK });
    await flushPromises();
    expect(w.get('[data-testid="collected-30d"]').text()).toBe('$0');
  });
});

describe('dashboard attention queue: parts to order', () => {
  it('counts only status=needed — ordered rows are the supplier\'s queue, not ours', async () => {
    const w = mountDashboard({
      '/api/parts-needed/pending': [PART(), PART(), PART({ status: 'ordered' })],
    });
    await flushPromises();
    const texts = attentionTexts(w);
    expect(texts.some((t) => t.includes('2 parts awaiting order'))).toBe(true);
    expect(texts.some((t) => t.includes('critical'))).toBe(false);
  });

  it('calls out criticals — a critical part is a tech stuck on a job', async () => {
    const w = mountDashboard({
      '/api/parts-needed/pending': [PART({ urgency: 'critical' }), PART()],
    });
    await flushPromises();
    expect(attentionTexts(w).some((t) => t.includes('2 parts awaiting order — 1 critical'))).toBe(true);
  });

  it('names urgent parts — the strongest signal a closeout can send', async () => {
    const w = mountDashboard({
      '/api/parts-needed/pending': [
        PART({ urgency: 'critical' }),
        PART({ urgency: 'urgent' }),
        PART({ urgency: 'urgent' }),
      ],
    });
    await flushPromises();
    expect(
      attentionTexts(w).some((t) => t.includes('3 parts awaiting order — 1 critical, 2 urgent')),
    ).toBe(true);
  });

  it('does not fetch at all without inventory.read — no guaranteed 403 noise', async () => {
    mockAuth.hasPermission.mockImplementation((key) => key !== 'inventory.read');
    try {
      const w = mountDashboard({
        '/api/parts-needed/pending': [PART()],
      });
      await flushPromises();
      expect(attentionTexts(w).some((t) => t.includes('awaiting order'))).toBe(false);
      expect(mockGet.mock.calls.some(([url]) => url.startsWith('/api/parts-needed'))).toBe(false);
    } finally {
      mockAuth.hasPermission.mockReturnValue(true);
    }
  });

  it('a critical ONLY counts while still needed (ordered critical is handled)', async () => {
    const w = mountDashboard({
      '/api/parts-needed/pending': [PART({ urgency: 'critical', status: 'ordered' }), PART()],
    });
    await flushPromises();
    const texts = attentionTexts(w);
    expect(texts.some((t) => t.includes('1 part awaiting order'))).toBe(true);
    expect(texts.some((t) => t.includes('critical'))).toBe(false);
  });

  it('renders nothing when the queue is empty and survives a failing fetch', async () => {
    const w1 = mountDashboard({ '/api/parts-needed/pending': [] });
    await flushPromises();
    expect(attentionTexts(w1).some((t) => t.includes('awaiting order'))).toBe(false);

    const w2 = mountDashboard({}, { reject: ['/api/parts-needed/pending'] });
    await flushPromises();
    expect(attentionTexts(w2).some((t) => t.includes('awaiting order'))).toBe(false);
    expect(w2.find('[data-testid="recent-activity-list"]').exists()).toBe(true);
  });
});
