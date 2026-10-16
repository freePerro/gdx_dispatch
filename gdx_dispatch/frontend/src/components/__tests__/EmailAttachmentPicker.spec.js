/**
 * EmailAttachmentPicker — what a sender can pick, and what it hands back.
 *
 * The list comes from GET /api/email-attachments/sources (server-scoped);
 * these pin that the picker asks for the right job/customer, offers several
 * estimates for one customer as separate ticks, falls back to a customer
 * search when there is no context, and emits picked items the send path
 * can turn into attachment_refs.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
vi.mock('../../composables/useApi', () => ({ useApi: () => ({ get: apiGet }) }));
const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

import EmailAttachmentPicker from '../EmailAttachmentPicker.vue';

const stubs = {
  Dialog: { props: ['visible'], template: '<div v-if="visible" data-test="dlg"><slot /><slot name="footer" /></div>' },
  Button: {
    props: ['label', 'disabled', 'loading'],
    emits: ['click'],
    template: '<button :data-test="$attrs[\'data-test\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue', 'input'],
    template: '<input :data-test="$attrs[\'data-test\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value); $emit(\'input\', $event)" />',
    inheritAttrs: false,
  },
};

const SOURCES = {
  job: { id: 'j1', label: 'Job 1001' },
  customer: { id: 'c1', name: 'Pat Doe' },
  estimates: [
    { id: 'e1', number: 'E-1', label: 'Single door', status: 'sent', total: 1200, created_at: '2026-10-01T00:00:00Z' },
    { id: 'e2', number: 'E-2', label: 'Double door', status: 'draft', total: 2400, created_at: '2026-10-02T00:00:00Z' },
  ],
  invoices: [{ id: 'i1', number: 'INV-9', status: 'sent', total: 300 }],
  job_photos: [
    { id: 'p1', name: 'before.jpg', size_bytes: 400_000, customer_visible: true },
    { id: 'p2', name: 'spring.jpg', size_bytes: 300_000, customer_visible: false },
  ],
  job_documents: [{ id: 'd1', name: 'Permit', file_name: 'permit.pdf', size_bytes: 50_000, content_type: 'application/pdf' }],
  customer_documents: [],
};

function mountPicker(props = {}) {
  return mount(EmailAttachmentPicker, {
    props: { modelValue: [], ...props },
    global: { stubs },
  });
}

function lastEmitted(w) {
  const ev = w.emitted('update:modelValue');
  return ev[ev.length - 1][0];
}

describe('EmailAttachmentPicker', () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue(SOURCES);
    toastAdd.mockReset();
  });

  it('loads the job and customer it is given and lists every section that has records', async () => {
    const w = mountPicker({ jobId: 'j1', customerId: 'c1' });
    await w.find('[data-test="eap-records"]').trigger('click');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/email-attachments/sources?job_id=j1&customer_id=c1', { suppressErrorToast: true });
    for (const s of ['estimates', 'invoices', 'photos', 'job-files']) {
      expect(w.find(`[data-test="eap-section-${s}"]`).exists(), s).toBe(true);
    }
    expect(w.find('[data-test="eap-section-customer-files"]').exists()).toBe(false);
    expect(w.text()).toContain('Internal'); // the not-customer-visible photo is labelled
  });

  it('several estimates for one customer come back as separate picks', async () => {
    const w = mountPicker({ customerId: 'c1' });
    await w.find('[data-test="eap-records"]').trigger('click');
    await flushPromises();
    const boxes = w.find('[data-test="eap-section-estimates"]').findAll('input[type="checkbox"]');
    expect(boxes).toHaveLength(2);
    await boxes[0].trigger('change');
    await boxes[1].trigger('change');
    await w.find('[data-test="eap-apply"]').trigger('click');
    const picked = lastEmitted(w);
    expect(picked.map((a) => `${a.kind}:${a.id}`)).toEqual(['estimate_pdf:e1', 'estimate_pdf:e2']);
  });

  it('unticking a record removes it and keeps device files', async () => {
    const upload = { kind: 'upload', name: 'a.jpg', size_bytes: 10, content_base64: 'QQ==' };
    const est = { kind: 'estimate_pdf', id: 'e1', name: 'Estimate E-1' };
    const w = mountPicker({ customerId: 'c1', modelValue: [upload, est] });
    await w.find('[data-test="eap-records"]').trigger('click');
    await flushPromises();
    const box = w.find('[data-test="eap-section-estimates"]').findAll('input[type="checkbox"]')[0];
    expect(box.element.checked).toBe(true);
    await box.trigger('change');
    await w.find('[data-test="eap-apply"]').trigger('click');
    expect(lastEmitted(w)).toEqual([upload]);
  });

  it('with no job or customer it searches customers first, then loads the chosen one', async () => {
    vi.useFakeTimers();
    apiGet.mockImplementation((url) =>
      url.startsWith('/api/customers/search') ? Promise.resolve([{ id: 'c9', name: 'Lee Roe' }]) : Promise.resolve(SOURCES));
    try {
      const w = mountPicker();
      await w.find('[data-test="eap-records"]').trigger('click');
      expect(apiGet).not.toHaveBeenCalled();
      await w.find('[data-test="eap-cust-q"]').setValue('Lee');
      await vi.advanceTimersByTimeAsync(300);
      await flushPromises();
      expect(apiGet).toHaveBeenCalledWith('/api/customers/search?q=Lee', { suppressErrorToast: true });
      await w.find('[data-test="eap-cust-row"]').trigger('click');
      await flushPromises();
      expect(apiGet).toHaveBeenLastCalledWith('/api/email-attachments/sources?customer_id=c9', { suppressErrorToast: true });
      expect(w.find('[data-test="eap-section-estimates"]').exists()).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('says so when there is nothing to attach', async () => {
    apiGet.mockResolvedValue({ estimates: [], invoices: [], job_photos: [], job_documents: [], customer_documents: [] });
    const w = mountPicker({ jobId: 'j1' });
    await w.find('[data-test="eap-records"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-test="eap-empty"]').exists()).toBe(true);
  });

  it('adds a device file as an upload and removes it from its chip', async () => {
    const w = mountPicker();
    const input = w.find('[data-test="eap-file-input"]');
    const file = new File(['hi'], 'note.txt', { type: 'text/plain' });
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true });
    await input.trigger('change');
    // FileReader settles on a macrotask, after any flushPromises.
    await vi.waitFor(() => expect(w.emitted('update:modelValue')).toBeTruthy());
    const added = lastEmitted(w);
    expect(added).toHaveLength(1);
    expect(added[0]).toMatchObject({ kind: 'upload', name: 'note.txt', size_bytes: 2 });

    await w.setProps({ modelValue: added });
    await w.find('[data-test="eap-remove"]').trigger('click');
    expect(lastEmitted(w)).toEqual([]);
  });

  it('warns and adds nothing for a file over the per-file limit', async () => {
    const w = mountPicker();
    const input = w.find('[data-test="eap-file-input"]');
    const big = new File([new Uint8Array(15_000_001)], 'clip.mp4', { type: 'video/mp4' });
    Object.defineProperty(input.element, 'files', { value: [big], configurable: true });
    await input.trigger('change');
    await flushPromises();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'File not attached' }));
    expect(lastEmitted(w)).toEqual([]);
  });

  it('shows the total and flags it over the limit', async () => {
    const w = mountPicker({ modelValue: [{ kind: 'upload', name: 'a', size_bytes: 19_000_000 }] });
    const total = w.find('[data-test="eap-total"]');
    expect(total.classes()).toContain('eap-over');
    expect(total.text()).toContain('Remove a file');
  });
});
