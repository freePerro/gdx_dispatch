/**
 * Email unread store — sidebar Inbox badge + new-mail toast (P2.6).
 *
 * The load-bearing rule is the BASELINE: the first poll of a session must not
 * fire "new email", or every login with a full inbox announces week-old mail
 * as new. After that, only a genuine rise notifies.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const apiMock = { get: vi.fn() };
vi.mock('../../composables/useApi', () => ({
  createApiClient: () => apiMock,
}));

import { useEmailUnreadStore } from '../emailUnread';

describe('emailUnread store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads the unread-count endpoint', async () => {
    const store = useEmailUnreadStore();
    apiMock.get.mockResolvedValueOnce({ count: 4, capped: false });
    await store.fetchCount();
    expect(apiMock.get).toHaveBeenCalledWith('/api/outlook/messages/unread-count');
    expect(store.count).toBe(4);
  });

  it('dedupes concurrent fetches — dashboard load + sidebar poll share one request', async () => {
    const store = useEmailUnreadStore();
    let resolveGet;
    apiMock.get.mockReturnValueOnce(new Promise((res) => { resolveGet = res; }));
    const first = store.fetchCount();
    const second = store.fetchCount();
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    resolveGet({ count: 7 });
    await Promise.all([first, second]);
    expect(store.count).toBe(7);
    // The guard releases: a later call fetches again.
    apiMock.get.mockResolvedValueOnce({ count: 8 });
    await store.fetchCount();
    expect(apiMock.get).toHaveBeenCalledTimes(2);
    expect(store.count).toBe(8);
  });

  it('collapses errors to 0 — a tenant without email never badges', async () => {
    const store = useEmailUnreadStore();
    apiMock.get.mockRejectedValueOnce(new Error('403'));
    await store.fetchCount();
    expect(store.count).toBe(0);
  });

  it('does NOT notify on the first poll (it only seeds the baseline)', async () => {
    const store = useEmailUnreadStore();
    const seen = vi.fn();
    store.onIncrease(seen);
    apiMock.get.mockResolvedValueOnce({ count: 12 });
    await store.fetchCount();
    expect(store.count).toBe(12);
    expect(seen).not.toHaveBeenCalled();
  });

  it('notifies with the delta when the count rises after the baseline', async () => {
    const store = useEmailUnreadStore();
    const seen = vi.fn();
    store.onIncrease(seen);
    apiMock.get.mockResolvedValueOnce({ count: 2 });
    await store.fetchCount();
    apiMock.get.mockResolvedValueOnce({ count: 5 });
    await store.fetchCount();
    expect(seen).toHaveBeenCalledWith(3, 5);
  });

  it('does not notify when mail is READ (the count falls)', async () => {
    const store = useEmailUnreadStore();
    const seen = vi.fn();
    store.onIncrease(seen);
    apiMock.get.mockResolvedValueOnce({ count: 5 });
    await store.fetchCount();
    apiMock.get.mockResolvedValueOnce({ count: 1 });
    await store.fetchCount();
    expect(seen).not.toHaveBeenCalled();
    expect(store.count).toBe(1);
  });

  it('unsubscribing stops the notifications', async () => {
    const store = useEmailUnreadStore();
    const seen = vi.fn();
    const off = store.onIncrease(seen);
    apiMock.get.mockResolvedValueOnce({ count: 1 });
    await store.fetchCount();
    off();
    apiMock.get.mockResolvedValueOnce({ count: 9 });
    await store.fetchCount();
    expect(seen).not.toHaveBeenCalled();
  });

  it('startPolling fetches immediately and stopPolling clears the timer', async () => {
    const store = useEmailUnreadStore();
    apiMock.get.mockResolvedValue({ count: 0 });
    store.startPolling(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    // Let the first fetch settle (releases the in-flight dedup guard) the
    // way it always does in real time before a 60s tick can fire.
    await Promise.resolve();
    await Promise.resolve();
    vi.advanceTimersByTime(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
    store.stopPolling();
    vi.advanceTimersByTime(180000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
  });

  it('a throwing listener cannot break polling', async () => {
    const store = useEmailUnreadStore();
    const bad = vi.fn(() => { throw new Error('bad listener'); });
    const good = vi.fn();
    store.onIncrease(bad);
    store.onIncrease(good);
    apiMock.get.mockResolvedValueOnce({ count: 0 });
    await store.fetchCount();
    apiMock.get.mockResolvedValueOnce({ count: 1 });
    await store.fetchCount();
    expect(good).toHaveBeenCalled();
  });
});

describe('emailUnread store — audit round 3', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('a failed poll does NOT zero the badge or fake a new-mail toast on recovery', async () => {
    // The bug: catch set count=0 and kept _seeded=true, so the recovery poll
    // read 0 → 12 as "12 new emails" for week-old mail. Every network blip
    // produced a false toast.
    const store = useEmailUnreadStore();
    const seen = vi.fn();
    store.onIncrease(seen);
    apiMock.get.mockResolvedValueOnce({ count: 12 });
    await store.fetchCount();          // baseline
    apiMock.get.mockRejectedValueOnce(new Error('502'));
    await store.fetchCount();          // blip
    expect(store.count).toBe(12);      // badge holds
    apiMock.get.mockResolvedValueOnce({ count: 12 });
    await store.fetchCount();          // recovery
    expect(seen).not.toHaveBeenCalled();
  });
});
