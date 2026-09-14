/**
 * CustomerDetailView — the Locations tab keeps what its dialog collects (#683).
 *
 * The dialog has Address, City, State, Zip and "Access notes (gate codes,
 * dogs, parking)". It sent the notes as `notes`, a key the API does not
 * declare (the column and every reader use `access_notes`), and the card read
 * `loc.notes`, which the API never returns — so a gate code was dropped on
 * save and could not have been shown even if it had been kept.
 *
 * The server half (the request models also dropped city/state/zip) is pinned
 * by tests/test_customer_location_fields.py. This spec pins the page's half:
 * the exact keys it sends and the fields it reads back.
 *
 * jsdom applies no media queries — this proves wiring, never layout.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDelete = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, delete: apiDelete, del: apiDelete }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
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

const CUSTOMER = { id: 'cust-1', name: 'Riverbend Lumber' };
const SAVED = {
  id: 'loc-1',
  customer_id: 'cust-1',
  label: 'Lake house',
  address: '12 Shore Ln',
  city: 'Brainerd',
  state: 'MN',
  zip: '56401',
  access_notes: 'Gate 4471, dog in yard',
  is_primary: false,
};

const model = (tag) => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<${tag} :value="modelValue" @input="$emit('update:modelValue', $event.target.value)"></${tag}>`,
});

const stubs = {
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'rounded', 'outlined', 'size', 'type'],
    emits: ['click'],
    template: '<button type="button" :data-label="label" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Dialog: { props: ['visible', 'header'], template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>' },
  InputText: model('input'),
  PhoneInput: model('input'),
  Textarea: model('textarea'),
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div><slot /></div>' },
  Select: { props: ['modelValue'], template: '<select />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  ToggleSwitch: { props: ['modelValue'], template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  Toast: { template: '<div />' },
  JobStateChip: { template: '<span />' },
  EmailTimeline: { template: '<div />' },
};

let locations = [];

async function openLocationsTab() {
  const wrapper = mount(CustomerDetailView, { global: { stubs, directives: { tooltip: {} } } });
  await flushPromises();
  wrapper.vm.activeTab = 'Locations';
  await flushPromises();
  return wrapper;
}

async function submitDialog(wrapper) {
  await wrapper.get('[data-testid="location-dialog"] form').trigger('submit');
  await flushPromises();
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  locations = [];
  apiGet.mockImplementation((url) => {
    if (url === '/api/customers/cust-1/locations') return Promise.resolve(locations);
    if (url === '/api/customers/cust-1') return Promise.resolve(CUSTOMER);
    return Promise.resolve([]);
  });
  apiPost.mockResolvedValue({ ...SAVED });
  apiPatch.mockResolvedValue({ ...SAVED });
});

describe('CustomerDetailView — Locations tab (#683)', () => {
  it('Add Location sends city, state, zip and the notes as access_notes', async () => {
    const wrapper = await openLocationsTab();
    await wrapper.get('[data-testid="add-location-btn"]').trigger('click');
    await flushPromises();

    await wrapper.get('[data-testid="location-address-input"]').setValue('12 Shore Ln');
    await wrapper.get('[data-testid="location-city-input"]').setValue('Brainerd');
    await wrapper.get('[data-testid="location-state-input"]').setValue('MN');
    await wrapper.get('[data-testid="location-zip-input"]').setValue('56401');
    await wrapper.get('[data-testid="location-notes-input"]').setValue('Gate 4471, dog in yard');
    await submitDialog(wrapper);

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toBe('/api/customers/cust-1/locations');
    expect(body).toMatchObject({
      address: '12 Shore Ln',
      city: 'Brainerd',
      state: 'MN',
      zip: '56401',
      access_notes: 'Gate 4471, dog in yard',
    });
    // The key the API silently dropped must not come back.
    expect(body).not.toHaveProperty('notes');
  });

  it('the card shows the saved access notes and the city/state/zip line', async () => {
    locations = [SAVED];
    const wrapper = await openLocationsTab();

    const card = wrapper.get('[data-testid="location-loc-1"]');
    expect(card.text()).toContain('Gate 4471, dog in yard');
    expect(card.text()).toContain('Brainerd, MN, 56401');
  });

  it('the card shows a zip even when city and state are empty', async () => {
    locations = [{ ...SAVED, city: null, state: null, zip: '56468', access_notes: null }];
    const wrapper = await openLocationsTab();

    expect(wrapper.get('[data-testid="location-loc-1"]').text()).toContain('56468');
  });

  it('Edit reopens with the saved access notes and PATCHes them back as access_notes', async () => {
    locations = [SAVED];
    const wrapper = await openLocationsTab();
    await wrapper.get('[data-testid="edit-location-loc-1"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-testid="location-notes-input"]').element.value).toBe('Gate 4471, dog in yard');
    expect(wrapper.get('[data-testid="location-zip-input"]').element.value).toBe('56401');

    await wrapper.get('[data-testid="location-notes-input"]').setValue('Gate 9900');
    await submitDialog(wrapper);

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, body] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/customers/cust-1/locations/loc-1');
    expect(body).toMatchObject({ city: 'Brainerd', state: 'MN', zip: '56401', access_notes: 'Gate 9900' });
    expect(body).not.toHaveProperty('notes');
  });
});
