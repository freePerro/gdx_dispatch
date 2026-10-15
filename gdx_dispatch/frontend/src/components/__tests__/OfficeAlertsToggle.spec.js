/**
 * "Alerts on this phone": office users in the Android app only; turning it on
 * mints and starts, then asks for unrestricted battery; turning it off revokes.
 * Mounts the real component with the native wrapper mocked.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const lib = vi.hoisted(() => ({
  isOfficeAlertsAvailable: vi.fn(),
  getOfficeAlertsStatus: vi.fn(),
  enableOfficeAlerts: vi.fn(),
  disableOfficeAlerts: vi.fn(),
  openBatterySettings: vi.fn(),
}));
vi.mock('../../lib/officeAlerts', () => lib);

const perms = vi.hoisted(() => ({ set: new Set() }));
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: (k) => perms.set.has(k) }),
}));
const api = { post: vi.fn() };
vi.mock('../../composables/useApi', () => ({ useApi: () => api }));

import OfficeAlertsToggle from '../OfficeAlertsToggle.vue';

const stubs = {
  ToggleSwitch: {
    props: ['modelValue', 'disabled'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" data-testid="office-alerts-switch" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
};
const mk = (props = {}) => mount(OfficeAlertsToggle, { props, global: { stubs } });

describe('OfficeAlertsToggle', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    perms.set = new Set(['nav.office']);
    lib.isOfficeAlertsAvailable.mockReturnValue(true);
    lib.getOfficeAlertsStatus.mockResolvedValue({ enabled: false, batteryUnrestricted: false });
  });

  it('renders nothing for a tech', () => {
    perms.set = new Set();
    expect(mk().find('[data-testid="office-alerts"]').exists()).toBe(false);
  });

  it('renders nothing outside the Android app', () => {
    lib.isOfficeAlertsAvailable.mockReturnValue(false);
    expect(mk().find('[data-testid="office-alerts"]').exists()).toBe(false);
  });

  it('turning on enables, then asks for unrestricted battery', async () => {
    lib.enableOfficeAlerts.mockResolvedValue({ enabled: true, batteryUnrestricted: false });
    lib.openBatterySettings.mockResolvedValue({ enabled: true, batteryUnrestricted: true });
    const w = mk();
    await flushPromises();
    await w.find('[data-testid="office-alerts-switch"]').setValue(true);
    await flushPromises();
    expect(lib.enableOfficeAlerts).toHaveBeenCalledWith(api);
    expect(lib.openBatterySettings).toHaveBeenCalled();
    expect(w.find('[data-testid="office-alerts-battery"]').exists()).toBe(false);
  });

  it('shows the battery nudge while alerts are on but restricted', async () => {
    lib.getOfficeAlertsStatus.mockResolvedValue({ enabled: true, batteryUnrestricted: false });
    const w = mk();
    await flushPromises();
    expect(w.find('[data-testid="office-alerts-battery"]').exists()).toBe(true);
    lib.openBatterySettings.mockResolvedValue({ enabled: true, batteryUnrestricted: true });
    await w.find('[data-testid="office-alerts-battery-btn"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="office-alerts-battery"]').exists()).toBe(false);
  });

  it('turning off revokes', async () => {
    lib.getOfficeAlertsStatus.mockResolvedValue({ enabled: true, batteryUnrestricted: true });
    lib.disableOfficeAlerts.mockResolvedValue({ enabled: false });
    const w = mk();
    await flushPromises();
    await w.find('[data-testid="office-alerts-switch"]').setValue(false);
    await flushPromises();
    expect(lib.disableOfficeAlerts).toHaveBeenCalledWith('disabled');
  });

  it('a failed enable shows why and leaves the switch off', async () => {
    lib.enableOfficeAlerts.mockRejectedValue(new Error('Phone alerts are for office users'));
    const w = mk();
    await flushPromises();
    await w.find('[data-testid="office-alerts-switch"]').setValue(true);
    await flushPromises();
    expect(w.find('[data-testid="office-alerts-error"]').text()).toContain('office users');
    expect(w.find('[data-testid="office-alerts-switch"]').element.checked).toBe(false);
  });

  it('says what the phone will get: office alerts, or planner reminders in GDX Planner', async () => {
    const office = mk();
    await flushPromises();
    expect(office.text()).toContain('Get new texts, payments and leads on this phone');
    const planner = mk({ planner: true });
    await flushPromises();
    expect(planner.text()).toContain('Get planner reminders on this phone');
    expect(planner.text()).not.toContain('texts, payments');
  });
});
