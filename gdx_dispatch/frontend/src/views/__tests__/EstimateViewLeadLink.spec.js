/**
 * EstimateView — a new estimate opened from a lead carries the lead
 * (2026-09-29).
 *
 * The Leads page opens /estimates/new?customer_id=<lead's customer>&lead_id=.
 * The draft autosave-create must send that lead_id — and must NOT when the
 * user picked a different customer, because a lead link means "this person's
 * estimate" and the server would refuse it (422), stranding the draft.
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
const keepSpy = vi.hoisted(() => vi.fn());
vi.mock('../../lib/viewRemount', async (importOriginal) => ({
  ...(await importOriginal()),
  keepMountedThroughNextNavigation: keepSpy,
}));

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
// The dialog's Accept, pressed: run the accept callback the view registers.
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: (opts) => opts.accept?.() }) }));
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



describe('EstimateView — lead link on create', () => {
  async function draftCreatePayload() {
    apiPost.mockReset().mockImplementation(async (url) => (
      url === '/api/estimates' ? { id: 'est-new', estimate_number: 'EST-0002' } : {}
    ));
    mountView();
    await flushPromises();
    await new Promise((r) => setTimeout(r, 700));  // the 500 ms pick debounce
    await flushPromises();
    const call = apiPost.mock.calls.find(([url]) => url === '/api/estimates');
    return call?.[1];
  }

  afterEach(() => { routeMock.query = {}; });

  it("sends the lead when the estimate is for the lead's customer", async () => {
    routeMock.params = {};
    routeMock.query = { customer_id: 'cust-1', lead_id: 'lead-1' };
    const payload = await draftCreatePayload();
    expect(payload).toBeTruthy();
    expect(payload.customer_id).toBe('cust-1');
    expect(payload.lead_id).toBe('lead-1');
  });

  it('sends no lead for an estimate not opened from a lead', async () => {
    routeMock.params = {};
    routeMock.query = { customer_id: 'cust-1' };
    const payload = await draftCreatePayload();
    expect(payload).toBeTruthy();
    expect(payload.lead_id).toBeNull();
  });
});
