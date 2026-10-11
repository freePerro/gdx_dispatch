/**
 * CustomerTaxExemptionCard — the only UI caller of /api/tax/exemptions
 * (GDXA-624). Before it, POST and DELETE were reachable from tests alone,
 * while /api/tax/resolve read the rows to zero every exempt customer's
 * invoice tax.
 *
 * Pins:
 *  - hidden, and no request made, for a role the tax router would 403
 *  - lists only THIS customer's exemption
 *  - a failed POST that committed anyway closes the dialog (no duplicate)
 *  - add POSTs the real endpoint with exempt:true and this customer's id
 *  - no add button once an exemption exists (the resolver reads one row)
 *  - remove asks first, says removal is permanent, and DELETEs by id
 *  - a reversed date range is refused before any request
 *
 * jsdom applies no media queries, and useDestructiveConfirm is mocked —
 * the real dialog is proven only in a browser.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiDel = vi.fn();
const confirmDestructive = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, del: apiDel }),
}));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive }),
}));

import CustomerTaxExemptionCard from '../CustomerTaxExemptionCard.vue';
import { useAuthStore } from '../../stores/auth';

const CID = '11111111-2222-3333-4444-555555555555';

const stubs = {
  Card: { template: '<div class="card"><slot name="content" /></div>' },
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)"></textarea>',
  },
};

function asRole(role) {
  const auth = useAuthStore();
  auth.user = { id: 'u1', role };
}

async function mountCard() {
  const wrapper = mount(CustomerTaxExemptionCard, {
    props: { customerId: CID },
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

const MINE = {
  id: 'ex-1',
  customer_id: CID,
  exempt: true,
  reason: 'Reseller',
  certificate_id: 'CERT-42',
  exempt_from: null,
  exempt_until: null,
  notes: null,
};
const OTHER = { ...MINE, id: 'ex-2', customer_id: '99999999-2222-3333-4444-555555555555', reason: 'Other customer' };

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  apiGet.mockResolvedValue([]);
  apiPost.mockResolvedValue({ id: 'ex-new' });
  apiDel.mockResolvedValue(null);
});

describe('CustomerTaxExemptionCard', () => {
  it('renders nothing and calls nothing for a non-admin role', async () => {
    asRole('office');
    const w = await mountCard();
    expect(w.find('[data-testid="customer-tax-exemption-card"]').exists()).toBe(false);
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('shows only this customer\'s exemption', async () => {
    asRole('admin');
    apiGet.mockResolvedValue([OTHER, MINE]);
    const w = await mountCard();
    expect(apiGet.mock.calls[0][0]).toBe('/api/tax/exemptions');
    expect(w.find('[data-testid="tax-exemption-ex-1"]').exists()).toBe(true);
    expect(w.find('[data-testid="tax-exemption-ex-2"]').exists()).toBe(false);
    expect(w.text()).toContain('CERT-42');
    expect(w.find('[data-testid="tax-exemption-status"]').text()).toBe('Tax-exempt');
    // One exemption per customer: no second add.
    expect(w.find('[data-testid="add-tax-exemption-btn"]').exists()).toBe(false);
  });

  it('says the default rate applies when there is no exemption', async () => {
    asRole('owner');
    const w = await mountCard();
    expect(w.find('[data-testid="tax-exemption-none"]').exists()).toBe(true);
    expect(w.find('[data-testid="tax-exemption-status"]').text()).toBe('Taxable');
  });

  it('adds an exemption through the real POST and reloads', async () => {
    asRole('admin');
    const w = await mountCard();
    await w.find('[data-testid="add-tax-exemption-btn"]').trigger('click');
    await w.find('[data-testid="tax-ex-reason"]').setValue('  Non-profit ');
    await w.find('[data-testid="tax-ex-cert"]').setValue('NP-7');
    await w.find('[data-testid="tax-ex-until"]').setValue('2027-12-31');
    apiGet.mockResolvedValue([{ ...MINE, reason: 'Non-profit', certificate_id: 'NP-7' }]);
    await w.find('[data-testid="save-tax-exemption-btn"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/tax/exemptions');
    expect(body).toEqual({
      customer_id: CID,
      exempt: true,
      reason: 'Non-profit',
      certificate_id: 'NP-7',
      exempt_from: null,
      exempt_until: '2027-12-31',
      notes: null,
    });
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(w.find('[data-testid="tax-exemption-ex-1"]').exists()).toBe(true);
  });

  it('closes the dialog when a failed POST turns out to have saved the row', async () => {
    asRole('admin');
    const w = await mountCard();
    await w.find('[data-testid="add-tax-exemption-btn"]').trigger('click');
    apiPost.mockRejectedValue(new Error('timeout'));
    apiGet.mockResolvedValue([MINE]);
    await w.find('[data-testid="save-tax-exemption-btn"]').trigger('click');
    await flushPromises();
    expect(w.find('.dlg').exists()).toBe(false);
    expect(w.find('[data-testid="add-tax-exemption-btn"]').exists()).toBe(false);
    expect(w.find('[data-testid="tax-exemption-ex-1"]').exists()).toBe(true);
  });

  it('refuses an end date before the start date without calling the server', async () => {
    asRole('admin');
    const w = await mountCard();
    await w.find('[data-testid="add-tax-exemption-btn"]').trigger('click');
    await w.find('[data-testid="tax-ex-from"]').setValue('2027-06-01');
    await w.find('[data-testid="tax-ex-until"]').setValue('2027-01-01');
    await w.find('[data-testid="save-tax-exemption-btn"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
    expect(w.find('[data-testid="tax-ex-form-error"]').exists()).toBe(true);
  });

  it('confirms removal as permanent, then DELETEs by exemption id', async () => {
    asRole('admin');
    apiGet.mockResolvedValue([MINE]);
    const w = await mountCard();
    await w.find('[data-testid="remove-tax-exemption-ex-1"]').trigger('click');
    expect(confirmDestructive).toHaveBeenCalledTimes(1);
    const opts = confirmDestructive.mock.calls[0][0];
    expect(opts.message).toMatch(/permanent/i);
    expect(opts.message).toMatch(/audit log/i);
    expect(apiDel).not.toHaveBeenCalled(); // nothing happens until accepted
    apiGet.mockResolvedValue([]);
    await opts.accept();
    await flushPromises();
    expect(apiDel.mock.calls[0][0]).toBe('/api/tax/exemptions/ex-1');
    expect(w.find('[data-testid="tax-exemption-none"]').exists()).toBe(true);
  });

  it('flags an exemption whose end date has passed', async () => {
    asRole('admin');
    apiGet.mockResolvedValue([{ ...MINE, exempt_until: '2020-01-01' }]);
    const w = await mountCard();
    expect(w.find('[data-testid="tax-exemption-status"]').text()).toBe('Exemption expired');
  });
});
