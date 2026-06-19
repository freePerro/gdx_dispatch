/**
 * useRecurringStreams — manages /api/forecast/recurring/streams/* CRUD.
 *
 * QBO RecurringTransaction is a *template*; this composable wraps the
 * observed-recurring API which is grounded in what actually clears the
 * bank. Both kinds of recurring data coexist; this composable is the
 * UI layer for the observed/manual side.
 */
import { ref } from 'vue';
import { useApi } from './useApi';

export function useRecurringStreams(injectedApi) {
  const api = injectedApi || useApi();

  const streams = ref([]);
  const loading = ref(false);
  const error = ref(null);
  const selected = ref(null);
  const detailLoading = ref(false);

  async function list(status = null) {
    loading.value = true;
    error.value = null;
    try {
      const qs = status ? `?status=${encodeURIComponent(status)}` : '';
      const r = await api.get(`/api/forecast/recurring/streams${qs}`);
      streams.value = r?.items || [];
    } catch (e) {
      error.value = e?.message || 'Failed to load recurring streams';
    } finally {
      loading.value = false;
    }
  }

  async function get(id) {
    detailLoading.value = true;
    error.value = null;
    try {
      selected.value = await api.get(`/api/forecast/recurring/streams/${id}`);
    } catch (e) {
      error.value = e?.message || 'Failed to load stream';
    } finally {
      detailLoading.value = false;
    }
  }

  async function create(payload) {
    return api.post('/api/forecast/recurring/streams', payload, {
      successMessage: 'Recurring stream created',
    });
  }

  async function createFromTransaction(payload) {
    return api.post('/api/forecast/recurring/streams/from-transaction', payload, {
      successMessage: 'Marked as recurring',
    });
  }

  async function confirm(id) {
    return api.post(`/api/forecast/recurring/streams/${id}/confirm`, {}, {
      successMessage: 'Recurring stream confirmed',
    });
  }

  async function end(id, payload) {
    return api.post(`/api/forecast/recurring/streams/${id}/end`, payload, {
      successMessage: `Marked ${payload.reason.replace('_', ' ')}`,
    });
  }

  async function patch(id, payload) {
    return api.patch(`/api/forecast/recurring/streams/${id}`, payload, {
      successMessage: 'Recurring stream updated',
    });
  }

  async function dismiss(id) {
    // useApi exposes DELETE as `del` not `delete` (delete is a JS reserved word
    // when used as a property accessor pre-ES5 lookup ergonomics carry-over).
    return api.del(`/api/forecast/recurring/streams/${id}`, {
      successMessage: 'Suggestion dismissed',
    });
  }

  async function unlinkHit(streamId, hitId) {
    return api.post(`/api/forecast/recurring/streams/${streamId}/hits/${hitId}/unlink`, {}, {
      successMessage: 'Hit unlinked',
    });
  }

  async function detectNow() {
    return api.post('/api/forecast/recurring/detect', {}, {
      successMessage: 'Detector run complete',
    });
  }

  return {
    streams, loading, error, selected, detailLoading,
    list, get, create, createFromTransaction, confirm, end, patch, dismiss, unlinkHit, detectNow,
  };
}
