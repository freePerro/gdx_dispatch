/**
 * #656 — a cleared dispatch thread says who cleared it.
 *
 * Read state is shared by the whole office: the first dispatcher to open a
 * thread clears its "N new" badge for everyone. The owner kept that meaning and
 * asked for the row to name the reader, so a badge that disappeared is never
 * mistaken for one nobody acted on. test_chat_threads_seen_by_656.py pins the
 * API fields; this pins what the row renders from them.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const getMock = vi.fn();
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ params: {}, query: {}, path: '/mobile/dispatch' }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-09-13' }),
}));

const stubs = {
  MobileChatDialog: { template: '<div />' },
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Dialog: { template: '<div><slot /></div>' },
  SelectButton: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template:
      '<div><button v-for="o in options" :key="o.value" :data-tab="o.value" ' +
      '@click="$emit(\'update:modelValue\', o.value)">{{ o.label }}</button></div>',
  },
  Select: { template: '<div />' },
  InputText: { template: '<input />' },
  DatePicker: { template: '<div />' },
};

const minutesAgo = (m) => new Date(Date.now() - m * 60_000).toISOString();

async function threadsTab(threads) {
  getMock.mockImplementation(async (url) =>
    String(url).includes('/api/mobile/dispatch/threads') ? { threads } : [],
  );
  const { default: View } = await import('../MobileDispatchView.vue');
  const w = mount(View, { global: { stubs } });
  await flushPromises();
  await w.find('[data-tab="threads"]').trigger('click');
  await flushPromises();
  return w;
}

const row = (overrides) => ({
  job_id: 'job-1',
  job_title: 'Spring replacement',
  customer_name: 'Acme',
  last_message_at: minutesAgo(10),
  unread_count: 0,
  last_read_by_name: null,
  last_read_at: null,
  ...overrides,
});

beforeEach(() => vi.clearAllMocks());

describe('thread row read state', () => {
  it('names who cleared the thread, and when', async () => {
    const w = await threadsTab([row({ last_read_by_name: 'Dana Dispatch', last_read_at: minutesAgo(5) })]);
    const seen = w.find('[data-test="md-thread-seen"]');
    expect(seen.exists()).toBe(true);
    expect(seen.text()).toBe('Seen by Dana Dispatch · 5m ago');
    expect(w.find('.tag').exists()).toBe(false);
  });

  it('an unread thread keeps its badge and names nobody', async () => {
    // An earlier message was read, then the tech wrote again: the new
    // message is what matters, so no "Seen by" beside the badge.
    const w = await threadsTab([
      row({ unread_count: 2, last_read_by_name: 'Dana Dispatch', last_read_at: minutesAgo(30) }),
    ]);
    expect(w.find('.tag').text()).toBe('2 new');
    expect(w.find('[data-test="md-thread-seen"]').exists()).toBe(false);
  });

  it('a thread nobody has read shows neither', async () => {
    const w = await threadsTab([row({ unread_count: 0 })]);
    expect(w.find('[data-test="md-thread-seen"]').exists()).toBe(false);
  });

  it('a reader whose account is gone still shows the time', async () => {
    const w = await threadsTab([row({ last_read_by_name: null, last_read_at: minutesAgo(3) })]);
    expect(w.find('[data-test="md-thread-seen"]').text()).toBe('Seen by someone · 3m ago');
  });
});
