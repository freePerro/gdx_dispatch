import { ref } from 'vue';
import { defineStore } from 'pinia';
import { createApiClient } from '../composables/useApi';

/**
 * Unread inbound SMS count — drives the sidebar SMS pin badge.
 *
 * Same shape as the notifications store's count polling: 60s client poll
 * against GET /api/phone-com/messages/unread-count (the server itself only
 * ingests new SMS every 10 min via the beat poller, so 60s is plenty).
 * Errors collapse to 0 — a tenant without the phone.com module just never
 * shows a badge.
 */
export const useSmsUnreadStore = defineStore('smsUnread', () => {
  const count = ref(0);
  const _pollTimer = ref(null);
  // Subscriptions — one contract across the three polling stores
  // (emailUnread, smsUnread, notifications), adopted 2026-09-22 when the
  // mobile bottom nav became a second consumer of emailUnread: startPolling()
  // hands back the release for that one subscription, and the timer runs
  // while any is live. Only the sidebar polls this store today.
  const _subscribers = new Set();

  // In-flight dedup — same rationale as the email store: the dashboard's
  // load now runs concurrently with the sidebar poll, and out-of-order
  // responses would let an older count overwrite a newer one.
  let _inFlight = null;

  function fetchCount() {
    if (_inFlight) return _inFlight;
    _inFlight = _fetchCount().finally(() => {
      _inFlight = null;
    });
    return _inFlight;
  }

  async function _fetchCount() {
    try {
      const api = createApiClient();
      const data = await api.get('/api/phone-com/messages/unread-count');
      count.value = typeof data.count === 'number' ? data.count : 0;
    } catch {
      count.value = 0;
    }
  }

  function _clearTimer() {
    if (_pollTimer.value) {
      clearInterval(_pollTimer.value);
      _pollTimer.value = null;
    }
  }

  /**
   * Subscribe to the poll. Returns the release() for THIS subscription. It is
   * idempotent, so a caller may release on every exit path (a watch turning
   * off, unmount) without ever under-counting anyone else. The timer runs
   * while any subscription is live and stops with the last release; every
   * new subscriber gets a fresh count immediately (deduped in-flight). The
   * first subscriber's intervalMs sets the timer — later ones join it.
   */
  function startPolling(intervalMs = 60000) {
    const token = Symbol('poll');
    _subscribers.add(token);
    fetchCount();
    if (!_pollTimer.value) {
      _pollTimer.value = setInterval(fetchCount, intervalMs);
    }
    return () => {
      if (!_subscribers.delete(token)) return;
      if (_subscribers.size === 0) _clearTimer();
    };
  }

  return { count, fetchCount, startPolling };
});
