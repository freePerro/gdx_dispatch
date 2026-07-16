import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'inv-1' } }) }));

const createAuthedBlobUrl = vi.fn();
vi.mock('../../composables/useAuthedFile', () => ({
  createAuthedBlobUrl: (...a) => createAuthedBlobUrl(...a),
}));

import VendorBillDetailView from '../VendorBillDetailView.vue';

const INVOICE = {
  id: 'inv-1', vendor_name_raw: 'Midwest Wholesale Doors', invoice_number: '90000001',
  total: '6125.21', due_date: '2026-07-30', terms: 'Net 30', status: 'open',
  matched_job_id: null, document_id: 'doc-1', notes: null,
  suggestions: [{ job_id: 'job-1', score: 0.9, reason: 'PO ~ customer', customer_name: 'Smith', job_title: 'garage', lifecycle_stage: 'scheduled' }],
  lines: [
    { id: 'l1', kind: 'item', description: 'CHI 3285 door', quantity: '2', unit_cost: '100', line_total: '200.00', disposition: 'pending', status: 'pending' },
    { id: 'l2', kind: 'item', description: 'roller box', quantity: '1', unit_cost: '50', line_total: '50.00', disposition: 'pending', status: 'pending' },
  ],
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  SelectButton: { props: ['modelValue'], template: '<div />' },
  Select: { props: ['modelValue', 'options'], template: '<select />' },
  InputText: { props: ['modelValue'], template: '<input />' },
  ProgressSpinner: { template: '<div />' },
  DataTable: { props: ['value'], template: '<table><tbody><tr v-for="(r,i) in value" :key="i"><slot name="body" :data="r" /></tr></tbody></table>' },
  Column: { template: '<td><slot name="body" :data="$attrs.data" /></td>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
};

function mountView() {
  return mount(VendorBillDetailView, { global: { stubs } });
}

describe('VendorBillDetailView', () => {
  beforeEach(() => {
    apiGet.mockReset(); apiPost.mockReset(); apiPatch.mockReset(); createAuthedBlobUrl.mockReset();
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/vendor-invoices/')) return Promise.resolve(JSON.parse(JSON.stringify(INVOICE)));
      if (url === '/api/inventory/parts') return Promise.resolve([{ id: 'item-1', part_name: '9x7 panel', sku: 'P1' }]);
      if (url.startsWith('/api/jobs')) return Promise.resolve([{ id: 'job-1', title: 'garage', customer_id: 'c1' }]);
      return Promise.resolve([]);
    });
    createAuthedBlobUrl.mockResolvedValue('blob:pdf');
  });

  it('loads the invoice and fetches the PDF blob for the attached document', async () => {
    const wrapper = mountView();
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/vendor-invoices/inv-1');
    expect(createAuthedBlobUrl).toHaveBeenCalledWith('/api/documents/doc-1/download');
    expect(wrapper.vm.pdfUrl).toBe('blob:pdf');
  });

  it('confirming a job line posts the job disposition (after a job is matched)', async () => {
    apiPatch.mockResolvedValue({ ...INVOICE, matched_job_id: 'job-1' });
    apiPost.mockResolvedValue({ ...INVOICE, lines: INVOICE.lines.map((l) => l.id === 'l1' ? { ...l, status: 'confirmed', disposition: 'job' } : l) });
    const wrapper = mountView();
    await flushPromises();

    await wrapper.vm.setMatch('job-1');           // header job match → PATCH
    expect(apiPatch).toHaveBeenCalledWith('/api/vendor-invoices/inv-1', { matched_job_id: 'job-1' });

    await wrapper.vm.confirmLine({ id: 'l1' });    // confirm the item line as job
    expect(apiPost).toHaveBeenCalledWith(
      '/api/vendor-invoices/inv-1/lines/l1/confirm',
      { disposition: 'job' },
      expect.anything(),
    );
  });

  it('confirming a stock line posts the chosen inventory_item_id', async () => {
    apiPost.mockResolvedValue({ ...INVOICE });
    const wrapper = mountView();
    await flushPromises();
    wrapper.vm.draft.l2.disposition = 'stock';
    wrapper.vm.draft.l2.inventory_item_id = 'item-1';
    await wrapper.vm.confirmLine({ id: 'l2' });
    expect(apiPost).toHaveBeenCalledWith(
      '/api/vendor-invoices/inv-1/lines/l2/confirm',
      { disposition: 'stock', inventory_item_id: 'item-1' },
      expect.anything(),
    );
  });

  it('a bill with NO suggestions can still be routed to a job via the picker', async () => {
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/vendor-invoices/')) return Promise.resolve({ ...JSON.parse(JSON.stringify(INVOICE)), suggestions: [] });
      if (url === '/api/inventory/parts') return Promise.resolve([]);
      if (url.startsWith('/api/jobs')) return Promise.resolve([{ id: 'job-1', title: 'garage', customer_id: 'c1' }]);
      return Promise.resolve([]);
    });
    apiPatch.mockResolvedValue({ ...INVOICE, suggestions: [], matched_job_id: 'job-1' });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.vm.jobOptions.length).toBe(1);       // picker is populated even with 0 suggestions
    expect(wrapper.vm.canConfirm({ id: 'l1' })).toBe(false); // no job yet
    wrapper.vm.jobPick = 'job-1';
    await wrapper.vm.onJobPick();
    expect(apiPatch).toHaveBeenCalledWith('/api/vendor-invoices/inv-1', { matched_job_id: 'job-1' });
    expect(wrapper.vm.canConfirm({ id: 'l1' })).toBe(true);  // now confirmable
  });

  it('freight/tax lines cannot be routed to stock', async () => {
    const wrapper = mountView();
    await flushPromises();
    const itemOpts = wrapper.vm.dispositionOptionsFor({ kind: 'item' }).map((o) => o.value);
    const freightOpts = wrapper.vm.dispositionOptionsFor({ kind: 'freight' }).map((o) => o.value);
    expect(itemOpts).toContain('stock');
    expect(freightOpts).not.toContain('stock');
  });

  it('canConfirm gates a skip line on a reason and a job line on a matched job', async () => {
    const wrapper = mountView();
    await flushPromises();
    // job line: no matched job yet → cannot confirm
    expect(wrapper.vm.canConfirm({ id: 'l1' })).toBe(false);
    // skip with no reason → cannot; with reason → can
    wrapper.vm.draft.l1.disposition = 'skip';
    expect(wrapper.vm.canConfirm({ id: 'l1' })).toBe(false);
    wrapper.vm.draft.l1.skip_reason = 'already on estimate';
    expect(wrapper.vm.canConfirm({ id: 'l1' })).toBe(true);
  });
});
