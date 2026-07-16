import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() }),
}));

const pushSpy = vi.fn();
vi.mock('vue-router', () => ({ useRouter: () => ({ push: pushSpy }) }));

import VendorBillsView from '../VendorBillsView.vue';

const ROWS = [
  { id: 'v1', vendor_name_raw: 'Midwest Wholesale Doors', invoice_number: '90000001',
    po_reference: 'Smith Job', due_date: '2026-07-30', total: '6125.21', status: 'open',
    reviewed_at: null, notes: null, possible_duplicate_of_id: null },
  { id: 'v2', vendor_name_raw: 'Midwest Wholesale Doors', invoice_number: '90000002',
    po_reference: null, due_date: null, total: '100.00', status: 'open',
    reviewed_at: '2026-07-16T00:00:00Z', notes: null, possible_duplicate_of_id: 'v9' },
];

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  SelectButton: { props: ['modelValue'], template: '<div />' },
  FileUpload: { template: '<div />' },
  ProgressSpinner: { template: '<div />' },
  DataTable: { props: ['value'], template: '<table><tbody><tr v-for="(r,i) in value" :key="i"><slot name="body" :data="r" /></tr></tbody></table>' },
  Column: { template: '<td><slot name="body" :data="$attrs.data" /></td>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
};

describe('VendorBillsView', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    pushSpy.mockReset();
    apiGet.mockResolvedValue(ROWS);
  });

  it('loads the vendor-bill queue on mount', async () => {
    mount(VendorBillsView, { global: { stubs } });
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/vendor-invoices');
  });

  it('needsReview aligns with the filter (unreviewed); mathMismatch is separate', async () => {
    const wrapper = mount(VendorBillsView, { global: { stubs } });
    await flushPromises();
    // needs review == not yet reviewed (matches backend ?needs_review=true)
    expect(wrapper.vm.needsReview({ reviewed_at: null })).toBe(true);
    expect(wrapper.vm.needsReview({ reviewed_at: 'x' })).toBe(false);
    // math mismatch is independent — a reviewed bill can still flag it
    expect(wrapper.vm.mathMismatch({ notes: 'INVARIANT_MISMATCH: ...' })).toBe(true);
    expect(wrapper.vm.mathMismatch({ notes: null })).toBe(false);
  });

  it('a duplicate upload shows the notice and does NOT navigate away', async () => {
    apiPost.mockResolvedValue({ created: false, duplicate_reason: 'content_hash', invoice: { id: 'v1' } });
    const wrapper = mount(VendorBillsView, { global: { stubs } });
    await flushPromises();
    await wrapper.vm.onUpload({ files: [new File(['x'], 'b.pdf', { type: 'application/pdf' })] });
    expect(pushSpy).not.toHaveBeenCalled();
    expect(wrapper.vm.notice).toContain('Already imported');
  });

  it('filter selection re-fetches with the matching query', async () => {
    const wrapper = mount(VendorBillsView, { global: { stubs } });
    await flushPromises();
    wrapper.vm.statusFilter = 'needs_review';
    await wrapper.vm.fetchItems();
    expect(apiGet).toHaveBeenCalledWith('/api/vendor-invoices?needs_review=true');
    wrapper.vm.statusFilter = 'paid';
    await wrapper.vm.fetchItems();
    expect(apiGet).toHaveBeenCalledWith('/api/vendor-invoices?status=paid');
  });

  it('upload posts multipart to the upload endpoint and navigates', async () => {
    apiPost.mockResolvedValue({ created: true, invoice: { id: 'v1' } });
    const wrapper = mount(VendorBillsView, { global: { stubs } });
    await flushPromises();
    await wrapper.vm.onUpload({ files: [new File(['x'], 'bill.pdf', { type: 'application/pdf' })] });
    expect(apiPost).toHaveBeenCalled();
    expect(apiPost.mock.calls[0][0]).toBe('/api/vendor-invoices/upload');
    expect(apiPost.mock.calls[0][1]).toBeInstanceOf(FormData);
    expect(pushSpy).toHaveBeenCalledWith('/vendor-bills/v1');
  });
});
