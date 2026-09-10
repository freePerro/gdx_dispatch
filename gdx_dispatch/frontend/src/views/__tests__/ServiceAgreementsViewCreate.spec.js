/**
 * ServiceAgreementsView — the New Agreement dialog can create an agreement (#684).
 *
 * Before: the dialog collected a free-text "Customer Name" the API never
 * declared while the customer_id it requires stayed null, so every create was
 * a 422; a blank price went as an explicit null (also a 422); a Create click
 * with a field missing did nothing at all; dates went through toISOString(),
 * so an evening create in a US timezone saved tomorrow and the edit picker
 * showed the day before. These drive the REAL view and assert what it sends.
 */
process.env.TZ = 'America/Chicago';

import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ServiceAgreementsView from '../ServiceAgreementsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(async () => true) }),
}));

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));

const CUSTOMER = { id: '5a3c1e2d-0b4f-4a6e-9c7d-8e1f2a3b4c5d', name: 'Jane Customer' };

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  Badge: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"
            @click="$emit('row-click', { data: row })"><td>{{ row.name }}</td></tr>
      </tbody></table>`,
  },
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    inheritAttrs: false,
    template: `<button :data-testid="$attrs['data-testid']" @click="$emit('click')">{{ label }}</button>`,
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: `<select :data-testid="$attrs['data-testid']" :value="modelValue ?? ''"
        @change="$emit('update:modelValue', $event.target.value || null)">
        <option value=""></option>
        <option v-for="o in (options || [])" :key="o[optionValue || 'value']"
          :value="o[optionValue || 'value']">{{ o[optionLabel || 'label'] }}</option>
      </select>`,
  },
  // A calendar picker hands the form a LOCAL-midnight Date, like PrimeVue's.
  DatePicker: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: `<input :data-testid="$attrs['data-testid']"
        :data-day="modelValue ? modelValue.getDate() : ''"
        @change="$emit('update:modelValue', $event.target.value ? new Date($event.target.value + 'T00:00:00') : null)" />`,
  },
};

function routeGets(agreements = [], expiring = [], templates = []) {
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/customers')) return { items: [CUSTOMER] };
    if (url.startsWith('/api/service-agreements/templates')) return templates;
    if (url.startsWith('/api/service-agreements/expiring')) return expiring;
    if (url.startsWith('/api/service-agreements')) return agreements;
    throw new Error(`unexpected GET ${url}`);
  });
}

async function mountView() {
  const wrapper = mount(ServiceAgreementsView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

async function openCreate(wrapper) {
  const btn = wrapper.findAll('button').find((b) => b.text() === '+ New Agreement');
  await btn.trigger('click');
  await flushPromises();
}

async function fillRequired(wrapper, { price = '249.5' } = {}) {
  await wrapper.get('[data-testid="agreement-name"]').setValue('Spring tune-up plan');
  await wrapper.get('[data-testid="agreement-customer-dropdown"]').setValue(CUSTOMER.id);
  await wrapper.get('[data-testid="agreement-end-date"]').setValue('2027-09-10');
  await wrapper.get('[data-testid="agreement-end-date"]').trigger('change');
  if (price !== null) await wrapper.get('[data-testid="agreement-price"]').setValue(price);
}

describe('ServiceAgreementsView — New Agreement (#684)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    // 8:30 PM in Chicago on Sep 10 is already Sep 11 in UTC.
    vi.setSystemTime(new Date(2026, 8, 10, 20, 30));
    apiGet.mockReset();
    apiPost.mockReset().mockResolvedValue({ id: 'new-1' });
    apiPatch.mockReset().mockResolvedValue({});
    routeGets();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('sends a picked customer id and only keys the API declares', async () => {
    const wrapper = await mountView();
    await openCreate(wrapper);
    await fillRequired(wrapper);
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/service-agreements');
    expect(payload.customer_id).toBe(CUSTOMER.id);
    expect(payload).not.toHaveProperty('customer_name');
    expect(payload).not.toHaveProperty('status');
    expect(payload.price).toBe(249.5);
    // The default start date is TODAY in the office's timezone, not UTC's tomorrow.
    expect(payload.start_date).toBe('2026-09-10');
    expect(payload.end_date).toBe('2027-09-10');
  });

  it('requires a price — a blank never becomes a silent $0', async () => {
    const wrapper = await mountView();
    await openCreate(wrapper);
    await fillRequired(wrapper, { price: null });
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).not.toHaveBeenCalled();
    expect(wrapper.get('[data-testid="agreement-form-error"]').text()).toContain('price');

    await wrapper.get('[data-testid="agreement-price"]').setValue('0');
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][1].price).toBe(0);
  });

  it("picking a template offers the template's price when none is entered", async () => {
    routeGets([], [], [{ id: 'tpl-1', name: 'Gold', default_price: 299 }]);
    const wrapper = await mountView();
    await openCreate(wrapper);
    await wrapper.findAll('select').find((s) => s.findAll('option').some((o) => o.text() === 'Gold'))
      .setValue('tpl-1');
    await flushPromises();
    expect(wrapper.get('[data-testid="agreement-price"]').element.value).toBe('299');
  });

  function pickTemplate(wrapper, id) {
    const select = wrapper.findAll('select').find((s) => s.findAll('option').some((o) => o.element.value === id));
    return select.setValue(id);
  }

  it('a $0 template offers no price, so a blank still has to be filled in', async () => {
    // default_price is NOT NULL DEFAULT 0 — 0 there means "never set".
    routeGets([], [], [{ id: 'tpl-0', name: 'Legacy', default_price: 0 }]);
    const wrapper = await mountView();
    await openCreate(wrapper);
    await fillRequired(wrapper, { price: null });
    await pickTemplate(wrapper, 'tpl-0');
    await flushPromises();
    expect(wrapper.get('[data-testid="agreement-price"]').element.value).toBe('');

    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
    expect(wrapper.get('[data-testid="agreement-form-error"]').text()).toContain('price');
  });

  it('switching templates moves a price the dialog filled, never a typed one', async () => {
    routeGets([], [], [
      { id: 'tpl-g', name: 'Gold', default_price: 299 },
      { id: 'tpl-s', name: 'Silver', default_price: 149 },
    ]);
    const wrapper = await mountView();
    await openCreate(wrapper);
    await fillRequired(wrapper, { price: null });
    await pickTemplate(wrapper, 'tpl-g');
    await flushPromises();
    await pickTemplate(wrapper, 'tpl-s');
    await flushPromises();
    expect(wrapper.get('[data-testid="agreement-price"]').element.value).toBe('149');

    // A price the user typed is theirs: a switch leaves it alone.
    await wrapper.get('[data-testid="agreement-price"]').setValue('175');
    await pickTemplate(wrapper, 'tpl-g');
    await flushPromises();
    expect(wrapper.get('[data-testid="agreement-price"]').element.value).toBe('175');

    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][1]).toMatchObject({ template_id: 'tpl-g', price: 175 });
  });

  it('says what is missing instead of doing nothing', async () => {
    const wrapper = await mountView();
    await openCreate(wrapper);
    await wrapper.get('[data-testid="agreement-name"]').setValue('Spring tune-up plan');
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).not.toHaveBeenCalled();
    expect(wrapper.get('[data-testid="agreement-form-error"]').text()).toContain('customer');

    // Fixing the field clears the message rather than leaving it stale.
    await wrapper.get('[data-testid="agreement-customer-dropdown"]').setValue(CUSTOMER.id);
    await flushPromises();
    expect(wrapper.find('[data-testid="agreement-form-error"]').exists()).toBe(false);
  });

  it('has no Status field on create (a new agreement is always active)', async () => {
    const wrapper = await mountView();
    await openCreate(wrapper);
    expect(wrapper.find('[data-testid="agreement-status"]').exists()).toBe(false);
  });

  it('edit shows the stored calendar dates and sends a status string', async () => {
    routeGets([
      {
        id: 'sa-1',
        name: 'Annual plan',
        customer_id: CUSTOMER.id,
        customer_name: CUSTOMER.name,
        template_id: null,
        status: 'active',
        start_date: '2026-09-10T00:00:00+00:00',
        end_date: '2027-09-10T00:00:00+00:00',
        price: 299,
        services_included: [],
        notes: '',
      },
    ]);
    const wrapper = await mountView();
    await wrapper.get('.dt-row').trigger('click');
    await flushPromises();

    // UTC midnight read as an instant is Sep 9, 7 PM in Chicago.
    expect(wrapper.get('[data-testid="agreement-start-date"]').attributes('data-day')).toBe('10');
    expect(wrapper.get('[data-testid="agreement-customer-dropdown"]').element.value).toBe(CUSTOMER.id);

    await wrapper.get('[data-testid="agreement-status"]').setValue('expired');
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).not.toHaveBeenCalled();
    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/service-agreements/sa-1');
    // Only what the user changed — untouched dates, notes and customer are not
    // re-sent (re-sending rewrote a stored time of day to midnight and an
    // empty note to '', and the audit row reported both as edits).
    expect(payload).toEqual({ status: 'expired' });
  });

  it('an edit with nothing changed sends nothing and closes', async () => {
    routeGets([
      {
        id: 'sa-4', name: 'Quiet plan', customer_id: CUSTOMER.id, customer_name: CUSTOMER.name,
        customer_deleted: false, template_id: null, status: 'active',
        start_date: '2026-09-10T15:30:00+00:00', end_date: '2027-09-10T15:30:00+00:00',
        price: 299, services_included: ['Inspection'], notes: null,
      },
    ]);
    const wrapper = await mountView();
    await wrapper.get('.dt-row').trigger('click');
    await flushPromises();
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).not.toHaveBeenCalled();
    expect(wrapper.find('.dlg').exists()).toBe(false);
  });

  it('two customers with the same name can be told apart in the picker', async () => {
    const twins = [
      { id: 'aaaaaaaa-0000-0000-0000-000000000001', name: 'Pat Smith', email: 'pat@north.test' },
      { id: 'aaaaaaaa-0000-0000-0000-000000000002', name: 'Pat Smith', email: 'pat@south.test' },
      CUSTOMER,
    ];
    const routed = apiGet.getMockImplementation();
    apiGet.mockImplementation(async (url) => (url.startsWith('/api/customers') ? { items: twins } : routed(url)));
    const wrapper = await mountView();
    await openCreate(wrapper);
    const labels = wrapper.get('[data-testid="agreement-customer-dropdown"]').findAll('option').map((o) => o.text());
    expect(labels).toContain('Pat Smith — pat@north.test');
    expect(labels).toContain('Pat Smith — pat@south.test');
    expect(labels).toContain(CUSTOMER.name);
  });

  it('a hint that would itself collide falls back to one that does not', async () => {
    // Real duplicates usually share an email — then the phone tells them apart.
    const twins = [
      { id: 'bbbbbbbb-0000-0000-0000-000000000001', name: 'Troy R', email: 'troy@x.test', phone: '555-0101' },
      { id: 'bbbbbbbb-0000-0000-0000-000000000002', name: 'Troy R', email: 'troy@x.test', phone: '555-0202' },
    ];
    const routed = apiGet.getMockImplementation();
    apiGet.mockImplementation(async (url) => (url.startsWith('/api/customers') ? { items: twins } : routed(url)));
    const wrapper = await mountView();
    await openCreate(wrapper);
    const labels = wrapper.get('[data-testid="agreement-customer-dropdown"]').findAll('option').map((o) => o.text()).filter(Boolean);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels).toEqual(['Troy R — 555-0101', 'Troy R — 555-0202']);
  });

  it("editing a deleted customer's agreement still shows whose it is and saves", async () => {
    // /api/customers lists live customers only; this one was since deleted.
    const goneId = '11111111-2222-3333-4444-555555555555';
    routeGets([
      {
        id: 'sa-9',
        name: 'Old plan',
        customer_id: goneId,
        customer_name: 'Gone Customer',
        customer_deleted: true,
        template_id: null,
        status: 'active',
        start_date: '2026-01-01T00:00:00+00:00',
        end_date: '2026-12-31T00:00:00+00:00',
        price: 100,
        services_included: [],
        notes: '',
      },
    ]);
    const wrapper = await mountView();
    await wrapper.get('.dt-row').trigger('click');
    await flushPromises();

    const picker = wrapper.get('[data-testid="agreement-customer-dropdown"]');
    expect(picker.element.value).toBe(goneId);
    expect(picker.findAll('option').map((o) => o.text())).toContain('Gone Customer (deleted)');

    // Renewing it re-prices without touching (or re-checking) the customer.
    await wrapper.get('[data-testid="agreement-price"]').setValue('150');
    await wrapper.get('[data-testid="agreement-save"]').trigger('click');
    await flushPromises();
    expect(apiPatch.mock.calls[0][1]).toEqual({ price: 150 });
  });

  it('a customer missing only because the list failed to load is not called deleted', async () => {
    routeGets([
      {
        id: 'sa-7', name: 'Live plan', customer_id: CUSTOMER.id, customer_name: CUSTOMER.name,
        customer_deleted: false, template_id: null, status: 'active',
        start_date: '2026-01-01T00:00:00+00:00', end_date: '2026-12-31T00:00:00+00:00',
        price: 100, services_included: [], notes: '',
      },
    ]);
    const routed = apiGet.getMockImplementation();
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/customers')) throw new Error('network down');
      return routed(url);
    });
    const wrapper = await mountView();
    await wrapper.get('.dt-row').trigger('click');
    await flushPromises();
    const labels = wrapper.get('[data-testid="agreement-customer-dropdown"]').findAll('option').map((o) => o.text());
    expect(labels).toContain(CUSTOMER.name);
    expect(labels.some((l) => l.includes('(deleted)'))).toBe(false);
  });

  it('shows the expiring banner from the list the endpoint returns', async () => {
    routeGets([], [{ id: 'sa-2' }, { id: 'sa-3' }]);
    const wrapper = await mountView();
    expect(wrapper.find('.alert-banner').text()).toContain('2 agreements expire');
  });
});
