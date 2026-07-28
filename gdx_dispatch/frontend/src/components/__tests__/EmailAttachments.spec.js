import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));

import EmailAttachments from '../EmailAttachments.vue';

function mountAtt(props = {}) {
  return mount(EmailAttachments, { props: { messageId: 'm1', ...props } });
}

describe('EmailAttachments', () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it('lists non-inline attachments as download chips', async () => {
    apiGet.mockResolvedValue({
      fetched: true,
      attachments: [
        { id: 'a1', name: 'quote.pdf', content_type: 'application/pdf', size: 2048, is_inline: false },
        { id: 'a2', name: 'logo.png', content_type: 'image/png', size: 500, is_inline: true },
      ],
    });
    const w = mountAtt();
    await flushPromises();
    const chips = w.findAll('[data-test="att-chip"]');
    expect(chips).toHaveLength(1); // inline hidden
    expect(chips[0].text()).toContain('quote.pdf');
    expect(chips[0].text()).toContain('2 KB');
  });

  it('does not fetch when the message has no attachments', async () => {
    mountAtt({ hasAttachments: false });
    await flushPromises();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it('shows a note when the list could not be fetched', async () => {
    apiGet.mockResolvedValue({ fetched: false, reason: 'reconnect_required' });
    const w = mountAtt();
    await flushPromises();
    expect(w.find('[data-test="att-note"]').text()).toContain('Reconnect');
  });

  it('downloads via authed fetch → blob → anchor click', async () => {
    apiGet.mockResolvedValue({
      fetched: true,
      attachments: [{ id: 'a1', name: 'quote.pdf', size: 4, is_inline: false }],
    });
    const blob = new Blob([new Uint8Array([1, 2, 3])]);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(blob) });
    vi.stubGlobal('fetch', fetchMock);
    const createURL = vi.fn(() => 'blob:x');
    const revokeURL = vi.fn();
    vi.stubGlobal('URL', { createObjectURL: createURL, revokeObjectURL: revokeURL });
    const clickSpy = vi.fn();
    const origCreate = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag) => {
      const el = origCreate(tag);
      if (tag === 'a') el.click = clickSpy;
      return el;
    });
    sessionStorage.setItem('gdx_access_token', 'tok123');

    const w = mountAtt();
    await flushPromises();
    await w.find('[data-test="att-chip"]').trigger('click');
    await flushPromises();

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/outlook/messages/m1/attachments/a1',
      { headers: { Authorization: 'Bearer tok123' } },
    );
    expect(createURL).toHaveBeenCalledWith(blob);
    expect(clickSpy).toHaveBeenCalled();
    expect(revokeURL).toHaveBeenCalled();

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
});

describe('EmailAttachments — save to job (P2.3)', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    apiGet.mockResolvedValue({
      fetched: true,
      attachments: [{ id: 'a1', name: 'po.pdf', content_type: 'application/pdf', size: 2048, is_inline: false }],
    });
  });

  it('offers no save action when the message is not linked to a job', async () => {
    const w = mountAtt();
    await flushPromises();
    expect(w.find('[data-test="att-save-to-job"]').exists()).toBe(false);
  });

  it('posts save-to-job with the linked job id', async () => {
    apiPost.mockResolvedValue({ document_id: 'd1', filename: 'po.pdf', size_bytes: 2048, already_saved: false });
    const w = mountAtt({ linkedJobId: 'j-1', linkedJobLabel: 'JOB-2026-014' });
    await flushPromises();
    await w.find('[data-test="att-save-to-job"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toContain('/api/outlook/messages/m1/attachments/a1/save-to-job');
    expect(body).toEqual({ job_id: 'j-1' });
  });

  it('marks the attachment saved and blocks a second identical save', async () => {
    apiPost.mockResolvedValue({ document_id: 'd1', filename: 'po.pdf', size_bytes: 2048, already_saved: false });
    const w = mountAtt({ linkedJobId: 'j-1' });
    await flushPromises();
    const btn = w.find('[data-test="att-save-to-job"]');
    await btn.trigger('click');
    await flushPromises();
    expect(w.find('[data-test="att-save-to-job"]').text()).toContain('Saved to job');
    await w.find('[data-test="att-save-to-job"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it('keeps the button usable after a failure so the user can retry', async () => {
    apiPost.mockRejectedValue(new Error('boom'));
    const w = mountAtt({ linkedJobId: 'j-1' });
    await flushPromises();
    await w.find('[data-test="att-save-to-job"]').trigger('click');
    await flushPromises();
    const btn = w.find('[data-test="att-save-to-job"]');
    expect(btn.text()).toContain('Save to job');
    expect(btn.attributes('disabled')).toBeUndefined();
  });
});
