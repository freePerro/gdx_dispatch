/**
 * Dashboard "Needs Attention" × persisted next-actions.
 *
 * GET /api/next-actions existed since PR5 but NOTHING in the SPA ever called
 * it — found 2026-08-04 with $13K of uncollected invoices and 12 unbilled
 * jobs sitting invisible in the table for weeks. These tests pin:
 *   1. persisted rows render inside the Needs Attention card
 *   2. ephemeral "auto:" rows are excluded (their content is already
 *      summarized by the client-computed lines, and they can't be
 *      completed/snoozed server-side)
 *   3. Done POSTs /complete and removes the row
 *   4. Snooze POSTs /snooze with a future `until` and removes the row
 *   5. a failing next-actions fetch never takes the dashboard down
 */
import { flushPromises, mount } from '@vue/test-utils';
import { describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockAuth = { isAdmin: true, role: 'admin' };

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: mockPost, patch: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-08-04' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => mockAuth,
}));
vi.mock('../../stores/emailUnread', () => ({
  useEmailUnreadStore: () => ({ count: 0, fetchCount: vi.fn().mockResolvedValue(undefined) }),
}));
vi.mock('../../stores/smsUnread', () => ({
  useSmsUnreadStore: () => ({ count: 0, fetchCount: vi.fn().mockResolvedValue(undefined) }),
}));

import DashboardView from '../DashboardView.vue';

const passthrough = { template: '<div><slot /><slot name="title" /><slot name="content" /></div>' };
const stubs = {
  Card: passthrough,
  Button: {
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\', $event)"><slot /></button>',
    inheritAttrs: false,
  },
  Skeleton: true,
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: true,
  InputText: true,
  Textarea: true,
  Select: true,
  PhoneInput: true,
};

const BILLING_NAG = {
  id: 'b8d9a112-0000-0000-0000-000000000001',
  action_type: 'billing_followup',
  title: 'Billing follow-up: work is waiting to be billed',
  description: 'Money is sitting in the pipeline: 12 completed job(s) unbilled >3d.',
  priority: 'high',
  action_url: '/billing',
  status: 'pending',
};

const AUTO_ROW = {
  id: 'auto:call_overdue_invoice:inv-1',
  action_type: 'call_overdue_invoice',
  title: 'Call on Overdue Invoice #INV-000315',
  priority: 'high',
  status: 'pending',
};

function mountWithActions(rows, { failFetch = false } = {}) {
  mockGet.mockReset();
  mockPost.mockReset().mockResolvedValue({ status: 'ok' });
  mockGet.mockImplementation((url) => {
    if (url.startsWith('/api/next-actions')) {
      return failFetch ? Promise.reject(new Error('boom')) : Promise.resolve(rows);
    }
    return Promise.resolve(null);
  });
  return mount(DashboardView, { global: { stubs } });
}

describe('dashboard next-actions widget', () => {
  it('renders persisted rows in the Needs Attention card', async () => {
    const w = mountWithActions([BILLING_NAG]);
    await flushPromises();
    const card = w.get('[data-testid="needs-attention"]');
    const rows = card.findAll('[data-testid="next-action-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('work is waiting to be billed');
    expect(rows[0].text()).toContain('12 completed job(s)');
    expect(rows[0].text()).toContain('Billing'); // mapped tag label
  });

  it('excludes ephemeral auto: rows', async () => {
    const w = mountWithActions([AUTO_ROW, BILLING_NAG]);
    await flushPromises();
    const rows = w.findAll('[data-testid="next-action-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).not.toContain('INV-000315');
  });

  it('Done completes the action and removes the row', async () => {
    const w = mountWithActions([BILLING_NAG]);
    await flushPromises();
    await w.get('[data-testid="next-action-done"]').trigger('click');
    await flushPromises();
    expect(mockPost).toHaveBeenCalledWith(
      `/api/next-actions/${BILLING_NAG.id}/complete`,
      {},
      expect.anything(),
    );
    expect(w.findAll('[data-testid="next-action-row"]')).toHaveLength(0);
  });

  it('Snooze defers a week out and removes the row', async () => {
    const w = mountWithActions([BILLING_NAG]);
    await flushPromises();
    await w.get('[data-testid="next-action-snooze"]').trigger('click');
    await flushPromises();
    const call = mockPost.mock.calls.find((c) => c[0].endsWith('/snooze'));
    expect(call).toBeTruthy();
    const until = new Date(call[1].until);
    expect(until.getTime()).toBeGreaterThan(Date.now() + 6 * 24 * 60 * 60 * 1000);
    expect(w.findAll('[data-testid="next-action-row"]')).toHaveLength(0);
  });

  it('a failing fetch renders no rows and no crash', async () => {
    const w = mountWithActions([], { failFetch: true });
    await flushPromises();
    expect(w.findAll('[data-testid="next-action-row"]')).toHaveLength(0);
    // The rest of the dashboard still mounted.
    expect(w.find('[data-testid="recent-activity-list"]').exists()).toBe(true);
  });
});
