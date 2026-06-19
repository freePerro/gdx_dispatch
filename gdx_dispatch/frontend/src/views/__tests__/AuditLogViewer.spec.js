/**
 * Tests for AuditLogViewer.vue — closes the test gap on the platform↔SS-28
 * field-name shim added in S111 (commit 8695debb). The platform audit
 * router serializes (event_type, actor_id, entity_type, entity_id) but the
 * viewer's template renders (action, principal_identity_id, resource_type,
 * resource_id). Without the shim the table reads "No audit rows match"
 * even with 10,153 rows in the DB. Tests:
 *   1. response shape `data.items` (platform) renders rows
 *   2. response shape `data.rows` (legacy/SS-28) renders rows
 *   3. platform field names map to SS-28 names in the rendered cells
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AuditLogViewer from '../AuditLogViewer.vue';

const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Button: { template: '<button><slot /></button>' },
};
const mountOpts = { global: { stubs } };

function mountWithBody(body) {
  return mount(AuditLogViewer, {
    ...mountOpts,
    props: { fetchFn: vi.fn().mockResolvedValue(body) },
  });
}

describe('AuditLogViewer', () => {
  it('renders rows from platform {items: [...]} shape with platform field names', async () => {
    // Platform router (admin_ops.py:402) returns event_type/actor_id/
    // entity_type — viewer template expects action/principal_identity_id/
    // resource_type. Shim must remap.
    const w = mountWithBody({
      page: 1,
      page_size: 50,
      total: 1,
      items: [
        {
          id: 'aud-1',
          event_type: 'job_created',
          actor_id: 'user-doug',
          entity_type: 'job',
          entity_id: 'job-100',
          payload: { ip_address: '10.0.0.1' },
          created_at: '2026-05-09T17:00:00Z',
        },
      ],
    });
    await flushPromises();
    const text = w.text();

    // Action column shows event_type via shim
    expect(text).toContain('job_created');
    // Resource column shows entity_type:entity_id
    expect(text).toContain('job:job-100');
    // Principal column shows actor_id
    expect(text).toContain('user-doug');
    // Empty-state must NOT render
    expect(text).not.toContain('No audit rows match');
  });

  it('still works on legacy {rows: [...]} response shape', async () => {
    const w = mountWithBody({
      rows: [
        {
          id: 'aud-2',
          action: 'invoice_sent',
          resource_type: 'invoice',
          resource_id: 'inv-200',
          principal_identity_id: 'user-jane',
          result: 'ok',
          ip_address: '10.0.0.2',
          created_at: '2026-05-09T18:00:00Z',
        },
      ],
      total: 1,
    });
    await flushPromises();
    const text = w.text();

    expect(text).toContain('invoice_sent');
    expect(text).toContain('invoice:inv-200');
    expect(text).toContain('user-jane');
    expect(text).not.toContain('No audit rows match');
  });

  it('shows empty state only when truly empty', async () => {
    const w = mountWithBody({ items: [], total: 0 });
    await flushPromises();
    expect(w.text()).toContain('No audit rows match');
  });
});
