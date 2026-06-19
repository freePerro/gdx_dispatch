/**
 * PaymentsView tests — pins the S110b AutoComplete invoice-picker
 * auto-fill contract: when the user selects an invoice from the
 * dropdown, the form's invoice_id, customer, and amount fields populate
 * automatically (so the operator only confirms — doesn't retype).
 *
 * Pre-fix the dialog had a free-text Invoice ID; users had to know the
 * UUID. Test guarantees the AutoComplete remains the source of truth
 * AND the on-select hydrates the form correctly.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import PaymentsView from '../PaymentsView.vue';

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: apiPostMock }),
}));

// Capture the AutoComplete @item-select handler so the test can call it
// with a chosen invoice — the real PrimeVue AutoComplete needs a full
// dropdown render that's not worth wiring up in jsdom.
let capturedOnItemSelect = null;
const stubs = {
  AppLayout: { template: '<div><slot /></div>' },
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  InputText: { template: '<input />' },
  InputNumber: { template: '<input type="number" />' },
  Select: { template: '<select />' },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Tag: { template: '<span><slot /></span>' },
  ProgressSpinner: { template: '<div />' },
  AutoComplete: {
    props: ['suggestions'],
    emits: ['complete', 'item-select'],
    template: '<input data-test="ac" />',
    mounted() {
      // Persist the parent's @item-select handler for direct invocation.
      capturedOnItemSelect = (event) => this.$emit('item-select', event);
    },
  },
};

describe('PaymentsView Record Payment dialog — invoice AutoComplete', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGetMock.mockReset();
    apiPostMock.mockReset();
    capturedOnItemSelect = null;
    // Loaded payments list (empty) and invoice catalog response
    apiGetMock.mockImplementation((url) => {
      if (url.startsWith('/api/payments')) return Promise.resolve([]);
      if (url.startsWith('/api/invoices')) {
        return Promise.resolve([
          {
            id: 'inv-uuid-1',
            invoice_number: 'INV-000123',
            customer_name: 'Acme Co',
            balance_due: 250.0,
            total: 500.0,
            status: 'sent',
          },
        ]);
      }
      return Promise.resolve([]);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('opens the dialog + loads invoice catalog when "+ Record Payment" clicked', async () => {
    const w = mount(PaymentsView, { global: { stubs } });
    await flushPromises();
    // Click the open-dialog button
    const openBtn = w.findAll('button').find((b) => /Record Payment/.test(b.text()));
    expect(openBtn).toBeTruthy();
    await openBtn.trigger('click');
    await flushPromises();
    // Catalog endpoint hit
    const calls = apiGetMock.mock.calls.map((c) => c[0]);
    expect(calls.some((u) => /\/api\/invoices\?per_page=500/.test(u))).toBe(true);
  });

  it('auto-fills form.invoice_id, customer, and amount on item-select', async () => {
    const w = mount(PaymentsView, { global: { stubs } });
    await flushPromises();
    const openBtn = w.findAll('button').find((b) => /Record Payment/.test(b.text()));
    await openBtn.trigger('click');
    await flushPromises();
    // Trigger the on-select via the captured emit. The component's
    // @item-select handler reads event.value.
    expect(capturedOnItemSelect).toBeTruthy();
    await capturedOnItemSelect({
      value: {
        id: 'inv-uuid-1',
        invoice_number: 'INV-000123',
        customer_name: 'Acme Co',
        balance_due: 250.0,
        total: 500.0,
      },
    });
    await flushPromises();
    // Inspect the form ref through the wrapper's setup-exposed state
    // by reading the underlying input values. We use the InputText stub
    // which renders a bare <input>; the v-model binding lives in
    // wrapper.vm.form via setup script.
    const form = w.vm.form;
    expect(form.invoice_id).toBe('INV-000123');
    expect(form.customer).toBe('Acme Co');
    // amount auto-fills from balance_due (250) when previously null.
    expect(form.amount).toBe(250.0);
  });
});
