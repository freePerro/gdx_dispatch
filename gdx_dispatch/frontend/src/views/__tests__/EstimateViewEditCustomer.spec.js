/**
 * EstimateView — edit the current customer from the estimate (2026-09-21).
 *
 * Until this the estimate was the only document page with no way to its
 * customer: the picker is disabled once the estimate exists, the header name
 * was a plain span, and the only exit was /customers and a search. The invoice
 * page has had a name link and an "Edit Customer" button opening the shared
 * CustomerFormDialog since the initial release; this is the same wiring.
 *
 * Mounted (the first mount harness for this view — the earlier specs pin
 * source text, which proves presence and nothing else). Pins:
 *  1. An existing estimate with a customer renders the name as a link to
 *     /customers/{id} and an "Edit" button. A new estimate renders neither.
 *  2. Edit reads the row fresh (GET /api/customers/{id}, quietly — the
 *     client toasts by default) and opens the dialog in edit mode with THAT,
 *     not the picker row loaded at page open.
 *  3. When that read fails the dialog still opens on the picker's row.
 *  4. When it fails and the customer is not in the picker (a >500-customer
 *     list drops the oldest), no dialog opens on an empty shell — a save from
 *     {id, name} would blank the record — and the record page is the way in.
 *  5. @saved reloads the picker list, and the header shows the saved name —
 *     the list is the header's source (GET /estimates/{id} carries no name).
 *  6. A save whose list reload fails still shows the saved name: the emitted
 *     row is upserted first, so the header never renders the customer as gone.
 *  7. An existing estimate with no customer (duplicate of a deleted one)
 *     offers no link and no Edit — the button's own guard, not the header's.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const apiRawGet = vi.fn();
const toastAdd = vi.fn();
const routerPush = vi.fn();
const routerReplace = vi.fn();
const routeMock = { params: { id: 'est-1' }, query: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiRawGet, post: apiPost, patch: apiPatch, del: apiDel }),
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
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: routerPush, replace: routerReplace }),
}));

import EstimateView from '../EstimateView.vue';

// The picker list already carries every field the dialog edits (CustomerOut
// serializes notes and customer_type on the list route too). The two rows
// differ only in notes so a test can tell which one the dialog received.
const CUSTOMER_LISTED = {
  id: 'cust-1', name: 'Acme Door Co', phone: '5550142',
  email: 'ops@acme.example', address: '123 Main St', notes: 'as listed at page open',
};
const CUSTOMER_FRESH = { ...CUSTOMER_LISTED, notes: 'as the server holds it now' };
const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-0001', status: 'draft', customer_id: 'cust-1',
  lines: [], created_at: '2026-09-01T12:00:00Z', expires_at: '2026-10-01',
};

let customerList;

function routeGet(url) {
  if (url === '/api/estimates/est-1') return ESTIMATE;
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url.startsWith('/api/customers?')) return customerList;
  if (url === '/api/tax/config' || url === '/api/pricing-engine/settings' || url === '/api/estimates-features') return {};
  return [];
}

const baseStubs = {
  RouterLink: {
    props: ['to'],
    template: '<a :href="to" :data-testid="$attrs[\'data-testid\']"><slot /></a>',
    inheritAttrs: false,
  },
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'outlined', 'rounded', 'disabled', 'size', 'loading', 'type', 'link'],
    emits: ['click'],
    template: '<button :type="type || \'button\'" :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  Dialog: {
    props: ['visible', 'header'],
    emits: ['update:visible'],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /><slot name="footer" /></div>',
    inheritAttrs: false,
  },
  CustomerFormDialog: {
    props: ['visible', 'mode', 'customer'],
    emits: ['update:visible', 'saved'],
    template: '<div v-if="visible" data-testid="customer-form-dialog">'
      + 'stub:{{ mode }}:{{ customer?.id }}:{{ customer?.name }}:{{ customer?.notes }}'
      + '<button data-testid="stub-save" @click="$emit(\'saved\', customer)">save</button></div>',
  },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<select :data-testid="$attrs[\'data-testid\']" :value="modelValue"></select>',
    inheritAttrs: false,
  },
  InputText: { props: ['modelValue'], template: '<input :data-testid="$attrs[\'data-testid\']" />', inheritAttrs: false },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  ToggleSwitch: { props: ['modelValue'], template: '<input type="checkbox" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  DataTable: { props: ['value'], template: '<div><slot /></div>' },
  Column: { template: '<span><slot /></span>' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Divider: { template: '<hr />' },
  Tag: { props: ['value'], template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>', inheritAttrs: false },
  Toast: { template: '<div />' },
  EstimateProfitPanel: { template: '<div />' },
  CatalogPickerDialog: { template: '<div />' },
  ComposerPdfPreview: { template: '<div />' },
  EstimateStatusContext: { template: '<div />' },
  PluginScreen: { template: '<div />' },
  PhoneInput: { template: '<input />' },
  PaymentCaptureForm: { template: '<div />' },
};

function mountView() {
  return mount(EstimateView, {
    global: { stubs: baseStubs, directives: { tooltip: {} } },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  customerList = [CUSTOMER_LISTED];
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiRawGet.mockReset().mockResolvedValue(CUSTOMER_FRESH);
  routeMock.params = { id: 'est-1' };
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('EstimateView — edit the current customer', () => {
  it('links the header name to the customer record and offers Edit on an existing estimate', async () => {
    const wrapper = mountView();
    await flushPromises();

    const name = wrapper.get('[data-testid="estimate-customer"]');
    expect(name.element.tagName).toBe('A');
    expect(name.attributes('href')).toBe('/customers/cust-1');
    expect(name.text()).toBe('Acme Door Co');
    expect(wrapper.find('[data-testid="estimate-edit-customer-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="customer-form-dialog"]').exists()).toBe(false);
  });

  it('offers neither on a new estimate (no customer yet)', async () => {
    routeMock.params = {};
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="estimate-edit-customer-btn"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="estimate-customer"]').exists()).toBe(false);
  });

  it('Edit reads the row fresh, quietly, and opens the shared dialog in edit mode with it', async () => {
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="estimate-edit-customer-btn"]').trigger('click');
    await flushPromises();

    // The option is what keeps a failed warm-up read from toasting — drop it
    // and this goes red.
    expect(apiRawGet).toHaveBeenCalledWith('/api/customers/cust-1', { suppressErrorToast: true });
    const dialog = wrapper.get('[data-testid="customer-form-dialog"]');
    expect(dialog.text()).toContain('stub:edit:cust-1:Acme Door Co:as the server holds it now');
  });

  it("still opens on the picker's row when the fresh read fails", async () => {
    apiRawGet.mockRejectedValue(new Error('500'));
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="estimate-edit-customer-btn"]').trigger('click');
    await flushPromises();

    const dialog = wrapper.get('[data-testid="customer-form-dialog"]');
    expect(dialog.text()).toContain('stub:edit:cust-1:Acme Door Co:as listed at page open');
    expect(routerPush).not.toHaveBeenCalled();
  });

  it('never opens on an empty shell: read failed and customer not in the picker → record page', async () => {
    apiRawGet.mockRejectedValue(new Error('500'));
    customerList = [];
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="estimate-edit-customer-btn"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="customer-form-dialog"]').exists()).toBe(false);
    expect(routerPush).toHaveBeenCalledWith('/customers/cust-1');
  });

  it('a save reloads the picker list and the header shows the saved name', async () => {
    const wrapper = mountView();
    await flushPromises();
    const listCallsBefore = apiGet.mock.calls.filter(([u]) => u.startsWith('/api/customers?')).length;
    expect(listCallsBefore).toBe(1);

    await wrapper.get('[data-testid="estimate-edit-customer-btn"]').trigger('click');
    await flushPromises();
    customerList = [{ ...CUSTOMER_LISTED, name: 'Acme Door Company' }];
    await wrapper.get('[data-testid="stub-save"]').trigger('click');
    await flushPromises();

    const listCallsAfter = apiGet.mock.calls.filter(([u]) => u.startsWith('/api/customers?')).length;
    expect(listCallsAfter).toBe(2);
    expect(wrapper.get('[data-testid="estimate-customer"]').text()).toBe('Acme Door Company');
  });

  it('a save whose list reload fails still shows the saved name (upsert first)', async () => {
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="estimate-edit-customer-btn"]').trigger('click');
    await flushPromises();
    // The dialog emits the saved row; the reload that follows is a 429/5xx.
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/customers?')) throw new Error('429');
      return routeGet(url);
    });
    await wrapper.findComponent('[data-testid="customer-form-dialog"]').vm.$emit('saved', { ...CUSTOMER_FRESH, name: 'Acme Door Company' });
    await flushPromises();

    expect(wrapper.get('[data-testid="estimate-customer"]').text()).toBe('Acme Door Company');
    expect(wrapper.find('[data-testid="estimate-customer-contact"]').exists()).toBe(true);
  });

  it('an existing estimate with no customer offers neither link nor Edit', async () => {
    apiGet.mockImplementation(async (url) => (
      url === '/api/estimates/est-1' ? { ...ESTIMATE, customer_id: null } : routeGet(url)
    ));
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.find('[data-testid="estimate-edit-customer-btn"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="estimate-customer"]').element.tagName).toBe('SPAN');
  });
});
