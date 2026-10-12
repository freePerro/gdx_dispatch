/**
 * EstimateView — a failed /api/tax/config read is not a 0% default (GDXA-768).
 *
 * loadTenantTaxDefault used to swallow the failure and leave the default at 0.
 * A NULL-rate estimate then loaded as "Tax Rate 0%", the Tax row hid and the
 * total left the tax out, while the server (compute_estimate_totals) charges
 * TaxConfig.default_rate on the PDF, portal and invoice.
 *
 * Mounted, asserting on what renders and on what autosave sends:
 *  1. Config fails, NULL rate: warning shown, no 0% in the form, tax and total
 *     blank, and an edit still saves tax_rate null (the row is not changed).
 *  2. Retry that succeeds fills the default in, clears the warning, writes
 *     nothing by itself, and the next save still sends null.
 *  3. Config fails, explicit rate: the stored rate shows and saves as stored.
 *  4. Config loads: no warning, NULL rate shows the default and saves null.
 *  5. Config fails, explicit rate with labor lines: warns, because whether
 *     labor is taxed is the tenant's setting, which was not read.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const routeMock = { params: { id: 'est-1' }, query: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  openAuthedFile: vi.fn(),
  createAuthedBlobUrl: vi.fn(),
}));
vi.mock('../../composables/useEstimateSources', async () => {
  const { ref } = await import('vue');
  return {
    classifyPickerError: () => 'unknown',
    useEstimateSources: () => ({ sources: ref([]), discover: vi.fn() }),
  };
});
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import EstimateView from '../EstimateView.vue';

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-0001', status: 'draft', customer_id: 'cust-1',
  created_at: '2026-09-01T12:00:00Z', tax_rate: null,
  lines: [{ id: 'l1', description: 'Spring', quantity: 1, unit_price: 100, category: 'Parts' }],
};

let estimateRow;
let taxConfigFails;

function routeGet(url) {
  if (url === '/api/estimates/est-1') return estimateRow;
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url === '/api/tax/config') {
    if (taxConfigFails) throw Object.assign(new Error('Server error'), { status: 500 });
    return { default_rate: 0.0825, tax_labor: false };
  }
  if (url === '/api/pricing-engine/settings') return { loaded_labor_cost_per_hour: 40 };
  if (url === '/api/estimates-features') return {};
  return [];
}

const stub = (tag = 'div') => ({ template: `<${tag}><slot /></${tag}>` });
const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Button: {
    props: ['label'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], template: '<select></select>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  InputNumber: {
    props: ['modelValue', 'placeholder'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue ?? \'\'" :placeholder="placeholder" />',
    inheritAttrs: false,
  },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  ToggleSwitch: { props: ['modelValue'], template: '<input type="checkbox" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  DataTable: stub(), Column: stub('span'), Divider: { template: '<hr />' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Toast: stub(), EstimateProfitPanel: stub(), CatalogPickerDialog: stub(),
  ComposerPdfPreview: stub(), EstimateStatusContext: stub(), PluginScreen: stub(),
  CustomerFormDialog: stub(), PhoneInput: { template: '<input />' }, PaymentCaptureForm: stub(),
};

async function settle() {
  await flushPromises();
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
}

async function mountAndSettle() {
  const wrapper = mount(EstimateView, { global: { stubs, directives: { tooltip: {} } } });
  await settle();
  return wrapper;
}

function headerPatches() {
  return apiPatch.mock.calls.filter(([url]) => url === '/api/estimates/est-1').map(([, body]) => body);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setActivePinia(createPinia());
  estimateRow = { ...ESTIMATE };
  taxConfigFails = false;
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiPatch.mockReset().mockResolvedValue({});
  routeMock.params = { id: 'est-1' };
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('EstimateView — a failed tax config read is not a 0% default', () => {
  it('NULL rate, config fails: warns, shows no 0%, blanks tax and total, saves null', async () => {
    taxConfigFails = true;
    const wrapper = await mountAndSettle();

    expect(wrapper.get('[data-testid="estimate-tax-config-warning"]').text())
      .toContain('may differ from what is billed');
    expect(wrapper.vm.form.tax_rate).toBeNull();
    const field = wrapper.get('[data-testid="estimate-tax-rate"]');
    expect(field.element.value).toBe('');
    expect(field.attributes('placeholder')).toBe('Default unavailable');
    expect(wrapper.get('[data-testid="estimate-tax"]').text()).toBe('—');
    expect(wrapper.get('[data-testid="estimate-total"]').text()).toBe('—');
    expect(wrapper.text()).not.toContain('Tax (0%)');

    wrapper.vm.form.notes = 'an edit';
    await settle();
    const patches = headerPatches();
    expect(patches.length).toBeGreaterThan(0);
    for (const body of patches) expect(body.tax_rate).toBeNull();
  });

  it('a retry that succeeds fills in the default and the save still sends null', async () => {
    taxConfigFails = true;
    const wrapper = await mountAndSettle();
    taxConfigFails = false;
    apiPatch.mockClear();

    await wrapper.get('[data-testid="estimate-tax-config-retry"]').trigger('click');
    await settle();
    // Filling in the default is not an edit: no write until the user makes one.
    expect(headerPatches()).toEqual([]);

    expect(wrapper.find('[data-testid="estimate-tax-config-warning"]').exists()).toBe(false);
    expect(wrapper.vm.form.tax_rate).toBe(8.25);
    expect(wrapper.get('[data-testid="estimate-tax"]').text()).toBe('$8.25');
    expect(wrapper.get('[data-testid="estimate-total"]').text()).toBe('$108.25');

    apiPatch.mockClear();
    wrapper.vm.form.notes = 'after retry';
    await settle();
    expect(headerPatches().at(-1).tax_rate).toBeNull();
  });

  it('explicit rate, config fails: the stored rate shows and saves as stored', async () => {
    taxConfigFails = true;
    estimateRow = { ...ESTIMATE, tax_rate: 0.05 };
    const wrapper = await mountAndSettle();

    expect(wrapper.find('[data-testid="estimate-tax-config-warning"]').exists()).toBe(false);
    expect(wrapper.vm.form.tax_rate).toBe(5);
    expect(wrapper.get('[data-testid="estimate-total"]').text()).toBe('$105.00');

    wrapper.vm.form.notes = 'an edit';
    await settle();
    expect(headerPatches().at(-1).tax_rate).toBeCloseTo(0.05);
  });

  it('config loads: no warning, NULL rate shows the default and saves null', async () => {
    const wrapper = await mountAndSettle();

    expect(wrapper.find('[data-testid="estimate-tax-config-warning"]').exists()).toBe(false);
    expect(wrapper.vm.form.tax_rate).toBe(8.25);
    expect(wrapper.get('[data-testid="estimate-total"]').text()).toBe('$108.25');

    wrapper.vm.form.notes = 'an edit';
    await settle();
    expect(headerPatches().at(-1).tax_rate).toBeNull();
  });

  it('explicit rate with labor, config fails: warns, since labor taxability is unknown', async () => {
    taxConfigFails = true;
    estimateRow = {
      ...ESTIMATE,
      tax_rate: 0.05,
      lines: [
        ...ESTIMATE.lines,
        { id: 'l2', description: 'Install', quantity: 1, unit_price: 200, category: 'Labor' },
      ],
    };
    const wrapper = await mountAndSettle();

    expect(wrapper.find('[data-testid="estimate-tax-config-warning"]').exists()).toBe(true);
    expect(wrapper.vm.form.tax_rate).toBe(5);
    // The rate is known, so the tax and total still show.
    expect(wrapper.get('[data-testid="estimate-total"]').text()).not.toBe('—');
  });
});
