/**
 * Tests for the dashboard "Recent Activity" feed — activity-attribution Phase 1.
 *
 * The feed used to render only a title + timestamp: the `user_name` that
 * routers/audit.py already resolves for every row was dropped on the floor in
 * loadRecentActivity(), so prod showed rows like "Data Accessed (customer)"
 * with no indication of who did it. These tests pin:
 *   1. the actor reaches the rendered meta line
 *   2. a raw UUID actor (server-side resolution missed) is guarded, not dumped
 *   3. 'system' actors render honestly as "System" rather than blank
 *   4. the actions that dominate the prod feed have real labels instead of
 *      falling through to title-case + "(entity_type)"
 */
import { flushPromises, mount } from '@vue/test-utils';
import { describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
const mockAuth = { isAdmin: true, role: 'admin' };

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-07-28' }),
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
  Button: { template: '<button><slot /></button>' },
  Skeleton: true,
  Tag: { template: '<span><slot /></span>' },
  Dialog: true,
  InputText: true,
  Textarea: true,
  Select: true,
  PhoneInput: true,
};

/**
 * Route every dashboard fetch to null except /api/audit/logs, which returns the
 * supplied rows. null (not {}) is deliberate: the other cards gate their render
 * on a truthy payload, so an empty object would render them against absent data
 * and throw for reasons unrelated to the activity feed.
 */
function mountWithAuditRows(items) {
  mockAuth.isAdmin = true;
  mockAuth.role = 'admin';
  mockGet.mockReset();
  mockGet.mockImplementation((url) => {
    if (url.startsWith('/api/audit/logs')) return Promise.resolve({ items });
    return Promise.resolve(null);
  });
  return mount(DashboardView, { global: { stubs } });
}

function activityText(wrapper) {
  return wrapper.get('[data-testid="recent-activity-list"]').text();
}

describe('dashboard recent activity — actor attribution', () => {
  it('renders the resolved actor name in the row meta', async () => {
    const w = mountWithAuditRows([
      {
        id: 'a1',
        action: 'data_accessed',
        entity_type: 'customer',
        entity_id: 'c-1',
        user_id: 'u-1',
        user_name: 'Amber Joy',
        created_at: '2026-07-28T18:21:40Z',
      },
    ]);
    await flushPromises();
    expect(activityText(w)).toContain('Amber Joy');
  });

  it('labels data_accessed instead of falling through to "Data Accessed (customer)"', async () => {
    const w = mountWithAuditRows([
      {
        id: 'a1',
        action: 'data_accessed',
        entity_type: 'customer',
        entity_id: 'c-1',
        user_id: 'u-1',
        user_name: 'Amber Joy',
        created_at: '2026-07-28T18:21:40Z',
      },
    ]);
    await flushPromises();
    const text = activityText(w);
    expect(text).toContain('Customer record viewed');
    expect(text).not.toContain('Data Accessed (customer)');
  });

  it('guards a raw UUID actor rather than rendering a 36-char wall', async () => {
    const w = mountWithAuditRows([
      {
        id: 'a2',
        action: 'job_updated',
        entity_type: 'job',
        entity_id: 'j-1',
        // audit.py falls back to the raw id when the users lookup misses
        user_id: '1f23a32a-198e-4a2d-90b7-4998c845790e',
        user_name: '1f23a32a-198e-4a2d-90b7-4998c845790e',
        created_at: '2026-07-28T18:21:17Z',
      },
    ]);
    await flushPromises();
    const text = activityText(w);
    expect(text).toContain('Unknown user (1f23a32a)');
    expect(text).not.toContain('1f23a32a-198e-4a2d-90b7-4998c845790e');
  });

  it('renders a system-attributed row honestly as System', async () => {
    // Most prod rows are 'system' because the writing handler never recorded
    // an actor (Phase 3). Showing "System" makes that visible; blank hides it.
    const w = mountWithAuditRows([
      {
        id: 'a3',
        action: 'patch_invoice',
        entity_type: 'invoice',
        entity_id: 'i-1',
        user_id: 'system',
        user_name: 'system',
        created_at: '2026-07-28T18:15:07Z',
      },
    ]);
    await flushPromises();
    const text = activityText(w);
    expect(text).toContain('System');
    expect(text).toContain('Invoice updated');
  });

  it('labels a staff-entered lead neutrally, not as a website capture', async () => {
    // landing_lead_created also fires from the authenticated staff route
    // (routers/leads.py), so the label must be true for both origins.
    const w = mountWithAuditRows([
      {
        id: 'a5',
        action: 'landing_lead_created',
        entity_type: 'landing_lead',
        entity_id: 'l-1',
        user_id: 'u-1',
        user_name: 'Office',
        created_at: '2026-07-28T18:00:00Z',
      },
    ]);
    await flushPromises();
    const text = activityText(w);
    expect(text).toContain('Lead captured');
    expect(text).not.toContain('New website lead');
  });

  it('still renders a timestamp alongside the actor', async () => {
    const w = mountWithAuditRows([
      {
        id: 'a4',
        action: 'estimate_created',
        entity_type: 'estimate',
        entity_id: 'e-1',
        user_id: 'u-1',
        user_name: 'Dispatch',
        created_at: '2026-07-28T18:21:40Z',
      },
    ]);
    await flushPromises();
    const text = activityText(w);
    // Deliberately does NOT assert a formatted date: Intl output depends on
    // the runner's TZ and locale, and 18:21Z is already the 29th east of
    // UTC+6. Assert the shape — actor, separator, non-empty timestamp — and
    // that we did not fall through to formatTimestamp's invalid-date path.
    expect(text).toContain('Dispatch');
    expect(text).toMatch(/Dispatch\s+·\s+\S/);
    expect(text).not.toContain('Updated recently');
  });
});

describe('dashboard recent activity — non-admin fallback path', () => {
  /**
   * Non-admins never get /api/audit/logs (it is admin/owner-only; asking would
   * be a guaranteed 403 on every dashboard load). They fall through to a
   * jobs-derived feed, which carries no actor at all. This is a genuinely
   * different widget for a different role and was previously untested.
   */
  function mountAsNonAdmin(jobs) {
    mockAuth.isAdmin = false;
    mockAuth.role = 'technician';
    mockGet.mockReset();
    mockGet.mockImplementation((url) => {
      if (url === '/api/jobs') return Promise.resolve({ items: jobs });
      return Promise.resolve(null);
    });
    return mount(DashboardView, { global: { stubs } });
  }

  it('never calls the admin-only audit endpoint', async () => {
    mountAsNonAdmin([]);
    await flushPromises();
    const auditCalls = mockGet.mock.calls.filter(([url]) =>
      String(url).startsWith('/api/audit/logs'),
    );
    expect(auditCalls).toHaveLength(0);
  });

  it('falls back to a jobs-derived feed', async () => {
    const w = mountAsNonAdmin([
      {
        id: 'j-1',
        title: 'Spring replacement',
        customer_name: 'Acme Storage',
        updated_at: '2026-07-28T17:00:00Z',
      },
    ]);
    await flushPromises();
    const text = w.get('[data-testid="recent-activity-list"]').text();
    expect(text).toContain('Spring replacement');
    expect(text).toContain('Acme Storage');
  });
});
