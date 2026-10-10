/**
 * CustomerDetailView — the Set Primary switch and the Customer Type field
 * (GDXA-413).
 *
 * 1. The location card's switch was wired `@change="value => …"`. PrimeVue's
 *    ToggleSwitch emits `change` with the DOM Event, so the PATCH body was
 *    `{ is_primary: <Event> }` and the API answered 422 "Input should be a
 *    valid boolean". This spec mounts the REAL ToggleSwitch (a stub would emit
 *    whatever the stub says), asserts the body carries a boolean, and that a
 *    failed save puts the switch back: ToggleSwitch keeps its own state, so
 *    the old refetch-only catch left it reading "Primary".
 * 2. The edit dialog's Customer Type offered only Residential and Commercial
 *    while CustomerFormDialog offered six, so a Contractor opened to a blank
 *    field. Both now read constants/customerTypes.js.
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDelete = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ isEnabled: () => false, modules: { value: [] } }),
}));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: () => true,
    permissions: { value: [] },
    permissionsLoaded: { value: true },
    reloadPermissions: vi.fn(),
  }),
}));
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: 'cust-1' } }),
  useRouter: () => ({ push: vi.fn(), back: vi.fn() }),
}));

import CustomerDetailView from '../CustomerDetailView.vue';
import { CUSTOMER_TYPES } from '../../constants/customerTypes';

const LOC = {
  id: 'loc-2',
  customer_id: 'cust-1',
  label: 'Shop',
  address: '9 Mill Rd',
  is_primary: false,
};

// The Select stub renders its options so the spec can read what the dialog
// offers; everything else is inert. ToggleSwitch is deliberately NOT stubbed.
const stubs = {
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button type="button" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  PhoneInput: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div><slot /></div>' },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    template: `<select :data-value="modelValue">
      <option v-for="o in options" :key="optionValue ? o[optionValue] : o" :value="optionValue ? o[optionValue] : o">
        {{ optionLabel ? o[optionLabel] : o }}
      </option></select>`,
  },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  ProgressSpinner: { template: '<div />' },
  Toast: { template: '<div />' },
  JobStateChip: { template: '<span />' },
  EmailTimeline: { template: '<div />' },
};

let customer;
let locations;

async function mountView() {
  const wrapper = mount(CustomerDetailView, {
    global: { plugins: [[PrimeVue, { unstyled: true }]], stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  customer = { id: 'cust-1', name: 'Riverbend Lumber', customer_type: 'Residential' };
  locations = [LOC];
  apiGet.mockImplementation((url) => {
    // Fresh objects per fetch, as the real API returns.
    if (url === '/api/customers/cust-1/locations') return Promise.resolve(locations.map((l) => ({ ...l })));
    if (url === '/api/customers/cust-1') return Promise.resolve(customer);
    return Promise.resolve([]);
  });
  apiPatch.mockResolvedValue({ ...LOC, is_primary: true });
});

describe('CustomerDetailView — Set Primary switch (GDXA-413)', () => {
  it('turning the switch on PATCHes is_primary as the boolean true, not the DOM event', async () => {
    const wrapper = await mountView();
    wrapper.vm.activeTab = 'Locations';
    await flushPromises();

    const input = wrapper.get('[data-testid="location-loc-2"] input[type="checkbox"]');
    await input.trigger('change');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, body] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/customers/cust-1/locations/loc-2');
    expect(body).toEqual({ is_primary: true });
  });

  it('a failed save puts the switch back to what the server holds', async () => {
    apiPatch.mockRejectedValue(new Error('403'));
    const wrapper = await mountView();
    wrapper.vm.activeTab = 'Locations';
    await flushPromises();

    const input = wrapper.get('[data-testid="location-loc-2"] input[type="checkbox"]');
    await input.trigger('change');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    expect(wrapper.get('[data-testid="location-loc-2"] input[type="checkbox"]').attributes('aria-checked')).toBe('false');
  });
});

describe('CustomerDetailView — Customer Type options (GDXA-413)', () => {
  async function openEdit() {
    const wrapper = await mountView();
    wrapper.vm.openEditDialog();
    await flushPromises();
    return wrapper.get('[data-testid="edit-customer-type"]');
  }

  it('offers the same list as CustomerFormDialog', async () => {
    const select = await openEdit();
    const offered = select.findAll('option').map((o) => o.element.value);
    expect(offered).toEqual(CUSTOMER_TYPES);
  });

  it('a Contractor customer opens with Contractor selected, not a blank field', async () => {
    customer.customer_type = 'contractor';
    const select = await openEdit();
    expect(select.attributes('data-value')).toBe('Contractor');
  });

  it('a type outside the list is offered verbatim rather than shown blank', async () => {
    customer.customer_type = 'Builder';
    const select = await openEdit();
    const offered = select.findAll('option').map((o) => o.element.value);
    expect(offered).toContain('Builder');
    expect(select.attributes('data-value')).toBe('Builder');
  });
});
