/**
 * Settings → Feature Settings links to the tech-mobile settings page, and no
 * /admin/* route is left reachable only by URL (GDXA-853).
 *
 * `/admin/feature-settings/tech-mobile` is the only UI for the per-tenant
 * tech-mobile feature settings, and until GDXA-853 nothing in the app linked
 * to it: office staff could not find it. The same shape had already been
 * repaired one route at a time (`/admin/games`, `/admin/audit-log`,
 * `/feedback` — see the comments in constants/modules.js), each time with no
 * guard, so the next one went unnoticed.
 *
 * Two halves:
 *
 *   1. A MOUNT test that presses the real link with the real route table and
 *      checks where it lands — that it resolves to the named route, not
 *      not-found, and that the card follows the route's settings.write gate.
 *   2. A class guard over every `/admin/*` route: some non-router, non-test
 *      source file must name its path or its route name. It is a text check
 *      on purpose, and only an absence detector — it cannot prove a link is
 *      visible, only catch a route nothing mentions at all.
 *
 * Red-test check: removing the card from SettingsView fails both halves
 * (the link is gone, and the tech-mobile route is reported as orphaned).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

import { routes } from '../../router/index.js';

const apiGet = vi.fn();
const granted = new Set();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet, patch: vi.fn(), post: vi.fn(), delete: vi.fn(), put: vi.fn(),
  }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmAsync: vi.fn(async () => true) }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ loadTenantModules: vi.fn() }),
}));
vi.mock('../../composables/useIdleLogout', () => ({
  getIdleTimeoutMin: () => 30,
  setIdleTimeoutMin: vi.fn(),
}));
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({
    hasPermission: (key) => granted.has(key),
    permissionsLoaded: true,
    permissions: granted,
    loadPermissions: vi.fn(async () => granted),
    accessToken: 'test-token',
  }),
}));

import SettingsView from '../SettingsView.vue';

const TARGET = '/admin/feature-settings/tech-mobile';

const passthrough = { template: '<div><slot /></div>' };
// Every panel renders under the stubs, so the panel's `value` is exposed to
// pin which tab the card lives on — a passthrough would stay green if the card
// moved to Branding.
const TabPanel = { props: ['value'], template: '<section :data-panel="value"><slot /></section>' };
const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel,
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
  const router = createRouter({ history: createMemoryHistory(), routes });
  await router.push('/settings');
  await router.isReady();
  const wrapper = mount(SettingsView, {
    global: { stubs, plugins: [router, createPinia()] },
  });
  await flushPromises();
  return { wrapper, router };
}

beforeEach(() => {
  granted.clear();
  apiGet.mockReset();
  apiGet.mockImplementation((url) => Promise.reject(new Error(`unmocked GET ${url}`)));
});

describe('Settings links to the tech-mobile settings page', () => {
  it('a settings.write user clicks through and lands on the tech-mobile route', async () => {
    granted.add('settings.read');
    granted.add('settings.write');
    const { wrapper, router } = await mountSettings();

    // On the Feature Settings tab (value "estimates"), not merely somewhere.
    const panel = wrapper.find('[data-panel="estimates"]');
    expect(panel.find('[data-testid="tech-mobile-settings-card"]').exists()).toBe(true);
    const link = panel.find('[data-testid="tech-mobile-settings-link"]');
    expect(link.exists()).toBe(true);
    expect(link.attributes('href')).toBe(TARGET);

    await link.trigger('click');
    await vi.waitFor(() => {
      if (router.currentRoute.value.path !== TARGET) throw new Error('not landed yet');
    }, { timeout: 2000 });

    expect(router.currentRoute.value.name).toBe('admin-feature-settings-tech-mobile');
    expect(router.currentRoute.value.name).not.toBe('not-found');
    wrapper.unmount();
  });

  it('the card follows the route gate: hidden without settings.write', async () => {
    // The route guard sends a settings.read-only user to access-denied; a
    // link they cannot follow is a dead end, so they must not see it.
    const target = routes.find((r) => r.path === TARGET);
    expect(target?.meta?.requiresPermission).toBe('settings.write');

    granted.add('settings.read');
    const { wrapper } = await mountSettings();
    expect(wrapper.find('[data-testid="tech-mobile-settings-card"]').exists()).toBe(false);
    wrapper.unmount();
  });
});

// ── Class guard: every /admin/* route has an in-app reference ────────────────

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === '__tests__') continue;
      out.push(...sourceFiles(full));
    } else if (/\.(vue|js|ts)$/.test(name) && !/\.spec\.[jt]s$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

function flatten(list, prefix = '') {
  return list.flatMap((r) => {
    const path = r.path.startsWith('/') ? r.path : `${prefix.replace(/\/$/, '')}/${r.path}`;
    return [{ ...r, path }, ...flatten(r.children || [], path)];
  });
}

describe('no /admin/* route is reachable only by URL', () => {
  it('every /admin/* route is named by path or route name outside the router', () => {
    const corpus = sourceFiles(SRC)
      .filter((f) => relative(SRC, f).split(sep).join('/') !== 'router/index.js')
      .map((f) => readFileSync(f, 'utf8'))
      .join('\n');

    const admin = flatten(routes).filter((r) => r.path.startsWith('/admin/') && !r.redirect);
    // Guard against the filter silently matching nothing.
    expect(admin.length).toBeGreaterThanOrEqual(7);

    const quoted = (s) => ['\'', '"', '`'].some((q) => corpus.includes(`${q}${s}${q}`));
    const orphans = admin
      .filter((r) => !quoted(r.path) && !(r.name && quoted(r.name)))
      .map((r) => r.path);
    expect(orphans).toEqual([]);
  });
});
