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

  async function fetchCount() {
    try {
      const api = createApiClient();
      const data = await api.get('/api/phone-com/messages/unread-count');
      count.value = typeof data.count === 'number' ? data.count : 0;
    } catch {
      count.value = 0;
    }
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

  return { count, fetchCount, startPolling, stopPolling };
});
