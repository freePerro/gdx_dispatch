/**
 * AccountingSettingsView (GL S4.5) — pins:
 *  1. Loads /api/accounting/settings and renders the posting status.
 *  2. Enable-posting confirm button stays disabled until the EXACT phrase
 *     is typed, then POSTs /settings/enable-posting.
 *  3. Locked fields render a disabled control + lock note.
 *  4. System accounts' edit dialog hides the Active toggle (deactivate
 *     unrepresentable in the UI, matching the API 409).
 *  5. Payment-map change PATCHes the WHOLE map (reassigned, not mutated).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}));

import AccountingSettingsView from '../AccountingSettingsView.vue';

function payload(overrides = {}) {
  return {
    seeded: true,
    settings: {
      ledger_posting_enabled: false,
      reporting_basis: 'accrual',
      tax_basis: 'cash',
      inventory_treatment: 'expense',
      cutover_month: null,
      entity_type: null,
      opening_bank_attested_at: null,
      opening_bank_attested_by: null,
      payment_method_role_map: { cash: 'UNDEPOSITED', other: 'UNDEPOSITED' },
      credit_reason_role_map: { discount: 'DISCOUNTS', other: 'REFUNDS' },
      expense_category_account_map: { Fuel: 'acct-fuel' },
      cpa_review: {},
      ...(overrides.settings || {}),
    },
    accounts: overrides.accounts || [
      { id: 'acct-ar', code: '1200', name: 'Accounts Receivable', type: 'asset', role: 'AR', is_system: true, active: true },
      { id: 'acct-fuel', code: '6100', name: 'Fuel', type: 'expense', role: null, is_system: false, active: true },
    ],
    roles: ['AR', 'UNDEPOSITED', 'OPERATING_BANK'],
    account_types: ['asset', 'liability', 'equity', 'revenue', 'expense'],
    locked_fields: overrides.locked_fields || {},
    enable_confirm_phrase: 'ENABLE LEDGER POSTING',
    entries_exist: overrides.entries_exist || false,
  };
}

const stubs = {
  Dialog: {
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: '<div v-if="visible" class="dialog-stub"><slot /><slot name="footer" /></div>',
    inheritAttrs: false,
  },
  Button: {
    props: ['label', 'disabled', 'icon', 'severity', 'text', 'outlined', 'size'],
    emits: ['click'],
    template:
      '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'blur'],
    template:
      '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" @blur="$emit(\'blur\')" />',
    inheritAttrs: false,
  },
  Select: {
    props: ['modelValue', 'options', 'disabled', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    template:
      '<select :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\')"><option v-for="o in options" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option></select>',
    inheritAttrs: false,
  },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  DatePicker: {
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :disabled="disabled" />',
    inheritAttrs: false,
  },
  Tag: {
    props: ['value', 'severity'],
    template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>',
    inheritAttrs: false,
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :data-testid="$attrs[\'data-testid\']" :checked="modelValue" />',
    inheritAttrs: false,
  },
  ProgressSpinner: { template: '<div />' },
};

async function mountView(data) {
  apiGet.mockResolvedValue(data);
  const wrapper = mount(AccountingSettingsView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
});

describe('AccountingSettingsView', () => {
  it('loads settings and shows posting status OFF', async () => {
    const wrapper = await mountView(payload());
    expect(apiGet).toHaveBeenCalledWith('/api/accounting/settings');
    expect(apiPost).not.toHaveBeenCalled(); // seeded — no initialize
    expect(wrapper.find('[data-testid="gl-posting-status"]').text()).toBe('OFF');
  });

  it('unseeded GET triggers the write-gated initialize', async () => {
    apiGet.mockResolvedValue({ seeded: false, settings: null, accounts: [] });
    apiPost.mockResolvedValue(payload());
    const wrapper = mount(AccountingSettingsView, { global: { stubs } });
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/accounting/settings/initialize',
      {},
      expect.anything(),
    );
    expect(wrapper.find('[data-testid="gl-posting-status"]').text()).toBe('OFF');
  });

  it('enable confirm button unlocks only on the exact phrase', async () => {
    const wrapper = await mountView(payload());
    await wrapper.find('[data-testid="gl-enable-posting-btn"]').trigger('click');

    const confirmBtn = () => wrapper.find('[data-testid="gl-enable-confirm-btn"]');
    expect(confirmBtn().attributes('disabled')).toBeDefined();

    await wrapper.find('[data-testid="gl-enable-confirm-input"]').setValue('enable ledger posting');
    expect(confirmBtn().attributes('disabled')).toBeDefined();

    await wrapper.find('[data-testid="gl-enable-confirm-input"]').setValue('ENABLE LEDGER POSTING');
    expect(confirmBtn().attributes('disabled')).toBeUndefined();

    apiPost.mockResolvedValue(payload({ settings: { ledger_posting_enabled: true } }));
    await confirmBtn().trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith(
      '/api/accounting/settings/enable-posting',
      { confirm: 'ENABLE LEDGER POSTING' },
      expect.anything(),
    );
    expect(wrapper.find('[data-testid="gl-posting-status"]').text()).toBe('ENABLED');
  });

  it('locked fields render disabled with a lock note', async () => {
    const wrapper = await mountView(
      payload({
        settings: { ledger_posting_enabled: true },
        locked_fields: {
          inventory_treatment: 'ledger posting is enabled',
          cutover_month: 'ledger posting is enabled',
          payment_method_role_map: 'ledger posting is enabled',
        },
      }),
    );
    expect(wrapper.find('[data-testid="gl-inventory-treatment"]').attributes('disabled')).toBeDefined();
    expect(wrapper.find('[data-testid="gl-paymap-cash"]').attributes('disabled')).toBeDefined();
    expect(wrapper.find('[data-testid="gl-lock-inventory_treatment"]').exists()).toBe(true);
  });

  it('system account edit dialog hides the Active toggle', async () => {
    const data = payload();
    const wrapper = await mountView(data);
    // DataTable is stubbed (Column bodies don't render) — drive the exposed handle.
    wrapper.vm.openEdit(data.accounts[0]); // 1200 AR, system
    await flushPromises();
    expect(wrapper.find('[data-testid="gl-account-active"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('rename/renumber only');

    wrapper.vm.openEdit(data.accounts[1]); // 6100 Fuel, non-system
    await flushPromises();
    expect(wrapper.find('[data-testid="gl-account-active"]').exists()).toBe(true);
  });

  it('payment-map change PATCHes the whole reassigned map', async () => {
    const wrapper = await mountView(payload());
    apiPatch.mockResolvedValue(payload());
    const select = wrapper.find('[data-testid="gl-paymap-cash"]');
    await select.setValue('OPERATING_BANK');
    await flushPromises();
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/accounting/settings',
      { payment_method_role_map: { cash: 'OPERATING_BANK', other: 'UNDEPOSITED' } },
      expect.anything(),
    );
  });
});
