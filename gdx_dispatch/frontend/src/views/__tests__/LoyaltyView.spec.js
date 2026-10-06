/**
 * LoyaltyView reads the real points ledger (GDXA-316).
 *
 * It used to read GET /api/loyalty, a ui_compat stub that always answered an
 * empty list, and posted to /api/loyalty/adjust and /redeem, which never
 * existed. These tests pin the real endpoints and that a failed load says so
 * instead of rendering "No loyalty members".
 */
import { flushPromises, mount } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
const mockPost = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: mockPost }),
}));

import LoyaltyView from '../LoyaltyView.vue';

const passthrough = { template: '<div><slot /><slot name="start" /><slot name="end" /><slot name="footer" /></div>' };
const DataTable = {
  props: ['value'],
  template: `<div data-testid="table">
    <div v-for="row in value" :key="row.customer_id" class="row">{{ row.customer_name || row.customer_id }}|{{ row.points }}|{{ row.tier }}</div>
    <slot v-if="!value || !value.length" name="empty" />
  </div>`,
};

function mountView() {
  return mount(LoyaltyView, {
    global: {
      stubs: {
        Toolbar: passthrough,
        Dialog: passthrough,
        DataTable,
        Column: true,
        Tag: true,
        Button: { props: ['label'], template: '<button>{{ label }}</button>' },
        ToggleSwitch: true,
        ProgressSpinner: true,
        AutoComplete: true,
        InputNumber: true,
        InputText: true,
        'router-link': { template: '<a><slot /></a>' },
      },
    },
  });
}

describe('LoyaltyView', () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
  });

  it('loads members from /api/loyalty/members, not the retired stub', async () => {
    mockGet.mockResolvedValue([
      { customer_id: 'c1', customer_name: 'Big Spender', points: 6000, tier: 'gold', joined_at: '2026-01-01T00:00:00Z' },
    ]);
    const wrapper = mountView();
    await flushPromises();

    expect(mockGet).toHaveBeenCalledWith('/api/loyalty/members');
    expect(mockGet).not.toHaveBeenCalledWith('/api/loyalty');
    expect(wrapper.text()).toContain('Big Spender|6000|gold');
    expect(wrapper.text()).not.toContain('No loyalty members');
  });

  it('discount-tiers toggle keeps any tier with a discount, whatever its name', async () => {
    mockGet.mockResolvedValue([
      { customer_id: 'c1', customer_name: 'Vip Person', points: 150, tier: 'vip', tier_discount_pct: 7 },
      { customer_id: 'c2', customer_name: 'Base Person', points: 5, tier: 'bronze', tier_discount_pct: 0 },
    ]);
    const wrapper = mountView();
    await flushPromises();

    wrapper.vm.eliteOnly = true;
    await flushPromises();
    expect(wrapper.text()).toContain('Vip Person|150|vip');
    expect(wrapper.text()).not.toContain('Base Person');
  });

  it('shows a load error instead of an empty list when the read fails', async () => {
    mockGet.mockRejectedValue(new Error('Module disabled'));
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="loyalty-load-error"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Module disabled');
    expect(wrapper.text()).not.toContain('No loyalty members');
  });

  it('awards points through the real per-customer endpoint', async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValue({ id: 'p1' });
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openDialog();
    vm.customerPick = { label: 'Acme', value: 'abc-123' };
    vm.form.amount = 50;
    vm.form.reason = '  Referral bonus ';
    await vm.saveEntry();
    await flushPromises();

    expect(mockPost).toHaveBeenCalledWith(
      '/api/loyalty/customers/abc-123/points',
      { amount: 50, reason: 'Referral bonus' },
      { successMessage: 'Points awarded' },
    );
    const posted = mockPost.mock.calls.map((c) => c[0]);
    expect(posted).not.toContain('/api/loyalty/adjust');
    expect(posted).not.toContain('/api/loyalty/redeem');
    // Reloaded after the award.
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('will not save without a customer, a positive amount and a reason', async () => {
    mockGet.mockResolvedValue([]);
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openDialog();
    vm.form.amount = 50;
    vm.form.reason = 'x';
    await vm.saveEntry();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
