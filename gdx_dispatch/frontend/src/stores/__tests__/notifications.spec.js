/**
 * Notifications store — delete + clear-all (2026-07-24).
 *
 * remove() is optimistic (row drops immediately, restored on failure);
 * clearAll() wipes list + badge. Both hit the new DELETE endpoints.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
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
