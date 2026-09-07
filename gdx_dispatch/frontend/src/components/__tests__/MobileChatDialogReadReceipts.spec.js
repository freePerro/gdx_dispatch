/**
 * 2026-09-07 (#641) — the dispatcher's unread badge has to be able to clear.
 *
 * `GET /api/mobile/dispatch/threads` counts a thread's unread as the tech-sent
 * messages with `read_at IS NULL`, and MobileDispatchView renders that as the
 * "N new" badge and sorts unread-first. The only writer of `read_at` is
 * `POST /api/mobile/chat/{id}/read`, and nothing in the SPA called it — so the
 * first time a tech wrote to a thread, its badge stayed lit forever.
 *
 * Locks:
 *   - the dispatch board (mark-read) stamps every unread TECH message on open
 *   - already-read messages and the viewer's own messages are left alone
 *   - the tech job page (no mark-read) stamps nothing — the endpoint 403s techs
 *   - reopening a thread does not re-stamp what it already stamped
 *   - a failed receipt is silent, retried later, and does not tell the parent
 *   - the dialog tells the parent, so the thread list refreshes its counts
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { flushPromises } from '@vue/test-utils';

const api = { get: vi.fn(), post: vi.fn() };
vi.mock('../../composables/useApi', () => ({ useApi: () => api }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

const Stub = { template: '<div><slot /></div>' };
const stubs = {
  Dialog: { props: ['visible'], template: '<div><slot /><slot name="footer" /></div>' },
  Button: Stub,
  InputText: Stub,
  Tag: Stub,
};

import MobileChatDialog from '../MobileChatDialog.vue';

// Factories, not shared constants: the component stamps `read_at` onto the
// message object it was handed, so a shared fixture would arrive pre-read in
// every test after the first and quietly stop exercising anything.
const techUnread = () => ({ id: 'm1', sender_role: 'tech', body: 'on site', read_at: null, created_at: '2026-09-07T10:00:00Z' });
const techRead = () => ({ id: 'm2', sender_role: 'tech', body: 'done', read_at: '2026-09-07T10:01:00Z', created_at: '2026-09-07T10:01:00Z' });
const ownMessage = () => ({ id: 'm3', sender_role: 'dispatcher', body: 'thanks', read_at: null, created_at: '2026-09-07T10:02:00Z' });

const mounted = [];

async function mountDialog(messages, markRead = true) {
  api.get.mockResolvedValue({ messages, quick_actions: {} });
  // The dialog fetches on the visible false -> true transition, which is what
  // opening a thread does; mounting already-visible fetches nothing.
  const wrapper = mount(MobileChatDialog, {
    props: { visible: false, job: { id: 'job-1', title: 'Job' }, markRead },
    global: { stubs },
  });
  await wrapper.setProps({ visible: true });
  await flushPromises();
  mounted.push(wrapper);
  return wrapper;
}

/** Reopen the dialog, which is how the poll re-enters fetchMessages. */
async function reopen(wrapper) {
  await wrapper.setProps({ visible: false });
  await wrapper.setProps({ visible: true });
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  api.post.mockResolvedValue({ id: 'm1', read_at: '2026-09-07T10:05:00Z' });
});

afterEach(() => {
  // Each dialog starts a 5s poll on open. Leaving instances alive leaks timers
  // across tests and makes this file order-dependent.
  while (mounted.length) mounted.pop().unmount();
});

function readCalls() {
  return api.post.mock.calls.filter(([url]) => /\/api\/mobile\/chat\/.+\/read$/.test(url));
}

describe('MobileChatDialog read receipts', () => {
  it('stamps the unread tech message a dispatcher opens', async () => {
    await mountDialog([techUnread(), techRead(), ownMessage()]);

    expect(readCalls().map(([url]) => url)).toEqual(['/api/mobile/chat/m1/read']);
  });

  it('leaves already-read messages and the viewer\'s own messages alone', async () => {
    await mountDialog([techRead(), ownMessage()]);

    expect(readCalls()).toHaveLength(0);
  });

  it('stamps nothing when the parent did not opt in — the tech job page', async () => {
    await mountDialog([techUnread()], false);

    expect(readCalls()).toHaveLength(0);
  });

  it('does not re-stamp a message it already stamped when reopened', async () => {
    const wrapper = await mountDialog([techUnread()]);
    expect(readCalls()).toHaveLength(1);

    // Reopening refetches. The server now returns the message as read, which is
    // what stops a second receipt — and so does the in-memory guard.
    api.get.mockResolvedValue({
      messages: [{ ...techUnread(), read_at: '2026-09-07T10:05:00Z' }],
      quick_actions: {},
    });
    await reopen(wrapper);

    expect(readCalls()).toHaveLength(1);
  });

  it('retries a failed receipt the next time the thread is opened', async () => {
    api.post.mockRejectedValueOnce(new Error('offline'));
    const wrapper = await mountDialog([techUnread()]);
    expect(readCalls()).toHaveLength(1);

    // Still unread on the server, because the receipt never landed.
    api.post.mockResolvedValue({ id: 'm1', read_at: '2026-09-07T10:06:00Z' });
    await reopen(wrapper);

    expect(readCalls()).toHaveLength(2);
  });

  it('suppresses the error toast on the receipt call', async () => {
    await mountDialog([techUnread()]);

    const [, , options] = readCalls()[0];
    expect(options).toMatchObject({ suppressErrorToast: true });
  });

  it('stamps a message that arrives on the incremental poll branch', async () => {
    const wrapper = await mountDialog([]);
    expect(readCalls()).toHaveLength(0);

    // What the 5s interval does: fetchMessages(false) -> the `?since=` URL,
    // whose results are merged into the existing list rather than replacing it.
    api.get.mockResolvedValue({ messages: [techUnread()], quick_actions: {} });
    await wrapper.vm.fetchMessages(false);
    await flushPromises();

    expect(api.get.mock.calls.at(-1)[0]).toContain('since=');
    expect(readCalls().map(([url]) => url)).toEqual(['/api/mobile/chat/m1/read']);
  });

  it('stamps nothing while the tab is hidden — nobody is looking', async () => {
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    try {
      await mountDialog([techUnread()]);
      expect(readCalls()).toHaveLength(0);
    } finally {
      spy.mockRestore();
    }
  });

  it('does not tell the parent when every receipt failed', async () => {
    api.post.mockRejectedValue(new Error('offline'));
    const wrapper = await mountDialog([techUnread()]);

    expect(readCalls()).toHaveLength(1);
    expect(wrapper.emitted('read')).toBeFalsy();
  });

  it('tells the parent so the thread list can refresh its counts', async () => {
    const wrapper = await mountDialog([techUnread()]);

    expect(wrapper.emitted('read')).toBeTruthy();
  });
});
