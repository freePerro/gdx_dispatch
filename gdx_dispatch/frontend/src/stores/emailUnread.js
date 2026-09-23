import { ref } from 'vue';
import { defineStore } from 'pinia';
import { createApiClient } from '../composables/useApi';

/**
 * Unread email count — drives the sidebar Inbox badge and the new-mail toast
 * (P2.6).
 *
 * Same shape as the SMS unread store: a 60s client poll against
 * GET /api/outlook/messages/unread-count. The server side is already
 * near-real-time (the Graph webhook enqueues a sync on arrival), so the poll
 * only decides how fast the BADGE notices.
 *
 * The count is visibility-filtered server-side — it's what THIS user can
 * actually open, not every unread row in the mailbox. Errors collapse to 0 so
 * a tenant without the email module never shows a badge.
 *
 * `onIncrease` fires only on a real rise after a known baseline: the very
 * first poll of a session establishes the baseline silently, or every login
 * with unread mail would pop a "new mail" toast for messages from last week.
 */
export const useEmailUnreadStore = defineStore('emailUnread', () => {
  const count = ref(0);
  const _pollTimer = ref(null);
  // Subscriptions (2026-09-22). The sidebar and the mobile bottom nav both
  // poll this store, and on a phone the sidebar lives inside a lazy Drawer:
  // it mounts when the hamburger opens and UNMOUNTS when it closes. A bare
  // stopPolling() from that unmount killed the bottom nav's poll and froze
  // the Email tab badge. One contract across the three polling stores
  // (emailUnread, smsUnread, notifications): startPolling() hands back the
  // release for that one subscription, and the timer runs while any is live.
  // A forgotten release cannot silence anyone else, and a double release is
  // a no-op.
  const _subscribers = new Set();
  const _seeded = ref(false);
  const _listeners = [];

  function onIncrease(fn) {
    _listeners.push(fn);
    return () => {
      const i = _listeners.indexOf(fn);
      if (i >= 0) _listeners.splice(i, 1);
    };
  }

  // In-flight dedup (2026-08-04): the dashboard is now a second concurrent
  // caller (its load fires alongside the sidebar's poll). Two overlapping
  // GETs can resolve out of order — the older response then overwrites the
  // newer count and the baseline/onIncrease logic misfires a "new mail"
  // toast. One request at a time; concurrent callers share it.
  let _inFlight = null;

  function fetchCount() {
    if (_inFlight) return _inFlight;
    _inFlight = _fetchCount().finally(() => {
      _inFlight = null;
    });
    return _inFlight;
  }

  async function _fetchCount() {
    let next = 0;
    try {
      const api = createApiClient();
      const data = await api.get('/api/outlook/messages/unread-count');
      next = typeof data?.count === 'number' ? data.count : 0;
    } catch {
      // Module off, not connected, or a transient blip — leave the last known
      // count and the baseline ALONE. Zeroing here made a 502 on one poll
      // announce "12 new emails" on the next: the count drops to 0, then the
      // recovery poll reads it as a rise. Every network hiccup produced a
      // false new-mail toast, which is the exact thing the baseline exists to
      // prevent. A stale badge for 60s is the cheaper wrong.
      return;
    }
    const prev = count.value;
    count.value = next;
    if (_seeded.value && next > prev) {
      const delta = next - prev;
      _listeners.forEach((fn) => {
        try { fn(delta, next); } catch { /* a bad listener must not kill polling */ }
      });
    }
    _seeded.value = true;
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

  return { count, fetchCount, startPolling, onIncrease };
});
