/**
 * #657 — a chat push notification opens the thread it is about.
 *
 * Both sides used to be sent to /mobile?job=<id>, a query nothing reads, so
 * the tap landed on the generic Today screen. The backend now sends:
 *   - the dispatcher → /mobile/dispatch?job=<id>   (thread opened WITH mark-read,
 *     so following the notification is what clears the unread badge)
 *   - the tech       → /mobile/jobs/<id>?chat=1     (the job page's chat)
 * test_notification_links_resolve_657.py pins those URLs; this pins that each
 * view actually acts on them.
 *
 * Each view consumes its key through router.replace, leaving any other query
 * params alone; ChatPushDeepLinksBack657.spec.js proves with the real router
 * that Back does not bring the key (and the dialog) back.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { ref } from 'vue';

const route = { params: {}, query: {}, path: '/' };
const getMock = vi.fn();
const routerReplace = vi.fn();

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), back: vi.fn(), replace: routerReplace }),
  useRoute: () => route,
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-09-13' }),
}));
vi.mock('../../composables/usePartsSeenCutoff', () => ({
  markJobSeen: vi.fn(),
  countUnseenForJob: () => 0,
}));
vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
  }),
}));

const chatStub = {
  // Typed like the real dialog: a bare `mark-read` attribute is only `true`
  // for a Boolean prop — untyped, it arrives as an empty string.
  props: { visible: Boolean, job: { type: Object, default: null }, markRead: { type: Boolean, default: false } },
  template:
    '<div v-if="visible" data-testid="stub-chat" :data-job="job && job.id" ' +
    ':data-title="job && job.title" :data-mark-read="String(markRead)" />',
};
const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  MobileChatDialog: chatStub,
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: passthrough,
  SelectButton: { props: ['modelValue'], template: '<div />' },
  Select: { template: '<div />' },
  InputText: { template: '<input />' },
  DatePicker: { template: '<div />' },
  MobileJobCloseoutDialog: { template: '<div />' },
  MobileInvoiceDialog: { template: '<div />' },
  MobileQuoteBuilderDialog: { template: '<div />' },
  MobileCustomerQuoteDialog: { template: '<div />' },
  MobileChangeOrderDialog: { template: '<div />' },
};

beforeEach(() => {
  vi.clearAllMocks();
  routerReplace.mockResolvedValue(undefined);
});

// ── Dispatcher: /mobile/dispatch?job=<id> ──────────────────────────────────

async function mountDispatch({ query = {}, threads = [] } = {}) {
  route.path = '/mobile/dispatch';
  route.params = {};
  route.query = query;
  getMock.mockImplementation(async (url) => {
    if (String(url).includes('/api/mobile/dispatch/threads')) return { threads };
    return [];
  });
  const { default: View } = await import('../MobileDispatchView.vue');
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

describe('dispatcher push link', () => {
  it('opens that job\'s thread, with mark-read, titled from the thread list', async () => {
    const w = await mountDispatch({
      query: { job: 'job-9' },
      threads: [
        { job_id: 'job-1', job_title: 'Other', unread_count: 0 },
        { job_id: 'job-9', job_title: 'Spring replacement', unread_count: 2 },
      ],
    });
    const chat = w.find('[data-testid="stub-chat"]');
    expect(chat.exists()).toBe(true);
    expect(chat.attributes('data-job')).toBe('job-9');
    expect(chat.attributes('data-title')).toBe('Spring replacement');
    // mark-read is the whole point: reading from the notification clears the badge.
    expect(chat.attributes('data-mark-read')).toBe('true');
  });

  it('consumes ?job= through the router and keeps any other query param', async () => {
    await mountDispatch({ query: { job: 'job-9', date: '2026-09-13' } });
    expect(routerReplace).toHaveBeenCalledWith({ query: { date: '2026-09-13' } });
  });

  it('fetches the thread list once, not once for the tab switch and again for the link', async () => {
    await mountDispatch({ query: { job: 'job-9' } });
    const threadCalls = getMock.mock.calls.filter((c) => String(c[0]).includes('/dispatch/threads'));
    expect(threadCalls).toHaveLength(1);
  });

  it('still opens a thread older than the 7-day list, just untitled', async () => {
    const w = await mountDispatch({ query: { job: 'job-old' }, threads: [] });
    const chat = w.find('[data-testid="stub-chat"]');
    expect(chat.attributes('data-job')).toBe('job-old');
  });

  it('opens nothing without the query', async () => {
    const w = await mountDispatch();
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(false);
    expect(routerReplace).not.toHaveBeenCalled();
  });
});

// ── Tech: /mobile/jobs/<id>?chat=1 ─────────────────────────────────────────

async function mountJob({ query = {}, readOnly = false } = {}) {
  route.path = '/mobile/jobs/job-123';
  route.params = { id: 'job-123' };
  route.query = query;
  getMock.mockImplementation(async () => ({
    job: {
      id: 'job-123',
      title: 'Spring replacement',
      dispatch_status: 'assigned',
      customer: { id: 'c1', name: 'Acme', phone: '5551234567', address: '123 Main St' },
    },
    notes: [],
    photos: [],
    read_only: readOnly,
  }));
  const { default: View } = await import('../MobileJobDetailView.vue');
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  return w;
}

describe('tech push link', () => {
  it('opens the job chat once the job has loaded', async () => {
    const w = await mountJob({ query: { chat: '1' } });
    const chat = w.find('[data-testid="stub-chat"]');
    expect(chat.exists()).toBe(true);
    expect(chat.attributes('data-job')).toBe('job-123');
    expect(routerReplace).toHaveBeenCalledWith({ query: {} });
  });

  it('does not open chat on a read-only job, where the Chat button is hidden too', async () => {
    const w = await mountJob({ query: { chat: '1' }, readOnly: true });
    expect(w.find('[data-testid="mjd-chat"]').exists()).toBe(false);
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(false);
  });

  it('opens nothing without the query', async () => {
    const w = await mountJob();
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(false);
    expect(routerReplace).not.toHaveBeenCalled();
  });
});
