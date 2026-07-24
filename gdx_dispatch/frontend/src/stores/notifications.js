import { ref, computed } from 'vue';
import { defineStore } from 'pinia';
import { createApiClient } from '../composables/useApi';

export const useNotificationsStore = defineStore('notifications', () => {
  const unreadCount = ref(0);
  const items = ref([]);
  const loading = ref(false);
  const _pollTimer = ref(null);

  const badgeCount = computed(() => unreadCount.value);

  async function fetchCount() {
    try {
      const api = createApiClient();
      const data = await api.get('/api/notifications/count');
      unreadCount.value = typeof data.count === 'number' ? data.count : 0;
    } catch {
      unreadCount.value = 0;
    }
  }

  async function fetchList(pageSize = 20) {
    loading.value = true;
    try {
      const api = createApiClient();
      const data = await api.get(`/api/notifications?page=1&page_size=${pageSize}`);
      items.value = Array.isArray(data?.items) ? data.items : [];
    } catch {
      items.value = [];
    } finally {
      loading.value = false;
    }
  }

  async function markRead(id) {
    try {
      const api = createApiClient();
      await api.post(`/api/notifications/${id}/read`);
      const row = items.value.find((n) => n.id === id);
      if (row) row.is_read = true;
      // Re-fetch the count so the badge drops promptly.
      fetchCount();
    } catch {
      // Best-effort; UI stays optimistic.
    }
  }

  async function remove(id) {
    // Optimistic: drop the row immediately, restore on failure.
    const idx = items.value.findIndex((n) => n.id === id);
    const [row] = idx >= 0 ? items.value.splice(idx, 1) : [null];
    try {
      const api = createApiClient();
      await api.del(`/api/notifications/${id}`);
      fetchCount();
    } catch {
      if (row) items.value.splice(Math.min(idx, items.value.length), 0, row);
      throw new Error('Could not delete notification');
    }
  }

  async function clearAll() {
    const api = createApiClient();
    await api.del('/api/notifications');
    items.value = [];
    unreadCount.value = 0;
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

  return {
    unreadCount, badgeCount, items, loading,
    fetchCount, fetchList, markRead, remove, clearAll, startPolling, stopPolling,
  };
});
