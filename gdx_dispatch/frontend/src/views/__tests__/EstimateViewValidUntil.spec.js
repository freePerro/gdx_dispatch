/**
 * EstimateView — valid_until is stored only when the user picks it (GDXA-232).
 *
 * The form used to seed valid_until = today + 30, on a new estimate and on any
 * loaded estimate whose valid_until was NULL. Loading fires the form's deep
 * watcher, so the very first autosave PATCH wrote that default to the row, and
 * send keeps any still-future valid_until: the tenant's estimate_expiry_days
 * (default 60) never applied to an estimate opened in the editor.
 *
 * The autosave now sends valid_until only when the user changed it from what
 * the server last returned. Sending it unconditionally (even as null) would
 * wipe the date send stamps server-side, since send does not refresh the form.
 *
 * Mounted, asserting on the PATCH the autosave actually sends:
 *  1. A NULL valid_until loads as blank and autosave leaves the field alone.
 *  2. A stored date is left alone too; picking one sends it, Clear sends null.
 *  3. Blank shows the tenant's estimate_expiry_days as the hint.
 *  4. A reopened estimate (sent_at kept) gets the same hint.
 *  5. Sent from the editor then edited: the stamped date is not overwritten.
 *  6. A new estimate starts blank too.
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
  lines: [], created_at: '2026-09-01T12:00:00Z', valid_until: null,
};

let estimateRow;

function routeGet(url) {
  if (url === '/api/estimates/est-1') return estimateRow;
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url === '/api/estimates-features') return { estimate_expiry_days: 45 };
  if (url === '/api/tax/config' || url === '/api/pricing-engine/settings') return {};
  return [];
}

const stub = (tag = 'div') => ({ template: `<${tag}><slot /></${tag}>` });
const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  Select: { props: ['modelValue', 'options'], template: '<select></select>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  ToggleSwitch: { props: ['modelValue'], template: '<input type="checkbox" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  DatePicker: {
    props: ['modelValue', 'placeholder'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :placeholder="placeholder" />',
    inheritAttrs: false,
  },
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
  await flushPromises();
  // The load reassigns the form, which arms the 800 ms autosave debounce.
  await vi.advanceTimersByTimeAsync(1000);
  await flushPromises();
  return wrapper;
}

function headerPatches() {
  return apiPatch.mock.calls.filter(([url]) => url === '/api/estimates/est-1').map(([, body]) => body);
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setActivePinia(createPinia());
  estimateRow = { ...ESTIMATE };
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiPatch.mockReset().mockResolvedValue({});
  routeMock.params = { id: 'est-1' };
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('EstimateView — valid_until is a user choice, not a seeded default', () => {
  it('a NULL valid_until loads blank and autosave does not write the field', async () => {
    const wrapper = await mountAndSettle();

    expect(wrapper.vm.form.valid_until).toBeNull();
    const patches = headerPatches();
    expect(patches.length).toBeGreaterThan(0);
    for (const body of patches) expect(body).not.toHaveProperty('valid_until');
  });

  it('a stored date is left alone; a picked date is sent; Clear sends null', async () => {
    estimateRow = { ...ESTIMATE, valid_until: '2026-11-20T00:00:00+00:00' };
    const wrapper = await mountAndSettle();
    for (const body of headerPatches()) expect(body).not.toHaveProperty('valid_until');

    apiPatch.mockClear();
    wrapper.vm.form.valid_until = new Date(2026, 11, 4);
    await settle();
    expect(headerPatches().at(-1).valid_until).toBe('2026-12-04');

    apiPatch.mockClear();
    wrapper.vm.form.valid_until = null;
    await settle();
    expect(headerPatches().at(-1).valid_until).toBeNull();

    apiPatch.mockClear();
    wrapper.vm.form.notes = 'later edit';
    await settle();
    expect(headerPatches().at(-1)).not.toHaveProperty('valid_until');
  });

  it("blank shows the tenant's estimate_expiry_days, and a set date hides the hint", async () => {
    const blank = await mountAndSettle();
    expect(blank.get('[data-testid="estimate-valid-until"]').attributes('placeholder'))
      .toBe("45 days after the next send");
    expect(blank.get('[data-testid="estimate-valid-until-hint"]').text())
      .toContain("45 days after the next send");
    blank.unmount();

    estimateRow = { ...ESTIMATE, valid_until: '2026-11-20T00:00:00+00:00' };
    const set = await mountAndSettle();
    expect(set.find('[data-testid="estimate-valid-until-hint"]').exists()).toBe(false);
  });

  it('a reopened estimate (draft, sent_at kept) still gets the hint', async () => {
    // Reopen keeps sent_at and nulls valid_until; the next send stamps
    // sent_at + estimate_expiry_days again, so the hint stays true.
    estimateRow = { ...ESTIMATE, status: 'draft', sent_at: '2026-09-02T12:00:00Z' };
    const wrapper = await mountAndSettle();
    expect(wrapper.get('[data-testid="estimate-valid-until-hint"]').text())
      .toContain('45 days after the next send');
  });

  it('sent from the editor then edited: the date send stamped is not overwritten', async () => {
    // sendComposer only flips the status; the form keeps valid_until null
    // while the server has stamped sent_at + estimate_expiry_days.
    const wrapper = await mountAndSettle();
    apiPatch.mockClear();
    wrapper.vm.estimate.status = 'Sent';
    wrapper.vm.form.notes = 'post-send edit';
    await settle();

    const patches = headerPatches();
    expect(patches.length).toBeGreaterThan(0);
    for (const body of patches) expect(body).not.toHaveProperty('valid_until');
  });

  it('a new estimate starts blank', async () => {
    routeMock.params = {};
    const wrapper = await mountAndSettle();
    expect(wrapper.find('[data-testid="estimate-valid-until-hint"]').exists()).toBe(true);
    expect(headerPatches()).toEqual([]);
  });
});
