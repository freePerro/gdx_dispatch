/**
 * useCashCalendar — the next N days of money in and out against the
 * operating accounts' synced balance.
 *
 *   GET /api/forecast/cash-calendar?days=N   → calendar envelope (N 1..90)
 *   PUT /api/forecast/settings               → cash_floor / clear_cash_floor /
 *                                              operating_account_ids
 *
 * Used by the Forecasting page (full list) and the Dashboard summary card.
 */
import { ref } from 'vue';
import { useApi } from './useApi';

export const CASH_CALENDAR_DAY_OPTIONS = [7, 14, 30, 60];
export const CASH_CALENDAR_DEFAULT_DAYS = 14;

export function useCashCalendar(injectedApi) {
  const api = injectedApi || useApi();
  const days = ref(CASH_CALENDAR_DEFAULT_DAYS);
  const calendar = ref(null);
  const loading = ref(false);
  const error = ref(null);
  const saving = ref(false);

  async function load({ quiet = false } = {}) {
    loading.value = true;
    error.value = null;
    try {
      calendar.value = await api.get(
        `/api/forecast/cash-calendar?days=${days.value}`,
        quiet ? { suppressErrorToast: true } : undefined,
      );
    } catch (err) {
      error.value = err?.message || 'Failed to load the cash calendar';
    } finally {
      loading.value = false;
    }
  }

  async function setDays(n) {
    days.value = n;
    await load();
  }

  /**
   * Save the calendar's two choices. `floor` null clears it; an empty
   * `accountIds` list returns to the default account selection.
   */
  async function saveChoices({ floor, accountIds }) {
    saving.value = true;
    try {
      const body = { operating_account_ids: accountIds || [] };
      if (floor === null || floor === undefined || floor === '') body.clear_cash_floor = true;
      else body.cash_floor = Number(floor);
      await api.put('/api/forecast/settings', body, { successMessage: 'Cash calendar settings saved' });
      await load();
    } finally {
      saving.value = false;
    }
  }

  return { days, calendar, loading, error, saving, load, setDays, saveChoices };
}
