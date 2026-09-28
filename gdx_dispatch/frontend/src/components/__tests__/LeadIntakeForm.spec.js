import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import LeadIntakeForm from '../LeadIntakeForm.vue';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockAddToast = vi.fn();
const mockPush = vi.fn();

let mockRole = 'admin';
let mockPermissions = new Set(['leads.write', 'estimates.write', 'leads.intake']);

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: mockGet,
    post: mockPost,
  }),
}));

vi.mock('primevue/usetoast', () => ({
  useToast: () => ({
    add: mockAddToast,
  }),
}));

vi.mock('vue-router', () => ({
  useRouter: () => ({
    push: mockPush,
  }),
}));

vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    role: mockRole,
    user: { role: mockRole },
  }),
}));

vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({
    hasPermission: (k) => mockPermissions.has(k),
  }),
}));

function mountForm(props = {}) {
  return mount(LeadIntakeForm, {
    props: {
      visible: true,
      ...props,
    },
    global: {
      stubs: {
        Dialog: {
          props: ['visible', 'header'],
          template: '<div v-if="visible" class="p-dialog-mock"><div class="header">{{ header }}</div><slot /></div>',
        },
        PhoneInput: {
          props: ['modelValue'],
          template: '<input class="phone-input-mock" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        Button: {
          props: ['label', 'loading', 'disabled'],
          template: '<button class="p-button-mock" :disabled="disabled"><slot>{{ label }}</slot></button>',
        },
        InputText: {
          props: ['modelValue', 'type'],
          template: '<input class="p-inputtext-mock" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        InputNumber: {
          props: ['modelValue'],
          template: '<input type="number" class="p-inputnumber-mock" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
        },
        Select: {
          props: ['modelValue', 'options'],
          template: '<select class="p-select-mock" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="o in options" :key="o" :value="o">{{ o }}</option></select>',
        },
        Textarea: {
          props: ['modelValue'],
          template: '<textarea class="p-textarea-mock" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
        },
        Checkbox: {
          props: ['modelValue'],
          template: '<input type="checkbox" class="p-checkbox-mock" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
        },
      },
    },
  });
}

