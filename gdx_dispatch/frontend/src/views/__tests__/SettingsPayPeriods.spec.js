/**
 * Settings → Pay periods.
 *
 * Mount tests, not source greps: a regex proving the string "biweekly"
 * appears in the .vue file proves authorship, not that the control renders
 * or that Save sends anything.
 *
 * The failures these are built to catch:
 *   1. Save silently drops a field — the operator sets a recipient, the
 *      toast says saved, and the column never changes.
 *   2. Biweekly saved with no anchor. The server refuses it too, but this
 *      guard is what turns a 422 into a sentence at the field.
 *   3. The preview computing periods in JavaScript. It must render what
 *      GET /api/timeclock/pay-periods returned — a second copy of the
 *      arithmetic here is how this screen ends up showing one fortnight
 *      while the emailed file covers another.
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
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive: vi.fn(async () => true) }),
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
  pay_period_cadence: 'biweekly',
  pay_period_anchor_start: '2026-08-10',
  pay_period_pay_lag_days: 5,
  payroll_recipient_emails: 'bookkeeper@example.com',
  payroll_autosend_enabled: true,
  payroll_autosend_hour: 7,
};

const CALENDAR = {
  configured: true,
  cadence: 'biweekly',
  cadence_label: 'Every two weeks',
  timezone: 'America/Chicago',
  today: '2026-08-26',
  current: { start: '2026-08-24', end: '2026-09-06', label: '2026-08-24 – 2026-09-06', pay_date: '2026-09-11' },
  previous: { start: '2026-08-10', end: '2026-08-23', label: '2026-08-10 – 2026-08-23', pay_date: '2026-08-28' },
  next: { start: '2026-09-07', end: '2026-09-20', label: '2026-09-07 – 2026-09-20', pay_date: '2026-09-25' },
};

const passthrough = { template: '<div><slot /></div>' };

// v-model-capable stubs. The auto-stub (`Select: true`) renders an inert
// element, so a test driving it would prove nothing about the binding.
const selectStub = {
  props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
  emits: ['update:modelValue'],
  template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"></select>',
};
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

const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: true,
  Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
  Select: selectStub, ToggleSwitch: toggleStub, InputNumber: numberStub,
  AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
  PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
  SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
};

async function mountSettings() {
  const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockReset();
  apiPost.mockReset();
  toastAdd.mockReset();
  apiGet.mockImplementation((url) => {
    if (url === '/api/settings') return Promise.resolve({ ...SETTINGS });
    if (url === '/api/timeclock/pay-periods') return Promise.resolve({ ...CALENDAR });
    return Promise.reject(new Error(`unmocked GET ${url}`));
  });
  apiPatch.mockResolvedValue({ ...SETTINGS });
  apiPost.mockResolvedValue({});
});

describe('Settings — Pay periods', () => {
  it('renders the controls an operator needs to configure a payroll calendar', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="pay-period-card"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-cadence"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-lag"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-recipients"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-autosend"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-save"]').exists()).toBe(true);
  });

  it('populates from the server rather than showing blanks', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="pay-period-recipients"]').element.value)
      .toBe('bookkeeper@example.com');
    expect(wrapper.find('[data-testid="pay-period-anchor"]').element.value)
      .toBe('2026-08-10');
  });

  it('shows the periods the SERVER computed, not ones worked out here', async () => {
    const wrapper = await mountSettings();
    const preview = wrapper.find('[data-testid="pay-period-preview"]');
    expect(preview.exists()).toBe(true);
    expect(preview.text()).toContain('2026-08-10 – 2026-08-23');
    expect(preview.text()).toContain('2026-08-28');
  });

  it('sends every field, so Save cannot silently drop one', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="pay-period-recipients"]').setValue('payroll@example.com');
    await wrapper.find('[data-testid="pay-period-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/settings',
      expect.objectContaining({
        pay_period_cadence: 'biweekly',
        pay_period_anchor_start: '2026-08-10',
        pay_period_pay_lag_days: 5,
        payroll_recipient_emails: 'payroll@example.com',
        payroll_autosend_enabled: true,
        payroll_autosend_hour: 7,
      }),
      expect.anything(),
    );
  });

  it('refuses to save a two-week period with no start date to count from', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="pay-period-anchor"]').setValue('');
    await wrapper.find('[data-testid="pay-period-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('hides the anchor for cadences the calendar can derive on its own', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="pay-period-anchor"]').exists()).toBe(true);
    await wrapper.find('[data-testid="pay-period-cadence"]').setValue('weekly_mon');
    await flushPromises();
    expect(wrapper.find('[data-testid="pay-period-anchor"]').exists()).toBe(false);
  });

  it('says so plainly when the calendar is not configured yet', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/settings') return Promise.resolve({ ...SETTINGS, pay_period_anchor_start: '' });
      if (url === '/api/timeclock/pay-periods') {
        return Promise.resolve({
          configured: false,
          message: 'Biweekly pay periods need a start date to count from.',
          current: null, previous: null, next: null,
          cadence: 'biweekly', timezone: 'America/Chicago',
        });
      }
      return Promise.reject(new Error(`unmocked GET ${url}`));
    });
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="pay-period-preview"]').text())
      .toContain('need a start date');
  });

  it('survives the timeclock module being switched off entirely', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/settings') return Promise.resolve({ ...SETTINGS });
      return Promise.reject(new Error('module disabled'));
    });
    const wrapper = await mountSettings();
    // The card still renders and is editable; only the preview is absent.
    expect(wrapper.find('[data-testid="pay-period-card"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="pay-period-preview"]').exists()).toBe(false);
  });
});
