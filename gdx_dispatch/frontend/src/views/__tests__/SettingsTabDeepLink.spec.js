/**
 * Settings deep link — `/settings?tab=<x>` selects that tab, and only a tab
 * that exists.
 *
 * Why this file exists: Bank Feeds' SimpleFIN "Re-link in Settings" button is a
 * client-side push to `/settings?tab=integrations` (GDXA-18). Its whole value
 * is landing on the Integrations tab, where SimpleFINCard lives — a link that
 * merely resolves is not a repair. Two ways that silently fails:
 *
 *   1. the query is ignored and the user gets Branding — the #657 class;
 *   2. the query is applied WITHOUT validation. PrimeVue Tabs activates on an
 *      exact value match, so an unknown tab selects no panel at all and the
 *      settings body renders blank. A stale bookmark or an old emailed link
 *      lands nowhere, with no error.
 *
 * These are MOUNT tests on purpose, for the reason the sibling
 * SettingsBrandingContact.spec.js gives: a regex over source proves only that
 * someone typed a string. An earlier draft of this guard asserted
 * `SETTINGS_TABS.includes(_qtab)` was PRESENT in the file — an audit broke the
 * behaviour while leaving the text intact and that guard stayed green.
 *
 * It also pins the ordering the button depends on: SettingsView reads
 * `window.location.search` once during setup(), so the history entry must
 * already be written when the component is created.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const apiGet = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet, patch: vi.fn(), post: vi.fn(), delete: vi.fn(), put: vi.fn(),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
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

async function mountAt(search) {
  window.history.replaceState({}, '', `/settings${search}`);
  const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  apiGet.mockReset();
  // Everything rejects; onMounted swallows it via Promise.allSettled, same as a
  // partially-configured tenant. Tab selection happens in setup() regardless.
  apiGet.mockImplementation((url) => Promise.reject(new Error(`unmocked GET ${url}`)));
});

afterEach(() => {
  window.history.replaceState({}, '', '/');
});

describe('Settings tab deep link', () => {
  it('opens Branding when no tab is given', async () => {
    const wrapper = await mountAt('');
    expect(wrapper.vm.activeTab).toBe('branding');
  });

  it('opens Integrations for the SimpleFIN re-link destination', async () => {
    const wrapper = await mountAt('?tab=integrations');
    expect(wrapper.vm.activeTab).toBe('integrations');
  });

  // Drift guard, done behaviourally: every tab the TabList renders must be
  // reachable by deep link. A tab added to the template but not to the
  // allowlist would silently ignore a perfectly valid link. The source is read
  // only to ENUMERATE the cases; each one is then actually mounted and
  // asserted, so the check cannot pass on the strength of text alone.
  it('honours a deep link to every tab it renders', async () => {
    const source = readFileSync(
      join(dirname(fileURLToPath(import.meta.url)), '..', 'SettingsView.vue'),
      'utf8',
    );
    const rendered = [...source.matchAll(/<Tab value="([^"]+)"/g)].map((m) => m[1]);
    expect(rendered.length).toBeGreaterThan(5);
    expect(rendered).toContain('integrations');

    const ignored = [];
    for (const tab of rendered) {
      const wrapper = await mountAt(`?tab=${tab}`);
      if (wrapper.vm.activeTab !== tab) ignored.push(tab);
      wrapper.unmount();
    }
    expect(ignored).toEqual([]);

    // The other direction, which mounting cannot see: an allowlist entry whose
    // <Tab> has been deleted still passes the check above (nothing iterates it)
    // while `?tab=<removed>` selects no panel — the blank body this allowlist
    // exists to prevent. So the two sets must match exactly.
    const listed = source.match(/const SETTINGS_TABS = \[([\s\S]*?)\]/);
    expect(listed).not.toBeNull();
    const allowed = [...listed[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    expect([...allowed].sort()).toEqual([...rendered].sort());
  });

  it('ignores a tab that does not exist rather than blanking the page', async () => {
    const wrapper = await mountAt('?tab=no_such_tab_exists');
    expect(wrapper.vm.activeTab).toBe('branding');
  });

  it('ignores an empty tab value', async () => {
    const wrapper = await mountAt('?tab=');
    expect(wrapper.vm.activeTab).toBe('branding');
  });
});
