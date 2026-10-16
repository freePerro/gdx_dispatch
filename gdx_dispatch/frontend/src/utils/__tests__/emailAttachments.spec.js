/**
 * utils/emailAttachments — the wire shape and the device-file path.
 *
 * toAttachmentRefs is what the send endpoints parse with extra=forbid, so a
 * stray UI field (size_bytes, internal, detail) on the wire is a 422. The
 * device path must refuse an oversize file with a message a person can act
 * on, and must not rename a file it did not re-encode.
 */
import { describe, expect, it } from 'vitest';
import {
  MAX_UPLOAD_BYTES,
  attachmentKey,
  fmtBytes,
  knownTotalBytes,
  readDeviceFile,
  sendErrorText,
  toAttachmentRefs,
} from '../emailAttachments';

describe('toAttachmentRefs', () => {
  it('sends uploads by content and stored records by id, nothing else', () => {
    const refs = toAttachmentRefs([
      { kind: 'upload', name: 'a.jpg', content_type: 'image/jpeg', content_base64: 'QQ==', size_bytes: 1, original_size_bytes: 9 },
      { kind: 'estimate_pdf', id: 'e1', name: 'Estimate 7', detail: 'Draft · $10', size_bytes: null },
      { kind: 'job_photo', id: 42, name: 'p.jpg', internal: true, size_bytes: 300 },
    ]);
    expect(refs).toEqual([
      { kind: 'upload', name: 'a.jpg', content_type: 'image/jpeg', content_base64: 'QQ==' },
      { kind: 'estimate_pdf', id: 'e1' },
      { kind: 'job_photo', id: '42' },
    ]);
  });

  it('defaults an unknown upload type to octet-stream', () => {
    expect(toAttachmentRefs([{ kind: 'upload', name: 'x', content_base64: 'QQ==' }])[0].content_type)
      .toBe('application/octet-stream');
  });
});

describe('sizes and identity', () => {
  it('totals only what is known; PDFs rendered at send count as zero', () => {
    expect(knownTotalBytes([{ size_bytes: 1000 }, { size_bytes: null }, { size_bytes: '500' }])).toBe(1500);
  });

  it('formats bytes the way the server message does (decimal MB)', () => {
    expect(fmtBytes(999)).toBe('999 B');
    expect(fmtBytes(18_000_000)).toBe('18.0 MB');
    expect(fmtBytes(null)).toBe('');
  });

  it('the same record picked twice is one key; two estimates are two', () => {
    expect(attachmentKey({ kind: 'estimate_pdf', id: 'a' })).toBe(attachmentKey({ kind: 'estimate_pdf', id: 'a', name: 'x' }));
    expect(attachmentKey({ kind: 'estimate_pdf', id: 'a' })).not.toBe(attachmentKey({ kind: 'estimate_pdf', id: 'b' }));
    expect(attachmentKey({ kind: 'document', id: 'a' })).not.toBe(attachmentKey({ kind: 'estimate_pdf', id: 'a' }));
  });
});

describe('readDeviceFile', () => {
  it('reads a small file as-is: name, type and base64 content', async () => {
    const f = new File(['hello'], 'note.txt', { type: 'text/plain' });
    const item = await readDeviceFile(f);
    expect(item).toMatchObject({ kind: 'upload', name: 'note.txt', content_type: 'text/plain', size_bytes: 5 });
    expect(atob(item.content_base64)).toBe('hello');
    expect(item.original_size_bytes).toBeUndefined();
  });

  it('refuses a file over the per-file limit with a message naming it', async () => {
    const big = new File([new Uint8Array(MAX_UPLOAD_BYTES + 1)], 'video.mp4', { type: 'video/mp4' });
    await expect(readDeviceFile(big)).rejects.toThrow(/video\.mp4 is 15\.0 MB; one file can be at most 15\.0 MB/);
  });

  it('keeps a large image unrenamed when it cannot be downscaled here', async () => {
    // jsdom has no createImageBitmap: the downscale step must step aside, not throw.
    const img = new File([new Uint8Array(1_200_000)], 'door.heic', { type: 'image/heic' });
    const item = await readDeviceFile(img);
    expect(item.name).toBe('door.heic');
    expect(item.content_type).toBe('image/heic');
    expect(item.size_bytes).toBe(1_200_000);
  });
});


describe('sendErrorText', () => {
  const MAYBE = 'it may have gone. Check Sent Items before sending again.';

  it("passes the app's own reason through — it already says whether the mail went", () => {
    const err = Object.assign(new Error("Outlook didn't confirm the send — check Sent Items"), { status: 502 });
    expect(sendErrorText(err)).toBe(err.message);
    const refused = Object.assign(new Error('Microsoft Graph rejected send: 400'), { status: 502 });
    expect(sendErrorText(refused)).toBe(refused.message);
  });

  it('a dropped connection (no status) may have sent it', () => {
    expect(sendErrorText(new TypeError('Failed to fetch'))).toBe(`Failed to fetch — ${MAYBE}`);
  });

  it("a proxy's bare 5xx page may have sent it — the app may still be working", () => {
    const err = Object.assign(new Error('Request failed (504)'), { status: 504 });
    expect(sendErrorText(err)).toContain(MAYBE);
  });

  it('a bare 4xx is a refusal, not an unknown outcome', () => {
    const err = Object.assign(new Error('Request failed (413)'), { status: 413 });
    expect(sendErrorText(err, 'Forward failed')).toBe('Request failed (413)');
  });
});
