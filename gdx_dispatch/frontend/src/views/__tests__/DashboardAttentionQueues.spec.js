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

// Rows as /api/parts-needed/pending serializes them (status needed|ordered,
// urgency normal|urgent|critical).
const PART = (over = {}) => ({
  id: `part-${Math.random()}`,
  part_name: 'Torsion spring 0.250x2x33',
  status: 'needed',
  urgency: 'normal',
  ...over,
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
