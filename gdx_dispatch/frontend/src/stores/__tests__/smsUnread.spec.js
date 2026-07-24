/**
 * SMS unread store — sidebar badge count (2026-07-24).
 *
 * Errors collapse to 0 (a tenant without phone.com must never badge or
 * toast), and polling starts with an immediate fetch.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const apiMock = { get: vi.fn() };
vi.mock('../../composables/useApi', () => ({
  createApiClient: () => apiMock,
}));

import { useSmsUnreadStore } from '../smsUnread';

describe('smsUnread store', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('fetchCount() reads the unread-count endpoint', async () => {
    const store = useSmsUnreadStore();
    apiMock.get.mockResolvedValueOnce({ count: 3 });
    await store.fetchCount();
    expect(apiMock.get).toHaveBeenCalledWith('/api/phone-com/messages/unread-count');
    expect(store.count).toBe(3);
  });

  it('collapses errors to 0 (module off / auth issues stay silent)', async () => {
    const store = useSmsUnreadStore();
    store.count = 5;
    apiMock.get.mockRejectedValueOnce(new Error('403'));
    await store.fetchCount();
    expect(store.count).toBe(0);
  });

  it('non-numeric payloads count as 0', async () => {
    const store = useSmsUnreadStore();
    apiMock.get.mockResolvedValueOnce({ count: 'nope' });
    await store.fetchCount();
    expect(store.count).toBe(0);
  });

  it('startPolling() fetches immediately and again on the interval', async () => {
    const store = useSmsUnreadStore();
    apiMock.get.mockResolvedValue({ count: 1 });
    store.startPolling(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
    store.stopPolling();
    await vi.advanceTimersByTimeAsync(120000);
    expect(apiMock.get).toHaveBeenCalledTimes(2);
  });
});
