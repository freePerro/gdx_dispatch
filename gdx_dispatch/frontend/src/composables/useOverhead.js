import { ref } from 'vue';
import { useApi } from './useApi';

/**
 * Overhead obligations register + forward projection (ADR-016).
 *
 * Outflow-only: this is overhead you must pay, NOT runway. The projection
 * steps down as obligations end (e.g. a loan paying off).
 */
export function useOverhead(injectedApi) {
  const api = injectedApi || useApi();

  const obligations = ref([]);
  const currentMonthlyTotal = ref('0');
  const categories = ref([]);
  const cadences = ref([]);
  const costTypes = ref([]);
  const listLoading = ref(false);
  const listError = ref(null);

  const projection = ref(null);
  const projectionLoading = ref(false);
  const projectionError = ref(null);
  const horizonMonths = ref(12);

  const suggestions = ref([]);
  const suggestionsLoading = ref(false);

  async function loadList() {
    listLoading.value = true;
    listError.value = null;
    try {
      const data = await api.get('/api/overhead');
      obligations.value = data.obligations || [];
      currentMonthlyTotal.value = data.current_monthly_total || '0';
      categories.value = data.categories || [];
      cadences.value = data.cadences || [];
      costTypes.value = data.cost_types || [];
    } catch (err) {
      listError.value = err?.message || 'Failed to load overhead obligations';
    } finally {
      listLoading.value = false;
    }
  }

  async function loadProjection() {
    projectionLoading.value = true;
    projectionError.value = null;
    try {
      projection.value = await api.get(
        `/api/overhead/projection?horizon_months=${horizonMonths.value}`,
      );
    } catch (err) {
      projectionError.value = err?.message || 'Failed to load projection';
    } finally {
      projectionLoading.value = false;
    }
  }

  async function loadSuggestions() {
    suggestionsLoading.value = true;
    try {
      const data = await api.get('/api/overhead/suggestions');
      suggestions.value = data.suggestions || [];
    } catch (_) {
      // Suggestions are a non-critical hint (and depend on the experimental
      // forecasting data); never let them block the page.
      suggestions.value = [];
    } finally {
      suggestionsLoading.value = false;
    }
  }

  async function refreshAll() {
    await Promise.all([loadList(), loadProjection(), loadSuggestions()]);
  }

  async function createObligation(payload) {
    await api.post('/api/overhead', payload, { successMessage: 'Obligation added' });
    await refreshAll();
  }

  async function updateObligation(id, payload) {
    await api.patch(`/api/overhead/${id}`, payload, { successMessage: 'Obligation updated' });
    await refreshAll();
  }

  async function deleteObligation(id) {
    await api.del(`/api/overhead/${id}`, { successMessage: 'Obligation removed' });
    await refreshAll();
  }

  return {
    obligations,
    currentMonthlyTotal,
    categories,
    cadences,
    costTypes,
    listLoading,
    listError,
    projection,
    projectionLoading,
    projectionError,
    horizonMonths,
    suggestions,
    suggestionsLoading,
    loadList,
    loadProjection,
    loadSuggestions,
    refreshAll,
    createObligation,
    updateObligation,
    deleteObligation,
  };
}
