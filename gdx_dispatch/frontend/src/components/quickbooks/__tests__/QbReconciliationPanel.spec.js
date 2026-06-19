/**
 * QbReconciliationPanel — Reconciliation tab on /quickbooks. Reads
 * /api/qb/events?action=qb_delete_sync + /api/qb/dashboard. Covers: banner
 * reflects delete_sync_enabled, table rows render qb_delete_sync entries,
 * empty-state copy switches based on flag.
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

import QbReconciliationPanel from '../QbReconciliationPanel.vue';

const STUBS = {
  Tag: {
    template: '<span class="stub-tag" :data-severity="severity">{{ value }}</span>',
    props: ['value', 'severity'],
  },
  ProgressSpinner: { template: '<div class="stub-spinner" />' },
  Button: {
    template:
      '<button class="stub-btn" :disabled="disabled" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\', $event)"><slot /></button>',
    props: ['icon', 'size', 'severity', 'disabled', 'label', 'loading'],
    inheritAttrs: false,
  },
  DataTable: {
    template:
      '<div class="stub-datatable"><template v-if="value.length"><div v-for="(row, i) in value" :key="i" class="stub-row" :data-row-index="i"><span class="stub-row-entity-id">{{ row.entity_id }}</span><span class="stub-row-action">{{ row.action }}</span></div></template><template v-else><slot name="empty" /></template></div>',
    props: ['value', 'paginator', 'rows', 'stripedRows'],
    inheritAttrs: false,
  },
  Column: { template: '<div></div>' },
};


function makeApi(events, dashboard) {
  return {
    get: vi.fn(async (url) => {
      if (url.startsWith('/api/qb/events')) return { events };
      if (url === '/api/qb/dashboard') return dashboard;
      throw new Error(`Unstubbed GET ${url}`);
    }),
    toast: { add: vi.fn() },
  };
}


// QbReconciliationPanel imports useApiWithToast as a default-style helper; we
// stub the module to return our fake api.
vi.mock('../../../composables/useApiWithToast', () => {
  return {
    useApiWithToast: () => globalThis.__qbReconcilFakeApi,
  };
});


describe('QbReconciliationPanel', () => {
  it('shows DISABLED banner + flag-off empty copy when delete_sync_enabled=false', async () => {
    globalThis.__qbReconcilFakeApi = makeApi([], { delete_sync_enabled: false });
    const wrapper = mount(QbReconciliationPanel, { global: { stubs: STUBS } });
    await flushPromises();

    const banner = wrapper.find('[data-testid="qb-reconciliation-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain('DISABLED');
    expect(wrapper.text()).toContain('flag is currently disabled');
  });

  it('shows ENABLED banner + neutral empty copy when delete_sync_enabled=true and no rows', async () => {
    globalThis.__qbReconcilFakeApi = makeApi([], { delete_sync_enabled: true });
    const wrapper = mount(QbReconciliationPanel, { global: { stubs: STUBS } });
    await flushPromises();

    const banner = wrapper.find('[data-testid="qb-reconciliation-banner"]');
    expect(banner.text()).toContain('ENABLED');
    expect(wrapper.text()).toContain('No reconciliation deletes recorded yet');
  });

  it('renders rows from /api/qb/events?action=qb_delete_sync', async () => {
    const events = [
      {
        timestamp: '2026-05-04T12:00:00Z',
        action: 'qb_delete_sync',
        entity_id: 'QB-C-100',
        details: { entity_type: 'customer', qb_id: 'QB-C-100', local_id: 'abc', reason: 'absent_from_full_set_diff' },
      },
      {
        timestamp: '2026-05-04T12:01:00Z',
        action: 'qb_delete_sync',
        entity_id: 'QB-I-200',
        details: { entity_type: 'invoice', qb_id: 'QB-I-200', local_id: 'def', reason: 'absent_from_full_set_diff' },
      },
    ];
    globalThis.__qbReconcilFakeApi = makeApi(events, { delete_sync_enabled: true });
    const wrapper = mount(QbReconciliationPanel, { global: { stubs: STUBS } });
    await flushPromises();

    const rows = wrapper.findAll('.stub-row');
    expect(rows).toHaveLength(2);
    expect(wrapper.text()).toContain('QB-C-100');
    expect(wrapper.text()).toContain('QB-I-200');
  });

  it('passes the right query string to /api/qb/events', async () => {
    globalThis.__qbReconcilFakeApi = makeApi([], { delete_sync_enabled: false });
    const wrapper = mount(QbReconciliationPanel, { global: { stubs: STUBS } });
    await flushPromises();
    const calls = globalThis.__qbReconcilFakeApi.get.mock.calls.map((c) => c[0]);
    expect(calls.some((u) => u.startsWith('/api/qb/events?action=qb_delete_sync'))).toBe(true);
    expect(calls).toContain('/api/qb/dashboard');
    wrapper.unmount();
  });
});
