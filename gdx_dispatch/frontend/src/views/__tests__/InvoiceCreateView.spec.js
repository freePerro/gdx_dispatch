/**
 * InvoiceCreateView — S122 contract pins.
 *
 * Pins:
 *  1. Tax-rate field hydrates from /api/tax/config (no more hardcoded 8.25%).
 *  2. ?job_id= in the route query pre-fills customer + estimate lines.
 *  3. POST payload shape: tax_rate as decimal fraction, from_part_ids passed through,
 *     line_items shape (description, quantity, unit_price, taxable).
 *  4. Submit disabled until customer + job + a real line item exist.
 *  5. Successful create routes to /billing/:id.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();
const routerPush = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: toastAdd }),
}));

// Route query is mutable per-test.
const routeQuery = { value: {} };
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: routeQuery.value }),
  useRouter: () => ({ push: routerPush }),
}));

import InvoiceCreateView from '../InvoiceCreateView.vue';

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'disabled', 'size', 'loading'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  Card: { template: '<div><slot name="content" /></div>' },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    template: '<select :data-testid="$attrs[\'data-testid\']" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>',
    inheritAttrs: false,
  },
  DatePicker: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Divider: { template: '<hr />' },
  LineItemEditor: {
    props: {
      lines: Array,
      fromPartIds: Array,
      jobId: String,
      showTaxable: Boolean,
      showCost: Boolean,
      showMargin: Boolean,
      categories: Array,
    },
    emits: ['update:lines', 'update:fromPartIds'],
    template: `
      <div data-testid="line-editor-stub">
        <span data-testid="le-job-id">{{ jobId }}</span>
        <span data-testid="le-line-count">{{ lines.length }}</span>
        <span data-testid="le-show-cost">{{ showCost ? 'yes' : 'no' }}</span>
        <span data-testid="le-show-margin">{{ showMargin ? 'yes' : 'no' }}</span>
        <span data-testid="le-cat-count">{{ (categories || []).length }}</span>
        <button data-testid="le-add-part" @click="$emit('update:fromPartIds', [...(fromPartIds||[]), 'p1'])">add part</button>
        <button data-testid="le-set-line" @click="$emit('update:lines', [{ description: 'Spring', quantity: 1, unit_price: 50, taxable: true, category: 'Springs', cost: 28, margin_pct_override: 40 }])">set line</button>
      </div>
    `,
  },
};

const CUSTOMERS = [
  { id: 'cust-1', name: 'Alice', phone: '555-1' },
  { id: 'cust-2', name: 'Bob', phone: null },
];
const JOBS = [
  { id: 'job-1', customer_id: 'cust-1', title: 'Fix door' },
  { id: 'job-2', customer_id: 'cust-2', title: 'Replace spring' },
];

function setupApiDefaults() {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
    if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
    if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0.0738, rate_pct: 7.38 });
    if (url.startsWith('/api/estimates?')) return Promise.resolve([]);
    return Promise.resolve([]);
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
  routerPush.mockReset();
  routeQuery.value = {};
  setupApiDefaults();
});

describe('InvoiceCreateView — tax rate', () => {
  it('hydrates tax_rate_pct from /api/tax/resolve', async () => {
    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    // 0.0738 decimal → 7.38%
    expect(wrapper.find('[data-testid="invoice-tax-rate"]').element.value).toBe('7.38');
  });

  it('leaves tax_rate at 0 when /api/tax/resolve fails or returns 0', async () => {
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0, rate_pct: 0 });
      return Promise.resolve([]);
    });
    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="invoice-tax-rate"]').element.value).toBe('0');
  });

  it('re-resolves to 0 for an exempt customer and POSTs tax_rate 0', async () => {
    routeQuery.value = { job_id: 'job-1' };  // derives customer cust-1
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      // Exempt customer → 0; the customer-less default is 7.38%.
      if (url.startsWith('/api/tax/resolve?customer_id=cust-1')) {
        return Promise.resolve({ rate: 0, rate_pct: 0 });
      }
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0.0738, rate_pct: 7.38 });
      return Promise.resolve([]);
    });
    apiPost.mockResolvedValue({ id: 'inv-3', invoice_number: 'INV-0003' });

    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="invoice-tax-rate"]').element.value).toBe('0');

    await wrapper.find('[data-testid="le-set-line"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="invoice-create-submit"]').trigger('click');
    await flushPromises();

    expect(apiPost.mock.calls[0][1].tax_rate).toBe(0);
  });
});

describe('InvoiceCreateView — query prefill', () => {
  it('prefills customer + job from ?job_id= and pulls estimate lines', async () => {
    routeQuery.value = { job_id: 'job-1' };
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0, rate_pct: 0 });
      if (url.startsWith('/api/estimates?job_id=job-1')) {
        return Promise.resolve([{ id: 'est-1', notes: 'Original quote' }]);
      }
      if (url === '/api/estimates/est-1') {
        return Promise.resolve({
          lines: [
            { description: 'Spring', quantity: 1, unit_price: 80, category: 'parts' },
            { description: 'Labor', quantity: 2, unit_price: 50, category: 'labor' },
          ],
        });
      }
      return Promise.resolve([]);
    });

    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();

    // LineEditor stub receives both lines + the job-id binding.
    expect(wrapper.find('[data-testid="le-job-id"]').text()).toBe('job-1');
    expect(wrapper.find('[data-testid="le-line-count"]').text()).toBe('2');
  });
});

describe('InvoiceCreateView — submit', () => {
  it('disables submit when customer or job is empty', async () => {
    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    expect(wrapper.find('[data-testid="invoice-create-submit"]').attributes('disabled')).toBeDefined();
  });

  it('POSTs the canonical payload shape: tax_rate as decimal, from_part_ids list, taxable on lines', async () => {
    routeQuery.value = { job_id: 'job-1' };
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0.0825, rate_pct: 8.25 });
      return Promise.resolve([]);
    });
    apiPost.mockResolvedValue({ id: 'inv-1', invoice_number: 'INV-0001' });

    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();

    // Adopt a line + a part-id via the LineEditor stub.
    await wrapper.find('[data-testid="le-set-line"]').trigger('click');
    await wrapper.find('[data-testid="le-add-part"]').trigger('click');
    await flushPromises();

    await wrapper.find('[data-testid="invoice-create-submit"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/invoices', expect.objectContaining({
      customer_id: 'cust-1',
      job_id: 'job-1',
      tax_rate: 0.0825,
      from_part_ids: ['p1'],
      line_items: [{
        description: 'Spring',
        quantity: 1,
        unit_price: 50,
        taxable: true,
        // S122-b parity fields when set
        category: 'Springs',
        cost: 28,
        margin_pct_override: 0.4,  // 40% → 0.40 decimal at POST boundary
      }],
    }));
  });

  it('mounts LineItemEditor with show-cost, show-margin, and categories', async () => {
    routeQuery.value = { job_id: 'job-1' };
    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-testid="le-show-cost"]').text()).toBe('yes');
    expect(wrapper.find('[data-testid="le-show-margin"]').text()).toBe('yes');
    // 6 categories: Doors, Openers, Springs, Labor, Parts, Other
    expect(wrapper.find('[data-testid="le-cat-count"]').text()).toBe('6');
  });

  it('routes to /billing/:id on success', async () => {
    routeQuery.value = { job_id: 'job-1' };
    apiPost.mockResolvedValue({ id: 'inv-99', invoice_number: 'INV-0099' });
    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();

    await wrapper.find('[data-testid="le-set-line"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="invoice-create-submit"]').trigger('click');
    await flushPromises();

    expect(routerPush).toHaveBeenCalledWith('/billing/inv-99');
  });

  it('sends an EXPLICIT tax_rate of 0 when the form value is zero (null would make the backend re-apply the default)', async () => {
    routeQuery.value = { job_id: 'job-1' };
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS);
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0, rate_pct: 0 });
      return Promise.resolve([]);
    });
    apiPost.mockResolvedValue({ id: 'inv-2', invoice_number: 'INV-0002' });

    const wrapper = mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    await wrapper.find('[data-testid="le-set-line"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-testid="invoice-create-submit"]').trigger('click');
    await flushPromises();

    const payload = apiPost.mock.calls[0][1];
    expect(payload.tax_rate).toBe(0);
  });
});

describe('InvoiceCreateView — bulk-fetch query contract', () => {
  it('asks /api/customers for per_page=1000 (not page_size — the endpoint ignores it)', async () => {
    mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();
    const customerUrls = apiGet.mock.calls
      .map(([url]) => url)
      .filter((u) => typeof u === 'string' && u.startsWith('/api/customers'));
    expect(customerUrls.length).toBeGreaterThan(0);
    // Every customers list call must use per_page (the real endpoint's param
    // name). The 2026-05-11 bug: page_size=500 was silently dropped and the
    // response capped at the default 50 — older customers fell off, the
    // Ready-for-Billing pre-fill rendered a blank Select.
    for (const u of customerUrls) {
      if (u === '/api/customers' || u.startsWith('/api/customers/')) continue; // single-customer fetch
      expect(u).toMatch(/per_page=\d+/);
      expect(u).not.toMatch(/[?&]page_size=/);
    }
  });

  it('fetches /api/customers/:id by ID when the URL pre-fill is missing from the bulk list', async () => {
    // Simulate a tenant where customer X exists but is NOT in the first
    // bulk-load page — exactly the >1000-customer case ensureCustomerLoaded
    // covers.
    routeQuery.value = { job_id: 'job-1', customer_id: 'cust-missing' };
    apiGet.mockImplementation((url) => {
      if (url === '/api/customers/cust-missing') {
        return Promise.resolve({ id: 'cust-missing', name: 'Older Customer', phone: '555-9' });
      }
      if (url.startsWith('/api/customers')) return Promise.resolve(CUSTOMERS); // bulk list — no cust-missing
      if (url.startsWith('/api/jobs?')) return Promise.resolve(JOBS);
      if (url.startsWith('/api/tax/resolve')) return Promise.resolve({ rate: 0, rate_pct: 0 });
      return Promise.resolve([]);
    });

    mount(InvoiceCreateView, { global: { stubs } });
    await flushPromises();

    const calledById = apiGet.mock.calls
      .map(([url]) => url)
      .some((u) => u === '/api/customers/cust-missing');
    expect(calledById).toBe(true);
  });
});
