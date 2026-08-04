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
const mockAuth = { isAdmin: true, role: 'admin' };

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
