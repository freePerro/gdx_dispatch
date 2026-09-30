/**
 * useCashCalendar — the day count flows into the URL, changing it reloads,
 * and saving the two choices sends the shape the settings endpoint expects:
 * an empty floor is an explicit clear, never a silent 0.
 */
import { describe, expect, it, vi } from 'vitest';
import { useCashCalendar, CASH_CALENDAR_DEFAULT_DAYS } from '../useCashCalendar';

function makeApi() {
  return {
    get: vi.fn(async (url) => ({ url })),
    put: vi.fn(async () => ({})),
  };
}

describe('useCashCalendar', () => {
  it('loads the default 14 days', async () => {
    const api = makeApi();
    const cc = useCashCalendar(api);
    await cc.load();
    expect(CASH_CALENDAR_DEFAULT_DAYS).toBe(14);
    expect(api.get).toHaveBeenCalledWith('/api/forecast/cash-calendar?days=14', undefined);
    expect(cc.calendar.value.url).toBe('/api/forecast/cash-calendar?days=14');
  });

  it('a quiet load suppresses the error toast', async () => {
    const api = makeApi();
    await useCashCalendar(api).load({ quiet: true });
    expect(api.get).toHaveBeenCalledWith('/api/forecast/cash-calendar?days=14', { suppressErrorToast: true });
  });

  it('setDays reloads with the new window', async () => {
    const api = makeApi();
    const cc = useCashCalendar(api);
    await cc.setDays(30);
    expect(api.get).toHaveBeenLastCalledWith('/api/forecast/cash-calendar?days=30', undefined);
  });

  it('records a load failure instead of throwing', async () => {
    const api = makeApi();
    api.get.mockRejectedValueOnce(new Error('boom'));
    const cc = useCashCalendar(api);
    await cc.load();
    expect(cc.error.value).toBe('boom');
  });

  it('saves a floor and accounts, then reloads', async () => {
    const api = makeApi();
    const cc = useCashCalendar(api);
    await cc.saveChoices({ floor: 1500, accountIds: ['a1'] });
    expect(api.put).toHaveBeenCalledWith(
      '/api/forecast/settings',
      { operating_account_ids: ['a1'], cash_floor: 1500 },
      { successMessage: 'Cash calendar settings saved' },
    );
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('an empty floor clears it and no accounts means the default', async () => {
    const api = makeApi();
    await useCashCalendar(api).saveChoices({ floor: null, accountIds: [] });
    expect(api.put.mock.calls[0][1]).toEqual({ operating_account_ids: [], clear_cash_floor: true });
  });

  it('a floor of zero is a real floor, not a clear', async () => {
    const api = makeApi();
    await useCashCalendar(api).saveChoices({ floor: 0, accountIds: [] });
    expect(api.put.mock.calls[0][1]).toEqual({ operating_account_ids: [], cash_floor: 0 });
  });
});
