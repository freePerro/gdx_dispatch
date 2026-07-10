/**
 * CustomerFormDialog — extracted 2026-05-21 from CustomersView.
 *
 * Pins:
 *  1. Edit mode pre-fills the form fields from the customer prop.
 *  2. Submit (edit) PATCHes /api/customers/{id} with the trimmed payload.
 *  3. Submit (create) POSTs /api/customers when no customer prop.
 *  4. `saved` event fires after a successful PATCH so the parent can refresh.
 *  5. Name required — blank name short-circuits with an inline error and no PATCH.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { nextTick } from 'vue';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

import CustomerFormDialog from '../../components/CustomerFormDialog.vue';

const stubs = {
  Dialog: {
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /></div>',
    inheritAttrs: false,
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'disabled', 'size', 'loading', 'type'],
    emits: ['click'],
    template: '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  // PhoneInput wraps PrimeVue InputMask; stub it like InputText (emit raw
  // value) so these behavior tests don't need the PrimeVue plugin.
  PhoneInput: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: '<select :data-testid="$attrs[\'data-testid\']" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>',
    inheritAttrs: false,
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :data-testid="$attrs[\'data-testid\']" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
    inheritAttrs: false,
  },
};

const baseCustomer = {
  id: 'cust-1',
  name: 'Acme Door Co',
  email: 'ops@acme.example',
  phone: '555-0142',
  address: '123 Main St',
  notes: 'VIP',
  referral_source: 'Angi',
  customer_type: 'Commercial',
};

// Keys CustomerCreateIn / CustomerUpdateIn accept (gdx/routers/customers.py).
// The dialog's PATCH payload must be a subset — anything else is silently
// dropped by Pydantic, which is the 2026-05-21 audit-block bug.
const ROUTER_ACCEPTED_KEYS = new Set([
  'name', 'phone', 'email', 'address',
  'customer_type', 'pricing_class', 'margin_override_pct',
  'clear_margin_override',
  'notes', 'referral_source',
]);

function mountDialog(props = {}) {
  return mount(CustomerFormDialog, {
    props: { visible: true, mode: 'create', customer: null, ...props },
    global: { stubs },
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  apiPatch.mockReset();
  toastAdd.mockReset();
});

describe('CustomerFormDialog', () => {
  it('pre-fills inputs from the customer prop in edit mode', async () => {
    const wrapper = mountDialog({ mode: 'edit', customer: baseCustomer });
    await nextTick();

    expect(wrapper.get('[data-testid="customer-name-input"]').element.value).toBe('Acme Door Co');
    expect(wrapper.get('[data-testid="customer-email-input"]').element.value).toBe('ops@acme.example');
    expect(wrapper.get('[data-testid="customer-phone-input"]').element.value).toBe('555-0142');
    expect(wrapper.get('[data-testid="customer-address-input"]').element.value).toBe('123 Main St');
  });

  it('PATCHes /api/customers/{id} on save and emits saved + closes dialog', async () => {
    apiPatch.mockResolvedValue({ ...baseCustomer, email: 'newops@acme.example' });
    const wrapper = mountDialog({ mode: 'edit', customer: baseCustomer });
    await nextTick();

    // Edit just the email — confirms the PATCH carries the full payload not
    // just deltas (the backend handler is full-record so this matters).
    await wrapper.get('[data-testid="customer-email-input"]').setValue('newops@acme.example');
    await wrapper.get('form').trigger('submit.prevent');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/customers/cust-1');
    expect(payload.email).toBe('newops@acme.example');
    expect(payload.name).toBe('Acme Door Co');
    expect(payload.phone).toBe('555-0142');

    // saved emitted; visible toggled false.
    expect(wrapper.emitted('saved')).toBeTruthy();
    const closeEvents = wrapper.emitted('update:visible') || [];
    expect(closeEvents[closeEvents.length - 1]).toEqual([false]);
  });

  it('POSTs /api/customers on save in create mode', async () => {
    apiPost.mockResolvedValue({ id: 'cust-new', name: 'Fresh Customer' });
    const wrapper = mountDialog({ mode: 'create', customer: null });
    await nextTick();

    await wrapper.get('[data-testid="customer-name-input"]').setValue('Fresh Customer');
    await wrapper.get('form').trigger('submit.prevent');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    expect(apiPost.mock.calls[0][0]).toBe('/api/customers');
    expect(apiPost.mock.calls[0][1].name).toBe('Fresh Customer');
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it('only sends keys that the backend Pydantic schema accepts', async () => {
    apiPatch.mockResolvedValue({ ...baseCustomer });
    const wrapper = mountDialog({ mode: 'edit', customer: baseCustomer });
    await nextTick();

    await wrapper.get('form').trigger('submit.prevent');
    await flushPromises();

    const payload = apiPatch.mock.calls[0][1];
    const extras = Object.keys(payload).filter((k) => !ROUTER_ACCEPTED_KEYS.has(k));
    expect(extras).toEqual([]);
  });

  it('blocks submit and surfaces an inline error when name is blank', async () => {
    const wrapper = mountDialog({ mode: 'create', customer: null });
    await nextTick();

    await wrapper.get('form').trigger('submit.prevent');
    await flushPromises();

    expect(apiPost).not.toHaveBeenCalled();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(wrapper.get('[data-testid="customer-form-error"]').text()).toMatch(/name is required/i);
  });
});
