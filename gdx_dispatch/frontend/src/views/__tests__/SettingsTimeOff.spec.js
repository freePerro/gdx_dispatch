/**
 * Settings → Time off and holidays.
 *
 * Mount tests. The failures these catch:
 *   1. Save dropping a field, or sending HOURS where the server wants
 *      MINUTES — a half day saved as 4.0 minutes.
 *   2. The calendar rendering from a stale copy instead of what GET
 *      /api/settings returned.
 *   3. Post enabled on a row that has not been saved (the server 422s a
 *      date that is not on the stored calendar).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';

const apiGet = vi.fn();
const apiPatch = vi.fn();
const apiPost = vi.fn();
const toastAdd = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, patch: apiPatch, post: apiPost, delete: vi.fn(), put: vi.fn() }),
}));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, patch: apiPatch, post: apiPost, delete: vi.fn(), put: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive: vi.fn(async () => true), confirmAsync: vi.fn(async () => true) }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ loadTenantModules: vi.fn() }),
}));
vi.mock('../../composables/useIdleLogout', () => ({
  getIdleTimeoutMin: () => 30,
  setIdleTimeoutMin: vi.fn(),
}));

import SettingsView from '../SettingsView.vue';

const SETTINGS = {
  pay_period_cadence: 'weekly_mon',
  holiday_calendar: [
    { date: '2026-11-26', name: 'Thanksgiving', minutes: 480 },
    { date: '2026-12-25', name: 'Christmas Day', minutes: 240 },
  ],
  time_off_default_minutes: 420,
  time_off_counts_toward_overtime: false,
};

const passthrough = { template: '<div><slot /></div>' };
const numberStub = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<input type="number" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
};
const toggleStub = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
};
const textStub = {
  props: ['modelValue', 'type'],
  emits: ['update:modelValue'],
  template: '<input :type="type || \'text\'" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
};
const buttonStub = {
  props: ['label', 'icon', 'severity', 'loading', 'disabled', 'text', 'outlined', 'size'],
  emits: ['click'],
  template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
};

const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: { props: ['value'], template: '<span class="tag">{{ value }}</span>' },
  Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
  Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select />' },
  InputNumber: numberStub, ToggleSwitch: toggleStub, InputText: textStub, Button: buttonStub,
  OutlookConnectButton: true, MarginTiersPanel: true, HolidayPostDialog: { props: ['visible', 'holiday'], template: '<div class="hpd" />' },
};

function mockGet(settings = SETTINGS, posted = { '2026-11-26': 3 }) {
  apiGet.mockImplementation((url) => {
    if (url === '/api/settings') return Promise.resolve({ ...settings });
    if (url.startsWith('/api/timeclock/time-off/holidays')) {
      return Promise.resolve(settings.holiday_calendar.map((h) => ({ ...h, posted: posted[h.date] || 0 })));
    }
    if (url.startsWith('/api/timeclock/pay-periods')) return Promise.resolve({ configured: false });
    return Promise.resolve({});
  });
}

async function mountSettings() {
  const w = mount(SettingsView, {
    global: { plugins: [createPinia()], stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
});

describe('Settings → Time off and holidays', () => {
  it('renders what GET /api/settings returned, in hours, with posted counts', async () => {
    mockGet();
    const w = await mountSettings();
    expect(w.find('[data-testid="time-off-card"]').exists()).toBe(true);
    expect(w.vm.timeOff.default_hours).toBe(7);
    expect(w.vm.timeOff.counts_toward_overtime).toBe(false);
    expect(w.vm.timeOff.holidays.map((h) => [h.date, h.name, h.hours])).toEqual([
      ['2026-11-26', 'Thanksgiving', 8],
      ['2026-12-25', 'Christmas Day', 4],
    ]);
    expect(w.find('[data-testid="holiday-posted-0"]').text()).toBe('3 people');
    expect(w.find('[data-testid="holiday-row-1"]').text()).toContain('not yet');
    // Post is enabled on saved rows only.
    expect(w.find('[data-testid="holiday-post-0"]').attributes('disabled')).toBeUndefined();
  });

  it('save sends MINUTES and the whole calendar, then re-renders from the response', async () => {
    mockGet();
    apiPatch.mockImplementation((url, body) => Promise.resolve({ ...SETTINGS, ...body }));
    const w = await mountSettings();

    await w.find('[data-testid="holiday-add"]').trigger('click');
    expect(w.find('[data-testid="holiday-post-2"]').attributes('disabled')).toBeDefined();
    await w.find('[data-testid="holiday-date-2"]').setValue('2026-07-03');
    await w.find('[data-testid="holiday-name-2"]').setValue('  Independence Day (observed) ');
    await w.find('[data-testid="holiday-hours-2"]').setValue('8');
    await w.find('[data-testid="time-off-default-hours"]').setValue('8');
    await w.find('[data-testid="time-off-counts-ot"]').setValue(true);

    await w.find('[data-testid="time-off-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledTimes(1);
    const [url, body, opts] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/settings');
    expect(body).toEqual({
      time_off_default_minutes: 480,
      time_off_counts_toward_overtime: true,
      holiday_calendar: [
        { date: '2026-11-26', name: 'Thanksgiving', minutes: 480 },
        { date: '2026-12-25', name: 'Christmas Day', minutes: 240 },
        { date: '2026-07-03', name: 'Independence Day (observed)', minutes: 480 },
      ],
    });
    expect(opts).toEqual(expect.objectContaining({ successMessage: expect.any(String) }));
    // The new row is now saved → Post enabled.
    expect(w.find('[data-testid="holiday-post-2"]').attributes('disabled')).toBeUndefined();
  });

  it('refuses to save a row with no date or name, at the row, without a request', async () => {
    mockGet();
    const w = await mountSettings();
    await w.find('[data-testid="holiday-add"]').trigger('click');
    await w.find('[data-testid="time-off-save"]').trigger('click');
    await flushPromises();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error' }));
  });

  it('remove drops the row; Post opens the shared dialog with minutes', async () => {
    mockGet();
    const w = await mountSettings();
    await w.find('[data-testid="holiday-post-1"]').trigger('click');
    expect(w.vm.holidayPostVisible).toBe(true);
    expect(w.vm.holidayToPost).toEqual({ date: '2026-12-25', name: 'Christmas Day', minutes: 240 });
    await w.find('[data-testid="holiday-remove-0"]').trigger('click');
    expect(w.vm.timeOff.holidays.map((h) => h.date)).toEqual(['2026-12-25']);
  });
});
