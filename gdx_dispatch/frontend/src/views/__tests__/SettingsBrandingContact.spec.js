/**
 * Branding → contact details (phone / email / address).
 *
 * Why this file exists: `app_settings.phone` is printed in EVERY outbound
 * email footer (core/email_layout.py), on the public estimate page as a
 * `tel:` link, and in the customer portal. `PATCH /api/settings/branding`
 * has accepted phone/email/address since it was written — but the Branding
 * tab never rendered an input for them, so there was no way for an admin to
 * change the value. GDX prod consequently mailed the seed placeholder
 * "1112223333" to customers for months.
 *
 * These are MOUNT tests on purpose. The sibling CompletionGates.spec.js
 * asserts against the raw .vue text; a regex over source proves only that
 * someone typed a string, not that the control renders or that Save sends
 * it. Both failures below are real user-visible breakage:
 *   1. the field is missing/not populated from the server, or
 *   2. Save drops it from the payload (the silent-write shape).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';

const apiGet = vi.fn();
const apiPatch = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, patch: apiPatch, post: apiPost, delete: vi.fn(), put: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive: vi.fn(async () => true) }),
}));
// useQBSync is left REAL: it is a pure factory returning refs, and a stub
// that forgot `running`/`overallStatus` breaks render before the branding
// panel is ever reached.
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ loadTenantModules: vi.fn() }),
}));
vi.mock('../../composables/useIdleLogout', () => ({
  getIdleTimeoutMin: () => 30,
  setIdleTimeoutMin: vi.fn(),
}));

import SettingsView from '../SettingsView.vue';

const BRANDING = {
  company_name: 'Garage Door Xperts',
  logo_url: '',
  primary_color: '#0057a8',
  accent_color: '#f7b500',
  phone: '(320) 555-0100',
  email: 'office@example.com',
  address: '123 Main St, Anytown, MN 56000',
};

// Pass-through stubs so the branding TabPanel actually renders its slot.
const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: true,
  Divider: true, ProgressSpinner: true, Password: true, Textarea: true,
  Select: true, ToggleSwitch: true, InputNumber: true,
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
  // Only branding resolves; every other loader rejects and is swallowed by
  // the onMounted Promise.allSettled — same as a partially-configured tenant.
  apiGet.mockImplementation((url) =>
    url === '/api/settings/branding'
      ? Promise.resolve({ ...BRANDING })
      : Promise.reject(new Error(`unmocked GET ${url}`)),
  );
  apiPatch.mockResolvedValue({ ...BRANDING });
  apiPost.mockResolvedValue({});
});

describe('Branding tab — company contact details', () => {
  it('renders an input for phone, email and address', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="company-phone"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="company-email"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="company-address"]').exists()).toBe(true);
  });

  it('populates the phone from the server rather than showing it blank', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="company-phone"]').element.value).toBe('(320) 555-0100');
    expect(wrapper.find('[data-testid="company-email"]').element.value).toBe('office@example.com');
    expect(wrapper.find('[data-testid="company-address"]').element.value).toBe('123 Main St, Anytown, MN 56000');
  });

  it('sends an edited phone number in the PATCH payload', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="company-phone"]').setValue('(320) 766-9933');
    await wrapper.find('[data-testid="save-branding"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/settings/branding',
      expect.objectContaining({ phone: '(320) 766-9933' }),
    );
  });

  it('sends email and address too, so Save cannot silently drop them', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="company-email"]').setValue('help@example.com');
    await wrapper.find('[data-testid="company-address"]').setValue('9 New Rd, Parkers Prairie, MN');
    await wrapper.find('[data-testid="save-branding"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/settings/branding',
      expect.objectContaining({
        email: 'help@example.com',
        address: '9 New Rd, Parkers Prairie, MN',
      }),
    );
  });

  it('a cleared phone stays cleared instead of repopulating from the server', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="company-phone"]').setValue('');
    await wrapper.find('[data-testid="save-branding"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/settings/branding',
      expect.objectContaining({ phone: '' }),
    );
  });
});
