/**
 * Picture texts (MMS), one photo per text (2026-10-15).
 *
 * The server shrinks and sends (modules/phone_com/mms.py, pinned in
 * tests/test_phone_com_mms.py). What the browser owns, and what is pinned
 * here on both composers — the office page and the tech's phone:
 *  1. each photo is its own POST to /messages/photo, words ride the first;
 *  2. a failure stops the run and keeps the unsent photos for a retry;
 *  3. a photo never follows the user to another customer's thread;
 *  4. Send works with a photo and no words; Later is off while one waits.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { useSmsPhotos } from '../../composables/useSmsPhotos';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, put: vi.fn(), del: vi.fn(), patch: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn().mockResolvedValue(true) }),
}));

const jpeg = (name = 'door.jpg') => new File([new Uint8Array([0xff, 0xd8, 0xff])], name, { type: 'image/jpeg' });

beforeEach(() => {
  setActivePinia(createPinia());
  apiGet.mockReset();
  apiPost.mockReset();
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x');
  globalThis.URL.revokeObjectURL = vi.fn();
  if (!globalThis.matchMedia) {
    globalThis.matchMedia = () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} });
  }
});

// ── the composable ──────────────────────────────────────────────────────
describe('useSmsPhotos', () => {
  it('sends each photo as its own text, the words with the first only', async () => {
    apiPost.mockResolvedValue({ id: 'm' });
    const p = useSmsPhotos({ post: apiPost });
    p.add([jpeg('a.jpg'), jpeg('b.jpg')]);
    const r = await p.sendAll({ to: '+15551112222', body: '  New door  ', customerId: 'c1' });

    expect(r).toEqual({ sent: 2, wordsSent: true, error: null });
    expect(apiPost).toHaveBeenCalledTimes(2);
    const [first, second] = apiPost.mock.calls.map(([url, fd]) => {
      expect(url).toBe('/api/phone-com/messages/photo');
      return fd;
    });
    expect(first.get('body')).toBe('New door');
    expect(first.get('customer_id')).toBe('c1');
    expect(first.get('file').name).toBe('a.jpg');
    expect(second.get('body')).toBeNull();
    expect(second.get('file').name).toBe('b.jpg');
    expect(p.photos.value).toEqual([]);
  });

  it('stops at a failure and keeps the unsent photo for a retry', async () => {
    apiPost.mockResolvedValueOnce({}).mockRejectedValueOnce(new Error('Carrier refused'));
    const p = useSmsPhotos({ post: apiPost });
    p.add([jpeg('a.jpg'), jpeg('b.jpg'), jpeg('c.jpg')]);
    const r = await p.sendAll({ to: '+15551112222', body: 'hi' });

    expect(r).toEqual({ sent: 1, wordsSent: true, error: 'Carrier refused' });
    expect(apiPost).toHaveBeenCalledTimes(2);
    expect(p.photos.value.map((x) => x.file.name)).toEqual(['b.jpg', 'c.jpg']);
  });

  it('sends only the photos that were there when Send was pressed', async () => {
    // Audit 2026-10-15: the loop used to drain the live list, so a photo
    // pasted for customer B mid-upload went to customer A's number.
    let release;
    apiPost.mockImplementationOnce(() => new Promise((r) => { release = r; })).mockResolvedValue({});
    const p = useSmsPhotos({ post: apiPost });
    p.add([jpeg('A1.jpg'), jpeg('A2.jpg')]);
    const run = p.sendAll({ to: '+15550000001' });
    p.clear(); // user opens customer B…
    p.add([jpeg('B1.png'), jpeg('B2.png')]); // …and pastes two screenshots
    release({});
    const r = await run;

    expect(apiPost.mock.calls.map(([, fd]) => fd.get('file').name)).toEqual(['A1.jpg', 'A2.jpg']);
    expect(r).toEqual({ sent: 2, wordsSent: true, error: null });
    expect(p.photos.value.map((x) => x.file.name)).toEqual(['B1.png', 'B2.png']);
  });

  it('keeps the words when the first photo fails', async () => {
    apiPost.mockRejectedValue(new Error('nope'));
    const p = useSmsPhotos({ post: apiPost });
    p.add([jpeg()]);
    expect((await p.sendAll({ to: 'x', body: 'hi' })).wordsSent).toBe(false);
  });

  it('refuses a non-photo, accepts a Windows HEIC with an empty type', () => {
    const p = useSmsPhotos({ post: apiPost });
    p.add([new File(['%PDF'], 'invoice.pdf', { type: 'application/pdf' })]);
    expect(p.photos.value).toEqual([]);
    expect(p.photoError.value).toMatch(/isn't a photo/);
    p.add([new File(['x'], 'IMG_1.HEIC', { type: '' })]);
    expect(p.photos.value).toHaveLength(1);
    // Chrome can't draw HEIC: no preview, so the chip shows the name, not a broken icon.
    expect(p.photos.value[0].preview).toBe('');
    p.add([jpeg()]);
    expect(p.photos.value[1].preview).toBe('blob:x');
  });
});

// ── both composers ──────────────────────────────────────────────────────
const stubs = {
  RouterLink: RouterLinkStub,
  AppLayout: { template: '<div><slot /></div>' },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot name='header' /><slot /><slot name='footer' /></div>" },
  Card: { template: "<div><slot name='title' /><slot name='content' /><slot /></div>" },
  Toolbar: { template: "<div><slot name='start' /><slot name='end' /></div>" },
  Button: {
    props: ['label', 'icon', 'loading', 'severity', 'text', 'size', 'disabled', 'outlined', 'rounded'],
    emits: ['click'],
    template: '<button v-bind="$attrs" :disabled="disabled" @click="$emit(\'click\')">{{ label }}<slot /></button>',
  },
  Tag: { props: ['value', 'severity'], template: '<span>{{ value }}</span>' },
  InputText: { props: ['modelValue'], template: '<input />' },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea v-bind="$attrs" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Select: { props: ['modelValue', 'options'], template: '<div />' },
  DatePicker: { props: ['modelValue'], template: '<div />' },
  ProgressSpinner: { template: '<div />' },
  Paginator: { template: '<div />' },
  Column: { template: '<div />' },
  DataTable: { template: '<div />' },
};

const SURFACES = [
  { name: 'MobileSmsView', load: () => import('../MobileSmsView.vue'), p: 'ms' },
  { name: 'PhoneComMessagesView', load: () => import('../PhoneComMessagesView.vue'), p: 'pc' },
];

describe.each(SURFACES)('$name — photo attach', ({ load, p }) => {
  async function open() {
    const { default: View } = await load();
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/messages/threads?')) {
        return {
          items: [
            { thread_key: 't1', other_party_number: '+15551112222', customer_id: 'c1' },
            { thread_key: 't2', other_party_number: '+15553334444', customer_id: 'c2' },
          ],
        };
      }
      if (url.includes('/api/phone-com/messages/threads/')) return { items: [] };
      return {};
    });
    const w = mount(View, { attachTo: document.body, global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.findAll(`[data-test="${p}-thread-row"]`)[0].trigger('click');
    await flushPromises();
    return w;
  }

  async function pick(w, files) {
    const input = w.find(`[data-test="${p}-compose-photo-input"]`);
    Object.defineProperty(input.element, 'files', { value: files, configurable: true });
    await input.trigger('change');
  }

  const btn = (w, id) => w.find(`[data-test="${p}-compose-${id}"]`);

  it('sends a photo with no words, and keeps Later off while it waits', async () => {
    const w = await open();
    expect(btn(w, 'send').attributes('disabled')).toBeDefined();
    await pick(w, [jpeg()]);
    expect(btn(w, 'send').attributes('disabled')).toBeUndefined();
    await w.find(`[data-test="${p}-compose-body"]`).setValue('Your door');
    expect(btn(w, 'later').attributes('disabled')).toBeDefined();

    apiPost.mockResolvedValue({ id: 'm1' });
    await btn(w, 'send').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, fd] = apiPost.mock.calls[0];
    expect(url).toBe('/api/phone-com/messages/photo');
    expect(fd.get('to')).toBe('+15551112222');
    expect(fd.get('body')).toBe('Your door');
    expect(apiPost.mock.calls.some(([u]) => u === '/api/phone-com/messages')).toBe(false);
    w.unmount();
  });

  it('a send still uploading never writes into the thread opened meanwhile', async () => {
    const w = await open();
    await pick(w, [jpeg('A1.jpg')]);
    await w.find(`[data-test="${p}-compose-body"]`).setValue('For A');
    let release;
    apiPost.mockImplementationOnce(() => new Promise((r) => { release = r; }));
    await btn(w, 'send').trigger('click');
    if (p === 'ms') {
      w.vm.$.setupState.closeThread();
      await flushPromises();
    }
    await w.findAll(`[data-test="${p}-thread-row"]`)[1].trigger('click');
    await flushPromises();
    await w.find(`[data-test="${p}-compose-body"]`).setValue('Draft for B');
    await pick(w, [jpeg('B1.png')]);
    release({ id: 'm1' });
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    expect(apiPost.mock.calls[0][1].get('to')).toBe('+15551112222');
    expect(w.find(`[data-test="${p}-compose-body"]`).element.value).toBe('Draft for B');
    expect(w.findAll('.photo-chip')).toHaveLength(1);
    w.unmount();
  });

  it('reloads the thread after a send that failed, so an unconfirmed one shows', async () => {
    // Audit 2026-10-15 (3rd pass): a timeout says "check the conversation" —
    // the server puts the maybe-sent text there as "unknown"; the view must
    // fetch it, or the user sees nothing and sends again.
    const w = await open();
    await pick(w, [jpeg()]);
    const threadGets = () => apiGet.mock.calls.filter(([u]) => u.startsWith('/api/phone-com/messages/threads/t1')).length;
    const before = threadGets();
    apiPost.mockRejectedValue(new Error('Phone.com didn\'t confirm the send'));
    await btn(w, 'send').trigger('click');
    await flushPromises();
    expect(threadGets()).toBe(before + 1);
    expect(w.findAll('.photo-chip')).toHaveLength(1); // kept: the user decides after looking
    w.unmount();
  });

  it('drops a picked photo when the user opens another customer', async () => {
    const w = await open();
    await pick(w, [jpeg()]);
    expect(w.findAll('.photo-chip')).toHaveLength(1);
    // Mobile shows the conversation over the list; close it to reach row 2.
    if (p === 'ms') {
      w.vm.$.setupState.closeThread();
      await flushPromises();
    }
    await w.findAll(`[data-test="${p}-thread-row"]`)[1].trigger('click');
    await flushPromises();
    expect(w.findAll('.photo-chip')).toHaveLength(0);
    w.unmount();
  });
});

// The photo work rewrote sendReply on the office page and once took the
// neighbouring markThreadRead with it (audit 2026-10-15) — pin the button.
describe('PhoneComMessagesView — Mark read still marks read', () => {
  it('posts mark-read for the open thread', async () => {
    const { default: View } = await import('../PhoneComMessagesView.vue');
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/messages/threads?')) {
        return { items: [{ thread_key: 't1', other_party_number: '+15551112222', unread_count: 2 }] };
      }
      return { items: [] };
    });
    apiPost.mockResolvedValue({});
    const w = mount(View, { global: { stubs, directives: { tooltip: {} } } });
    await flushPromises();
    await w.find('[data-test="pc-thread-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="pc-mark-thread-read"]').trigger('click');
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/phone-com/messages/threads/t1/mark-read');
    w.unmount();
  });
});