describe('LeadIntakeForm', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockRole = 'admin';
    mockPermissions = new Set(['leads.write', 'estimates.write', 'leads.intake']);
    mockGet.mockResolvedValue({
      sources: ['Google Search', 'Referral', 'Other'],
      custom_fields: [
        {
          field_key: 'job_kind',
          label: 'Job kind',
          field_type: 'select',
          options: ['Repair', 'New door'],
        },
        {
          field_key: 'door_count',
          label: 'Door count',
          field_type: 'number',
        },
      ],
    });
  });

  it('fetches intake form definitions and renders dynamic custom fields', async () => {
    const wrapper = mountForm();
    await flushPromises();

    expect(mockGet).toHaveBeenCalledWith('/api/leads/intake-form');
    expect(wrapper.find('[data-test="intake-custom-field-job_kind"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="intake-custom-field-door_count"]').exists()).toBe(true);
  });

  it('prefills contact info and notes when passed via props', async () => {
    const wrapper = mountForm({
      initialName: 'John Smith',
      initialPhone: '(555) 999-1234',
      initialEmail: 'john@example.com',
      initialNotes: 'Broken spring, urgent',
      originRef: 'phone_com_call:call-123',
    });
    await flushPromises();

    const nameInput = wrapper.find('[data-test="intake-name-input"]');
    expect(nameInput.attributes('value') || nameInput.element.value).toBe('John Smith');
  });

  it('an E.164 prefill from a call lands in the mask as the national number', async () => {
    mockPost.mockResolvedValueOnce({ lead: { id: 'l9', name: 'X' }, matched_customer: null, possible_duplicate: null });
    const wrapper = mountForm({ initialName: 'X', initialPhone: '+16125550199' });
    await flushPromises();
    await wrapper.find('form').trigger('submit');
    await flushPromises();
    expect(mockPost).toHaveBeenCalledWith('/api/leads/intake', expect.objectContaining({ phone: '(612)555-0199' }));
  });

  it('submits intake payload and shows Start estimate button for office user', async () => {
    mockPost.mockImplementation(async (url) => {
      if (url === '/api/leads/intake') {
        return {
          lead: { id: 'lead-uuid-1', name: 'Alice Walker', stage: 'new' },
          matched_customer: { id: 'cust-1', name: 'Alice Walker' },
          possible_duplicate: null,
        };
      }
      if (url === '/api/leads/lead-uuid-1/start-estimate') {
        return {
          estimate: { id: 'est-uuid-99' },
          customer: { id: 'cust-1', name: 'Alice Walker' },
          reused: false,
        };
      }
    });

    const wrapper = mountForm({ initialName: 'Alice Walker' });
    await flushPromises();

    // Submit the form
    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(mockPost).toHaveBeenCalledWith('/api/leads/intake', expect.objectContaining({
      name: 'Alice Walker',
    }));

    // Post-submission office view:
    expect(wrapper.find('[data-test="intake-result-pane"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="intake-start-estimate-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="intake-match-info"]').text()).toContain('Alice Walker');

    // Click Start estimate
    await wrapper.find('[data-test="intake-start-estimate-btn"]').trigger('click');
    await flushPromises();

    expect(mockPost).toHaveBeenCalledWith('/api/leads/lead-uuid-1/start-estimate');
    expect(mockPush).toHaveBeenCalledWith('/estimates/est-uuid-99');
  });

  it('technician: no Start estimate, and the phone lookup is never called', async () => {
    mockRole = 'technician';
    mockPermissions = new Set(['leads.intake']);

    // The server withholds matched_customer from a caller without
    // customers.read_all, so this is what a tech really receives.
    mockPost.mockResolvedValueOnce({
      lead: { id: 'lead-uuid-2', name: 'Bob Tech Lead' },
      matched_customer: null,
      possible_duplicate: null,
    });

    const wrapper = mountForm({ initialName: 'Bob Tech Lead', initialPhone: '(555) 999-1234' });
    await flushPromises();
    await wrapper.find('.phone-input-mock').setValue('5559991235');
    await new Promise((r) => setTimeout(r, 400));
    await flushPromises();

    // The match endpoint returns the owner's name; a tech must not be sent it.
    expect(mockGet.mock.calls.some(([url]) => url.includes('match-phone'))).toBe(false);

    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(wrapper.text()).toContain('Saved — the office will follow up');
    expect(wrapper.find('[data-test="intake-start-estimate-btn"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="intake-match-info"]').exists()).toBe(false);
  });

  it('office with customers.read_all: the phone lookup names the customer', async () => {
    mockPermissions = new Set(['leads.write', 'estimates.write', 'customers.read_all']);
    mockGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/planner/match-phone')) {
        return { customer_id: 'cust-9', name: 'Carol Existing', normalized: '+15559991234' };
      }
      return { sources: ['Google'], custom_fields: [] };
    });

    const wrapper = mountForm({ initialPhone: '(555) 999-1234' });
    await flushPromises();

    expect(wrapper.find('[data-test="intake-phone-match"]').text()).toContain('Carol Existing');
  });

  it('renders duplicate warning banner when possible duplicate is returned', async () => {
    mockPost.mockResolvedValueOnce({
      lead: { id: 'lead-uuid-3', name: 'Duplicate Caller' },
      matched_customer: null,
      possible_duplicate: { id: 'lead-uuid-old', name: 'Duplicate Caller' },
    });

    const wrapper = mountForm({ initialName: 'Duplicate Caller' });
    await flushPromises();

    await wrapper.find('form').trigger('submit');
    await flushPromises();

    expect(wrapper.find('[data-test="intake-dup-warning"]').exists()).toBe(true);
  });
});
