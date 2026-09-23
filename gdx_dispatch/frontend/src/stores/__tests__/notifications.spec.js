/**
 * Notifications store — delete + clear-all (2026-07-24).
 *
 * remove() is optimistic (row drops immediately, restored on failure);
 * clearAll() wipes list + badge. Both hit the new DELETE endpoints.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const apiMock = {
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
};
vi.mock('../../composables/useApi', () => ({
  createApiClient: () => apiMock,
}));

import { useNotificationsStore } from '../notifications';

describe('notifications store — remove / clearAll', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('remove() deletes the row and refreshes the count', async () => {
    const store = useNotificationsStore();
    store.items = [
      { id: 'n1', title: 'A', is_read: false },
      { id: 'n2', title: 'B', is_read: true },
    ];
    apiMock.del.mockResolvedValueOnce({ status: 'ok' });
    apiMock.get.mockResolvedValueOnce({ count: 0 });

    await store.remove('n1');

    expect(apiMock.del).toHaveBeenCalledWith('/api/notifications/n1');
    expect(store.items.map((n) => n.id)).toEqual(['n2']);
  });

  it('remove() restores the row when the DELETE fails', async () => {
    const store = useNotificationsStore();
    store.items = [{ id: 'n1', title: 'A', is_read: false }];
    apiMock.del.mockRejectedValueOnce(new Error('boom'));

    await expect(store.remove('n1')).rejects.toThrow('Could not delete notification');
    expect(store.items.map((n) => n.id)).toEqual(['n1']);
  });

  it('clearAll() empties the list and zeroes the badge', async () => {
    const store = useNotificationsStore();
    store.items = [{ id: 'n1' }, { id: 'n2' }];
    store.unreadCount = 2;
    apiMock.del.mockResolvedValueOnce({ cleared: 2 });

    await store.clearAll();

    expect(apiMock.del).toHaveBeenCalledWith('/api/notifications');
    expect(store.items).toEqual([]);
    expect(store.unreadCount).toBe(0);
  });

  it('clearAll() propagates failure without wiping the list', async () => {
    const store = useNotificationsStore();
    store.items = [{ id: 'n1' }];
    store.unreadCount = 1;
    apiMock.del.mockRejectedValueOnce(new Error('nope'));

    await expect(store.clearAll()).rejects.toThrow('nope');
    expect(store.items.map((n) => n.id)).toEqual(['n1']);
    expect(store.unreadCount).toBe(1);
  });
});

describe('notifications store — polling contract (2026-09-22)', () => {
  // Same subscriber-counted contract as emailUnread / smsUnread: the timer
  // runs while anyone is subscribed, and one consumer's stop cannot silence
  // another. AppTopbar is the only consumer today (a watch that pairs each
  // start with a stop), so this pins the contract, not a live defect.
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('startPolling fetches now and on the interval; its release clears it', async () => {
    const store = useNotificationsStore();
    apiMock.get.mockResolvedValue({ count: 1 });
    const release = store.startPolling(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
    release();
    await vi.advanceTimersByTimeAsync(120000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
  });

  it('releasing one subscription does not stop the poll for another', async () => {
    const store = useNotificationsStore();
    apiMock.get.mockResolvedValue({ count: 1 });
    const releaseA = store.startPolling(60000);
    await vi.advanceTimersByTimeAsync(0);
    const releaseB = store.startPolling(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
    releaseA();
    await vi.advanceTimersByTimeAsync(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(3);
    releaseB();
    await vi.advanceTimersByTimeAsync(120000);
    expect(apiMock.get).toHaveBeenCalledTimes(3);
  });

  it('logout → login: the second subscription can still be stopped by its own release', async () => {
    // The audit's scenario: AppTopbar subscribed, the shell unmounted on
    // logout, a re-login subscribed again. With a bare counter the module-off
    // stop then under-counted and the poll ran forever. Each subscription now
    // owns its release, and the top bar releases on unmount.
    const store = useNotificationsStore();
    apiMock.get.mockResolvedValue({ count: 1 });
    const first = store.startPolling(60000);
    await vi.advanceTimersByTimeAsync(0);
    first(); // AppTopbar unmounts on logout
    const second = store.startPolling(60000); // re-login
    await vi.advanceTimersByTimeAsync(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(3);
    second(); // admin turns communications off
    await vi.advanceTimersByTimeAsync(180000);
    expect(apiMock.get).toHaveBeenCalledTimes(3);
  });
});
