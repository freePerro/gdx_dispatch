/**
 * EstimateView — files picked in the send composer (2026-10-16).
 *
 * Pins:
 *  - the picks go to /send as attachment_refs, beside the estimate's own PDF
 *    (the server's handling is pinned by test_email_attachments_composers.py);
 *  - a refused pick (404/413) or a lost connection is said once, by the view;
 *    a 401 is left to useApi's sign-in handling;
 *  - an Outlook send that was not confirmed says to check Sent Items, not to
 *    retry — a second click is the only way it could go twice.
 *
 * Harness copied from EstimateViewVersionConflict.spec.js.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const routeMock = { params: { id: 'est-1' }, query: {} };
const toastAdd = vi.fn();

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
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import EstimateView from '../EstimateView.vue';

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-0001', status: 'draft', customer_id: 'cust-1',
  lines: [], created_at: '2026-09-01T12:00:00Z', valid_until: null, version: 3,
};

let estimateRow;
let taxConfig = {};

function routeGet(url) {
  if (url === '/api/estimates/est-1') return estimateRow;
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url === '/api/estimates-features') return { estimate_expiry_days: 45 };
  if (url === '/api/tax/config') return taxConfig;
  if (url === '/api/pricing-engine/settings') return {};
  return [];
}

const stub = (tag = 'div') => ({ template: `<${tag}><slot /></${tag}>` });
const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Button: { props: ['label', 'disabled'], template: '<button :data-testid="$attrs[\'data-testid\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>', inheritAttrs: false },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
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
  EmailAttachmentPicker: {
    props: ['modelValue', 'jobId', 'customerId', 'disabled'],
    template: '<div data-testid="picker-stub" :data-customer="customerId" />',
  },
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


const PICK = { kind: 'estimate_pdf', id: 'est-9', name: 'estimate-E-9.pdf' };

async function composeTo(wrapper, picks) {
  wrapper.vm.composer.customer_id = 'cust-1';
  wrapper.vm.composer.to = 'customer@example.com';
  wrapper.vm.composerPicked = picks;
  await flushPromises();
}

function sendCall() {
  return apiPost.mock.calls.find(([url]) => url === '/api/estimates/est-1/send');
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  setActivePinia(createPinia());
  estimateRow = { ...ESTIMATE };
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiPatch.mockReset().mockResolvedValue({});
  apiPost.mockReset().mockResolvedValue({});
  toastAdd.mockReset();
  routeMock.params = { id: 'est-1' };
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('EstimateView — composer attachments', () => {
  it('sends the picks as attachment_refs and leaves only a 401 to useApi', async () => {
    apiPost.mockResolvedValue({ email_sent: true, status: 'sent' });
    const w = await mountAndSettle();
    await composeTo(w, [PICK]);
    await w.vm.sendComposer();
    await settle();

    const [, body, opts] = sendCall();
    expect(body.attachment_refs).toEqual([{ kind: 'estimate_pdf', id: 'est-9' }]);
    expect(opts.suppressErrorToast({ status: 413 })).toBe(true);
    expect(opts.suppressErrorToast({ status: 401 })).toBe(false);
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'success' }));
  });

  it('with nothing picked, the body carries no attachment_refs', async () => {
    apiPost.mockResolvedValue({ email_sent: true, status: 'sent' });
    const w = await mountAndSettle();
    await composeTo(w, []);
    await w.vm.sendComposer();
    await settle();
    expect('attachment_refs' in sendCall()[1]).toBe(false);
  });

  it('an unconfirmed Outlook send says to check Sent Items', async () => {
    apiPost.mockResolvedValue({ email_sent: false, email_skip_reason: 'outlook_send_unconfirmed' });
    const w = await mountAndSettle();
    await composeTo(w, [PICK]);
    await w.vm.sendComposer();
    await settle();
    const t = toastAdd.mock.calls.map(([a]) => a).find((a) => a.severity === 'error');
    expect(t.detail).toContain('Check Sent Items before sending again');
  });

  it('a refused pick is said once, with the server reason', async () => {
    apiPost.mockRejectedValue(Object.assign(new Error('Attachment not found: estimate-E-9.pdf'), { status: 404 }));
    const w = await mountAndSettle();
    await composeTo(w, [PICK]);
    await w.vm.sendComposer();
    await settle();
    const errors = toastAdd.mock.calls.map(([a]) => a).filter((a) => a.severity === 'error');
    expect(errors).toHaveLength(1);
    expect(errors[0].detail).toBe('Attachment not found: estimate-E-9.pdf');
  });

  it('a 401 adds no toast of its own', async () => {
    apiPost.mockRejectedValue(Object.assign(new Error('Unauthorized'), { status: 401 }));
    const w = await mountAndSettle();
    await composeTo(w, [PICK]);
    await w.vm.sendComposer();
    await settle();
    expect(toastAdd.mock.calls.filter(([a]) => a.severity === 'error')).toHaveLength(0);
  });

  it('the open composer offers the picker, and Send is disabled while the picks are over the email cap', async () => {
    const w = await mountAndSettle();
    w.vm.showComposer = true;
    await composeTo(w, [PICK]);
    expect(w.get('[data-testid="picker-stub"]').attributes('data-customer')).toBe('cust-1');
    const sendBtn = () => w.get('[data-testid="composer-send"]');
    expect(sendBtn().attributes('disabled')).toBeUndefined();

    await composeTo(w, [{ kind: 'upload', name: 'big.mov', size_bytes: 19_000_000 }]);
    expect(sendBtn().attributes('disabled')).toBeDefined();
  });
});
