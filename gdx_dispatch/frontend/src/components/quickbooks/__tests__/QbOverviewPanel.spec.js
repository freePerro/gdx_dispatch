/**
 * QbOverviewPanel — Overview tab on /quickbooks. Reads /api/qb/dashboard
 * via the parent and renders connection state, error banner, entity counts,
 * and a per-entity sync split-button. Covers: entity-row mapping from
 * status.entity_counts, error-banner visibility, sync-entity emit, disabled
 * sync button when disconnected.
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import QbOverviewPanel from '../QbOverviewPanel.vue';


const STUBS = {
  Tag: {
    template: '<span class="stub-tag" :data-severity="severity">{{ value }}</span>',
    props: ['value', 'severity'],
  },
  ProgressSpinner: { template: '<div class="stub-spinner" />' },
  Button: {
    template:
      '<button class="stub-btn" :disabled="disabled" :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\', $event)"><slot /></button>',
    props: ['icon', 'size', 'severity', 'disabled', 'label'],
    inheritAttrs: false,
  },
};


function makeStatus(overrides = {}) {
  return {
    connected: true,
    last_sync_at: null,
    realm_id: null,
    error_count: 0,
    last_error: null,
    entity_counts: {},
    delete_sync_enabled: false,
    ...overrides,
  };
}


describe('QbOverviewPanel', () => {
  it('renders 5 entity rows mapping entity_counts keys to labels', () => {
    const status = makeStatus({
      entity_counts: { customer: 3, invoice: 5, item: 2, account: 7, bank_transaction: 12 },
    });
    const wrapper = mount(QbOverviewPanel, {
      props: { status, loading: false },
      global: { stubs: STUBS },
    });
    const rows = wrapper.findAll('[data-testid^="qb-overview-row-"]');
    expect(rows).toHaveLength(5);
    expect(wrapper.text()).toContain('Customers');
    expect(wrapper.text()).toContain('Chart of Accounts');
    expect(wrapper.text()).toContain('Banking');
    // Counts render verbatim.
    expect(wrapper.text()).toContain('3');
    expect(wrapper.text()).toContain('12');
  });

  it('shows the error banner only when status.last_error is set', () => {
    const wrapper = mount(QbOverviewPanel, {
      props: { status: makeStatus(), loading: false },
      global: { stubs: STUBS },
    });
    expect(wrapper.find('[data-testid="qb-overview-error-banner"]').exists()).toBe(false);

    wrapper.setProps({
      status: makeStatus({ last_error: 'OAuth token expired', error_count: 4 }),
      loading: false,
    });
    return wrapper.vm.$nextTick().then(() => {
      const banner = wrapper.find('[data-testid="qb-overview-error-banner"]');
      expect(banner.exists()).toBe(true);
      expect(banner.text()).toContain('OAuth token expired');
      expect(banner.text()).toContain('4');
    });
  });

  it('emits sync-entity with the entity key when a row sync button is clicked', async () => {
    const wrapper = mount(QbOverviewPanel, {
      props: { status: makeStatus({ entity_counts: { customer: 1 } }), loading: false },
      global: { stubs: STUBS },
    });
    await wrapper.find('[data-testid="qb-overview-sync-customers"]').trigger('click');
    const events = wrapper.emitted('sync-entity');
    expect(events).toBeTruthy();
    expect(events[0]).toEqual(['customers']);
  });

  it('disables the per-entity sync buttons when disconnected', () => {
    const wrapper = mount(QbOverviewPanel, {
      props: { status: makeStatus({ connected: false }), loading: false },
      global: { stubs: STUBS },
    });
    const buttons = wrapper.findAll('[data-testid^="qb-overview-sync-"]');
    expect(buttons.length).toBeGreaterThan(0);
    for (const b of buttons) {
      expect(b.attributes('disabled')).toBeDefined();
    }
  });

  it('shows the empty-state row when every entity count is zero', () => {
    const wrapper = mount(QbOverviewPanel, {
      props: { status: makeStatus(), loading: false },
      global: { stubs: STUBS },
    });
    expect(wrapper.text()).toContain('No entities synced yet');
  });

  it('renders the spinner while loading', () => {
    const wrapper = mount(QbOverviewPanel, {
      props: { status: makeStatus(), loading: true },
      global: { stubs: STUBS },
    });
    expect(wrapper.find('.stub-spinner').exists()).toBe(true);
  });
});
