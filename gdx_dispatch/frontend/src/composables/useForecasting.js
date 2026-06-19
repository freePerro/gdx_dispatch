/**
 * useForecasting — pulls revenue projection, QB recurring transactions,
 * and tenant settings for the Forecasting view.
 *
 * Three endpoints (all under /api):
 *   GET  /forecast/revenue?window=30|60|90  → projection envelope
 *   GET  /forecast/settings                 → per-tenant knobs
 *   PUT  /forecast/settings                 → update knobs
 *   GET  /quickbooks/recurring-transactions → cached list
 *   POST /quickbooks/sync/recurring-transactions → trigger pull
 *
 * The view-layer composable owns loading/error state for each panel
 * independently so a QB outage doesn't black out the AR projection.
 */
import { ref } from 'vue';
import { useApi } from './useApi';

export function useForecasting(injectedApi) {
  const api = injectedApi || useApi();

  const window = ref(30);
  const projection = ref(null);
  const projectionLoading = ref(false);
  const projectionError = ref(null);

  const recurring = ref([]);
  const recurringLoading = ref(false);
  const recurringError = ref(null);

  const settings = ref(null);
  const settingsLoading = ref(false);
  const settingsError = ref(null);
  const settingsSaving = ref(false);

  async function loadProjection() {
    projectionLoading.value = true;
    projectionError.value = null;
    try {
      projection.value = await api.get(`/api/forecast/revenue?window=${window.value}`);
    } catch (err) {
      projectionError.value = err?.message || 'Failed to load projection';
    } finally {
      projectionLoading.value = false;
    }
  }

  async function loadRecurring() {
    recurringLoading.value = true;
    recurringError.value = null;
    try {
      const r = await api.get('/api/quickbooks/recurring-transactions');
      recurring.value = r?.items || [];
    } catch (err) {
      recurringError.value = err?.message || 'Failed to load recurring transactions';
    } finally {
      recurringLoading.value = false;
    }
  }

  async function syncRecurring() {
    recurringLoading.value = true;
    recurringError.value = null;
    try {
      await api.post(
        '/api/quickbooks/sync/recurring-transactions',
        {},
        { successMessage: 'Recurring transactions synced from QuickBooks' },
      );
      await loadRecurring();
    } catch (err) {
      recurringError.value = err?.message || 'QuickBooks sync failed';
    } finally {
      recurringLoading.value = false;
    }
  }

  async function loadSettings() {
    settingsLoading.value = true;
    settingsError.value = null;
    try {
      settings.value = await api.get('/api/forecast/settings');
    } catch (err) {
      settingsError.value = err?.message || 'Failed to load settings';
    } finally {
      settingsLoading.value = false;
    }
  }

  async function saveSettings(patch) {
    settingsSaving.value = true;
    settingsError.value = null;
    try {
      settings.value = await api.put(
        '/api/forecast/settings',
        patch,
        { successMessage: 'Forecast settings saved' },
      );
      // Re-fetch projection because the knobs changed.
      await loadProjection();
    } catch (err) {
      settingsError.value = err?.message || 'Failed to save settings';
    } finally {
      settingsSaving.value = false;
    }
  }

  function setWindow(days) {
    window.value = days;
    return loadProjection();
  }

  return {
    window,
    setWindow,
    projection,
    projectionLoading,
    projectionError,
    loadProjection,
    recurring,
    recurringLoading,
    recurringError,
    loadRecurring,
    syncRecurring,
    settings,
    settingsLoading,
    settingsError,
    settingsSaving,
    loadSettings,
    saveSettings,
  };
}
