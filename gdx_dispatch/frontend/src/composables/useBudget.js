/**
 * useBudget — pulls + mutates the monthly budget for one (year, month).
 *
 * Endpoints (all under /api/budgets):
 *   GET    ?year=&month=                           → lines + actuals + variance
 *   GET    /grid?year=                             → 12-month grid
 *   POST   /seed?year=&month=&lookback_months=3    → auto-seed from P&L trailing avg
 *   POST   /classify?lookback_months=6             → fixed/variable proposals (no write)
 *   POST   /refresh-actuals?year=                  → re-pull QBO P&L for the year
 *   POST   /                                       → create one manual line
 *   PATCH  /{id}                                   → edit one line
 *   POST   /{id}/lock     POST /{id}/unlock        → lock/unlock
 *   DELETE /{id}                                   → remove one line
 *
 * Independent load/error state per concern so a slow QBO refresh doesn't
 * blank out an already-rendered table.
 */
import { ref } from 'vue';
import { useApi } from './useApi';

export function useBudget(injectedApi) {
  const api = injectedApi || useApi();

  const now = new Date();
  const year = ref(now.getFullYear());
  const month = ref(now.getMonth() + 1);

  const data = ref(null);          // { lines, available_accounts, totals }
  const loading = ref(false);
  const error = ref(null);

  const seedingBusy = ref(false);
  const refreshingActuals = ref(false);

  const classifyProposals = ref([]);
  const classifyLoading = ref(false);
  const classifyError = ref(null);

  async function load() {
    loading.value = true;
    error.value = null;
    try {
      data.value = await api.get(`/api/budgets?year=${year.value}&month=${month.value}`);
    } catch (err) {
      error.value = err?.message || 'failed to load budget';
      data.value = null;
    } finally {
      loading.value = false;
    }
  }

  async function setMonth(y, m) {
    year.value = y;
    month.value = m;
    await load();
  }

  async function seed(lookbackMonths = 3, overwriteUserEdits = false) {
    seedingBusy.value = true;
    try {
      const qs = `year=${year.value}&month=${month.value}&lookback_months=${lookbackMonths}&overwrite_user_edits=${overwriteUserEdits}`;
      const result = await api.post(`/api/budgets/seed?${qs}`);
      await load();
      return result;
    } finally {
      seedingBusy.value = false;
    }
  }

  async function refreshActuals() {
    refreshingActuals.value = true;
    try {
      const result = await api.post(`/api/budgets/refresh-actuals?year=${year.value}`);
      await load();
      return result;
    } finally {
      refreshingActuals.value = false;
    }
  }

  async function createLine(payload) {
    const created = await api.post('/api/budgets', payload);
    await load();
    return created;
  }

  async function updateLine(id, payload) {
    const updated = await api.patch(`/api/budgets/${id}`, payload);
    await load();
    return updated;
  }

  async function deleteLine(id) {
    // useApi exposes the verb as `del` (because `delete` is reserved
    // in many engines and the project standardizes on `del`).
    await api.del(`/api/budgets/${id}`);
    await load();
  }

  async function lock(id)   { await api.post(`/api/budgets/${id}/lock`);   await load(); }
  async function unlock(id) { await api.post(`/api/budgets/${id}/unlock`); await load(); }

  async function loadClassify(lookbackMonths = 6) {
    classifyLoading.value = true;
    classifyError.value = null;
    try {
      const result = await api.post(`/api/budgets/classify?lookback_months=${lookbackMonths}`);
      classifyProposals.value = result.proposals || [];
    } catch (err) {
      classifyError.value = err?.message || 'failed to classify';
      classifyProposals.value = [];
    } finally {
      classifyLoading.value = false;
    }
  }

  return {
    year, month, data, loading, error,
    seedingBusy, refreshingActuals,
    classifyProposals, classifyLoading, classifyError,
    load, setMonth, seed, refreshActuals,
    createLine, updateLine, deleteLine, lock, unlock,
    loadClassify,
  };
}
