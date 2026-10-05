/**
 * EstimateView — Duplicate (2026-09-29).
 *
 * Duplicate used to push /estimates/<copy> while the ORIGINAL stayed on
 * screen (the remount half is pinned in lib/__tests__/viewRemount.spec.js).
 * This spec pins the view's own half, on the mounted view:
 *  1. Unsaved edits are flushed BEFORE the duplicate POST — the server copies
 *     what is saved, so a copy made first would miss the last edits.
 *  3. Leaving mid-duplicate (another estimate opened while the flush is in
 *     flight) copies nothing — not even the estimate the user moved to — and
 *     does not yank the user to a copy.
 *  4. The draft flip (/estimates/new -> /estimates/<id> after autosave
 *     creates the draft) marks EXACTLY the path it then navigates to, before
 *     navigating — or KeyedRouterView remounts the form mid-flip and the
 *     pre-pick lines, input and queued photos are lost.
 *  2. When that flush fails, nothing is copied and the user is told why —
 *     forceFlush swallows its error into autosaveState, so without the check
 *     the copy silently comes from the older saved version.
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


describe('EstimateView — Duplicate', () => {
  it('saves pending edits first, then duplicates, then opens the copy', async () => {
    apiPatch.mockReset().mockResolvedValue({});
    apiPost.mockReset().mockImplementation(async (url) => (
      url === '/api/estimates/est-1/duplicate' ? { id: 'est-2', estimate_number: 'EST-0001-1' } : {}
    ));
    const wrapper = mountView();
    await flushPromises();
    apiPatch.mockClear();

    await wrapper.get('[data-testid="estimate-duplicate"]').trigger('click');
    await flushPromises();

    const headerPatch = apiPatch.mock.calls.findIndex(([url]) => url === '/api/estimates/est-1');
    const dupCall = apiPost.mock.calls.findIndex(([url]) => url === '/api/estimates/est-1/duplicate');
    expect(headerPatch).toBeGreaterThanOrEqual(0);
    expect(dupCall).toBeGreaterThanOrEqual(0);
    expect(apiPatch.mock.invocationCallOrder[headerPatch])
      .toBeLessThan(apiPost.mock.invocationCallOrder[dupCall]);
    expect(routerPush).toHaveBeenCalledWith('/estimates/est-2');
  });

  it('copies nothing when the user leaves while the pre-copy save is in flight', async () => {
    let releasePatch;
    apiPatch.mockReset().mockImplementation(() => new Promise((r) => { releasePatch = r; }));
    apiPost.mockReset().mockResolvedValue({ id: 'est-copy' });
    const wrapper = mountView();
    await flushPromises();
    releasePatch?.({});  // let any load-time flush settle
    await flushPromises();
    apiPost.mockClear();

    await wrapper.get('[data-testid="estimate-duplicate"]').trigger('click');
    await flushPromises();
    // The user opens estimate 9 (command palette) and this view unmounts.
    routeMock.params = { id: 'est-9' };
    wrapper.unmount();
    releasePatch({});
    await flushPromises();

    expect(apiPost.mock.calls.filter(([url]) => String(url).endsWith('/duplicate'))).toEqual([]);
    expect(routerPush).not.toHaveBeenCalledWith('/estimates/est-copy');
  });

  it('refuses to copy when the pending edits could not be saved', async () => {
    apiPatch.mockReset().mockRejectedValue(new Error('500'));
    apiPost.mockReset().mockResolvedValue({ id: 'est-2' });
    const wrapper = mountView();
    await flushPromises();

    await wrapper.get('[data-testid="estimate-duplicate"]').trigger('click');
    await flushPromises();

    expect(apiPost.mock.calls.some(([url]) => url === '/api/estimates/est-1/duplicate')).toBe(false);
    expect(routerPush).not.toHaveBeenCalledWith('/estimates/est-2');
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Not duplicated' }));
  });
});

describe('EstimateView — the draft flip keeps the form mounted', () => {
  it('marks the exact path it navigates to, before navigating', async () => {
    routeMock.params = {};
    routeMock.query = { customer_id: 'cust-1' };
    apiPost.mockReset().mockImplementation(async (url) => (
      url === '/api/estimates' ? { id: 'est-new', estimate_number: 'EST-0002' } : {}
    ));
    try {
      mountView();
      await flushPromises();
      // The customer pick arms a 500 ms debounce before the draft POST.
      await new Promise((r) => setTimeout(r, 700));
      await flushPromises();

      expect(routerReplace).toHaveBeenCalledWith('/estimates/est-new');
      expect(keepSpy).toHaveBeenCalledWith('/estimates/est-new');
      expect(keepSpy.mock.invocationCallOrder[0])
        .toBeLessThan(routerReplace.mock.invocationCallOrder[0]);
    } finally {
      routeMock.query = {};
    }
  });
});
