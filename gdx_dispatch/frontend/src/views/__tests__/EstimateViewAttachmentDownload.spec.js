/**
 * EstimateView — an uploaded attachment is saved, never opened in a tab (GDXA-973).
 *
 * The attachment link used to open the file as a blob: URL in a new tab. A
 * blob: URL inherits the app's origin and keeps the server's content type, so
 * HTML or SVG opened that way would run script with the session's reach. The
 * upload allowlist admits only images and PDFs today, so the HTML fixture here
 * stands for that list widening; the click must save the file to disk under
 * its own name either way.
 *
 * The real useAuthedFile composable runs here; only the network (fetch) and
 * the browser primitives jsdom lacks (createObjectURL, window.open) are stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiDel = vi.fn();
const toastAdd = vi.fn();
const routeMock = { params: { id: 'est-1' }, query: {} };

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: apiDel }),
}));
vi.mock('../../composables/useEstimateSources', async () => {
  const { ref } = await import('vue');
  return {
    classifyPickerError: () => 'unknown',
    useEstimateSources: () => ({ sources: ref([]), discover: vi.fn() }),
  };
});
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: (opts) => opts.accept?.() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => routeMock,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import EstimateView from '../EstimateView.vue';

const ESTIMATE = {
  id: 'est-1', estimate_number: 'EST-0001', status: 'draft', customer_id: 'cust-1',
  lines: [], created_at: '2026-09-01T12:00:00Z', expires_at: '2026-10-01',
};
const HTML_ATTACHMENT = {
  id: 'att-1', original_name: 'door-quote.html', content_type: 'text/html',
  file_size: 120, uploaded_by: 'office', label: null,
  download_url: '/api/estimates/est-1/attachments/att-1/download',
};

function routeGet(url) {
  if (url === '/api/estimates/est-1') return ESTIMATE;
  if (url === '/api/estimates/est-1/attachments') return [HTML_ATTACHMENT];
  if (url.startsWith('/api/estimates/est-1/activity')) return { items: [], total: 0, context: null };
  if (url === '/api/tax/config' || url === '/api/pricing-engine/settings' || url === '/api/estimates-features') return {};
  return [];
}

const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  RouterLink: { props: ['to'], template: '<a :href="to"><slot /></a>' },
  Button: {
    props: ['label', 'disabled', 'type'],
    emits: ['click'],
    template: '<button :type="type || \'button\'" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Select: { props: ['modelValue', 'options'], template: '<select></select>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  InputNumber: { props: ['modelValue'], template: '<input />' },
  Textarea: { props: ['modelValue'], template: '<textarea />' },
  ToggleSwitch: { props: ['modelValue'], template: '<input type="checkbox" />' },
  Checkbox: { props: ['modelValue'], template: '<input type="checkbox" />' },
  RadioButton: { props: ['modelValue'], template: '<input type="radio" />' },
  DatePicker: { props: ['modelValue'], template: '<input />' },
  DataTable: passthrough,
  Column: { template: '<span><slot /></span>' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Divider: { template: '<hr />' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Toast: { template: '<div />' },
  EstimateProfitPanel: { template: '<div />' },
  CatalogPickerDialog: { template: '<div />' },
  ComposerPdfPreview: { template: '<div />' },
  EstimateStatusContext: { template: '<div />' },
  PluginScreen: { template: '<div />' },
  PhoneInput: { template: '<input />' },
  PaymentCaptureForm: { template: '<div />' },
  CustomerFormDialog: { template: '<div />' },
};

let windowOpen;
let clickedAnchors;
let fetchSpy;

beforeEach(() => {
  setActivePinia(createPinia());
  apiGet.mockReset().mockImplementation(async (url) => routeGet(url));
  apiPatch.mockReset().mockResolvedValue({});
  routeMock.params = { id: 'est-1' };

  fetchSpy = vi.fn(async () => new Response(
    new Blob(['<script>alert(document.cookie)</script>'], { type: 'text/html' }),
    { status: 200 },
  ));
  vi.stubGlobal('fetch', fetchSpy);
  URL.createObjectURL = vi.fn(() => 'blob:http://localhost/att-1');
  URL.revokeObjectURL = vi.fn();
  windowOpen = vi.spyOn(window, 'open').mockImplementation(() => ({}));
  clickedAnchors = [];
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function record() {
    clickedAnchors.push({ href: this.getAttribute('href'), download: this.getAttribute('download') });
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('EstimateView — attachment open', () => {
  it('saves an uploaded HTML attachment under its own name instead of opening a tab', async () => {
    const wrapper = mount(EstimateView, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();

    const link = wrapper.get('[data-testid="estimate-attachment-list"] .attachment-name');
    expect(link.text()).toBe('door-quote.html');
    await link.trigger('click');
    await flushPromises();

    expect(fetchSpy).toHaveBeenCalledWith(HTML_ATTACHMENT.download_url, expect.anything());
    expect(windowOpen).not.toHaveBeenCalled();
    expect(clickedAnchors).toEqual([
      { href: 'blob:http://localhost/att-1', download: 'door-quote.html' },
    ]);
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Download failed' }));
  });
});
