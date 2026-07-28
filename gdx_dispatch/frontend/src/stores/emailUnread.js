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
  const _seeded = ref(false);
  const _listeners = [];

  function onIncrease(fn) {
    _listeners.push(fn);
    return () => {
      const i = _listeners.indexOf(fn);
      if (i >= 0) _listeners.splice(i, 1);
    };
  }

  async function fetchCount() {
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

  function startPolling(intervalMs = 60000) {
    stopPolling();
    fetchCount();
    _pollTimer.value = setInterval(fetchCount, intervalMs);
  }

  function stopPolling() {
    if (_pollTimer.value) {
      clearInterval(_pollTimer.value);
      _pollTimer.value = null;
    }
  }

  return { count, fetchCount, startPolling, stopPolling, onIncrease };
});
