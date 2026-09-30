/**
 * Billing terms → "Refuse debit cards on the pay page" (2026-09-30).
 *
 * MOUNT tests, like SettingsBrandingContact.spec.js: the toggle must render,
 * show the server's value, and Save must send it. A toggle that renders but
 * is dropped from the PATCH is the silent-write shape — the office would
 * believe debit is refused while the pay page keeps taking it.
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

const TERMS = {
  default_payment_terms_days: 30,
  late_fee_grace_days: 0,
  interest_grace_days: 0,
  card_surcharge_percent: '0.0290',
  refuse_debit_cards: false,
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
  Select: true, InputNumber: true,
  ToggleSwitch: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />' },
  AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
  PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
  SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
};

async function mountSettings() {
  const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
  await flushPromises();
  return wrapper;
}

function toggle(wrapper) {
  return wrapper.find('[data-testid="refuse-debit-cards-toggle"]');
}

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockReset();
  apiPost.mockReset();
  apiPatch.mockImplementation((_url, body) => Promise.resolve({ ...TERMS, ...body }));
  apiPost.mockResolvedValue({});
});

function serve(terms) {
  apiGet.mockImplementation((url) =>
    url === '/api/billing/terms'
      ? Promise.resolve({ ...terms })
      : Promise.reject(new Error(`unmocked GET ${url}`)),
  );
}

describe('Billing terms — refuse debit cards', () => {
  it('renders the toggle, showing the server value', async () => {
    serve({ ...TERMS, refuse_debit_cards: true });
    const wrapper = await mountSettings();
    expect(toggle(wrapper).exists()).toBe(true);
    expect(toggle(wrapper).element.checked).toBe(true);
  });

  it('sends the toggle in the PATCH payload when turned on', async () => {
    serve(TERMS);
    const wrapper = await mountSettings();
    expect(toggle(wrapper).element.checked).toBe(false);
    await toggle(wrapper).setValue(true);
    await wrapper.find('[data-testid="billing-terms-save"]').trigger('click');
    await flushPromises();
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/billing/terms',
      expect.objectContaining({ refuse_debit_cards: true, card_surcharge_percent: expect.any(Number) }),
      expect.anything(),
    );
  });

  it('sends false when left off, rather than dropping the field', async () => {
    serve(TERMS);
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="billing-terms-save"]').trigger('click');
    await flushPromises();
    expect(apiPatch.mock.calls[0][1].refuse_debit_cards).toBe(false);
  });
});
