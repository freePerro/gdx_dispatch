/**
 * Uploaded bytes never render as a page of the app's origin (GDXA-972).
 *
 * Two holes, one shape. Download opened the file as a blob tab, and a blob tab
 * is a document of the app's own origin, so an uploaded text/html ran with the
 * session in reach. The preview put the blob in an <iframe> whenever the
 * FILENAME ended in .pdf, so "invoice.pdf" stored as text/html rendered as a
 * page too. Pinned here: download saves the file under its own name and never
 * opens a tab; the frame and the image tag follow the type of the bytes the
 * server sent, not the filename or the stored metadata.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';
import Tooltip from 'primevue/tooltip';

const apiGet = vi.fn();
const toastAdd = vi.fn();
const createAuthedBlobUrl = vi.fn();
const downloadAuthedFile = vi.fn();
const openAuthedFile = vi.fn();

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ isAdmin: true, accessToken: 't', user: { role: 'admin' }, hasPermission: () => true }),
}));
vi.mock('../../composables/useAuthedFile', () => ({
  createAuthedBlobUrl: (...a) => createAuthedBlobUrl(...a),
  downloadAuthedFile: (...a) => downloadAuthedFile(...a),
  openAuthedFile: (...a) => openAuthedFile(...a),
}));

import DocumentsView from '../DocumentsView.vue';

function doc(id, original_name, content_type) {
  // The stored name keeps the upload's extension (`f"{uuid4()}{ext.lower()}"`),
  // and it is the name the old preview read the extension from.
  const ext = original_name.slice(original_name.lastIndexOf('.'));
  return { id, original_name, filename: `${id}${ext}`, content_type, file_size: 10 };
}

const DOCS = [
  doc('d-html-pdf', 'invoice.pdf', 'text/html'),
  doc('d-pdf', 'real.pdf', 'application/pdf'),
  doc('d-svg', 'logo.svg', 'image/svg+xml'),
  doc('d-png', 'door.png', 'image/png'),
];

// What the server sends for each (core/file_serving.py): html and svg come
// back as application/octet-stream; inert types keep their own.
const SERVED = {
  'd-html-pdf': { type: 'application/octet-stream', body: '<script>alert(1)</script>' },
  'd-pdf': { type: 'application/pdf', body: '%PDF-1.4' },
  'd-svg': { type: 'application/octet-stream', body: '<svg onload="alert(1)"/>' },
  'd-png': { type: 'image/png', body: 'png' },
};

let windowOpen;
let fetchSpy;
beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  apiGet.mockImplementation((url) => Promise.resolve(url.startsWith('/api/documents') ? DOCS : []));
  createAuthedBlobUrl.mockImplementation(async (url) => `blob:${url.split('/')[3]}`);
  downloadAuthedFile.mockResolvedValue(undefined);
  windowOpen = vi.spyOn(window, 'open').mockImplementation(() => null);
  fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    const s = SERVED[String(url).replace('blob:', '')];
    // jsdom's Blob has no .text(); the view reads only these three.
    return { blob: async () => ({ type: s.type, size: s.body.length, text: async () => s.body }) };
  });
});
afterEach(() => {
  windowOpen.mockRestore();
  fetchSpy.mockRestore();
  document.body.innerHTML = '';
});

async function mountView() {
  const w = mount(DocumentsView, {
    global: { plugins: [PrimeVue], directives: { tooltip: Tooltip } },
    attachTo: document.body,
  });
  await flushPromises();
  return w;
}

async function preview(w, id) {
  await w.find(`[data-testid="doc-preview-${id}"]`).trigger('click');
  await flushPromises();
  return document.body.querySelector('[data-testid="doc-preview-content"]');
}

describe('DocumentsView — uploaded bytes (GDXA-972)', () => {
  it('download saves the file under its own name and never opens a tab', async () => {
    const w = await mountView();
    await w.find('[data-testid="doc-download-btn-d-html-pdf"]').trigger('click');
    await flushPromises();
    expect(downloadAuthedFile).toHaveBeenCalledWith('/api/documents/d-html-pdf/download', 'invoice.pdf');
    expect(openAuthedFile).not.toHaveBeenCalled();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('a .pdf whose bytes are not a pdf is never framed', async () => {
    const w = await mountView();
    const content = await preview(w, 'd-html-pdf');
    expect(content.querySelector('iframe')).toBeNull();
    // Stored as text/html, so it is shown as source text, escaped.
    const pre = content.querySelector('[data-testid="doc-preview-text"]');
    expect(pre.textContent).toBe('<script>alert(1)</script>');
    expect(content.querySelector('script')).toBeNull();
  });

  it('a real pdf is framed', async () => {
    const w = await mountView();
    const content = await preview(w, 'd-pdf');
    const frame = content.querySelector('iframe');
    expect(frame).not.toBeNull();
    expect(frame.getAttribute('src')).toBe('blob:d-pdf');
  });

  it('an svg is shown as text, not put in an image tag', async () => {
    const w = await mountView();
    const content = await preview(w, 'd-svg');
    expect(content.querySelector('img')).toBeNull();
    expect(content.querySelector('iframe')).toBeNull();
    expect(content.querySelector('[data-testid="doc-preview-text"]').textContent).toBe('<svg onload="alert(1)"/>');
  });

  it('a raster image is shown in an image tag', async () => {
    const w = await mountView();
    const content = await preview(w, 'd-png');
    expect(content.querySelector('img')?.getAttribute('src')).toBe('blob:d-png');
  });
});
