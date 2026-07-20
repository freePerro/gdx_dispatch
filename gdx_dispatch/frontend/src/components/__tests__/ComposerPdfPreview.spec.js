/**
 * ComposerPdfPreview — inline preview of the composer's auto-attached PDF
 * (2026-07-20). Shared by the invoice + estimate email composers.
 *
 * Pinned:
 *  1. No pdf prop → renders nothing at all.
 *  2. With a pdf, builds a blob URL from the base64 (the SAME bytes that get
 *     sent) and renders it in an iframe, plus an "Open full size" anchor.
 *  3. Blob URL lifecycle: revoked on unmount and when the pdf changes.
 *  4. Browsers without createObjectURL degrade to a "still attached" note —
 *     never a crash, never a missing attachment.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import ComposerPdfPreview from '../ComposerPdfPreview.vue';

const PDF_BYTES = '%PDF-1.4 test-bytes';
const PDF = {
  name: 'INV-0001.pdf',
  content_type: 'application/pdf',
  content_base64: btoa(PDF_BYTES),
  size_bytes: PDF_BYTES.length,
};

const ORIG_CREATE = URL.createObjectURL;
const ORIG_REVOKE = URL.revokeObjectURL;
let createObjectURL;
let revokeObjectURL;
let urlSeq;

beforeEach(() => {
  urlSeq = 0;
  createObjectURL = vi.fn(() => `blob:mock-${++urlSeq}`);
  revokeObjectURL = vi.fn();
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
});

afterEach(() => {
  URL.createObjectURL = ORIG_CREATE;
  URL.revokeObjectURL = ORIG_REVOKE;
});

describe('ComposerPdfPreview', () => {
  it('renders nothing without a pdf', () => {
    const wrapper = mount(ComposerPdfPreview);
    expect(wrapper.find('[data-testid="composer-pdf-preview"]').exists()).toBe(false);
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it('renders an iframe preview + open-full-size link from the attachment bytes', () => {
    const wrapper = mount(ComposerPdfPreview, { props: { pdf: PDF } });

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blobArg = createObjectURL.mock.calls[0][0];
    expect(blobArg).toBeInstanceOf(Blob);
    expect(blobArg.type).toBe('application/pdf');
    expect(blobArg.size).toBe(PDF_BYTES.length); // byte-for-byte the attachment, not a re-fetch

    const frame = wrapper.get('[data-testid="composer-pdf-frame"]');
    expect(frame.attributes('src')).toBe('blob:mock-1#toolbar=0&navpanes=0&view=FitH');
    expect(frame.attributes('title')).toBe('INV-0001.pdf');

    const open = wrapper.get('[data-testid="composer-pdf-open"]');
    expect(open.attributes('href')).toBe('blob:mock-1');
    expect(open.attributes('target')).toBe('_blank');
  });

  it('revokes the blob URL on unmount', () => {
    const wrapper = mount(ComposerPdfPreview, { props: { pdf: PDF } });
    wrapper.unmount();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-1');
  });

  it('revokes the old URL and builds a new one when the pdf changes', async () => {
    const wrapper = mount(ComposerPdfPreview, { props: { pdf: PDF } });
    await wrapper.setProps({ pdf: { ...PDF, content_base64: btoa('%PDF-1.4 other') } });

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-1');
    expect(createObjectURL).toHaveBeenCalledTimes(2);
    expect(wrapper.get('[data-testid="composer-pdf-frame"]').attributes('src')).toContain('blob:mock-2');
  });

  it('degrades to a "still attached" note when createObjectURL is unavailable', () => {
    URL.createObjectURL = undefined;
    const wrapper = mount(ComposerPdfPreview, { props: { pdf: PDF } });

    expect(wrapper.find('[data-testid="composer-pdf-frame"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="composer-pdf-preview"]').text()).toMatch(/still attached/i);
  });

  it('survives malformed base64 without rendering a broken frame', () => {
    const wrapper = mount(ComposerPdfPreview, {
      props: { pdf: { ...PDF, content_base64: '!!!not-base64!!!' } },
    });
    expect(wrapper.find('[data-testid="composer-pdf-frame"]').exists()).toBe(false);
    expect(wrapper.get('[data-testid="composer-pdf-preview"]').text()).toMatch(/still attached/i);
  });
});
