/**
 * The Inbox entry gates on the `email` module — through the REAL composable.
 *
 * Found by the 2026-09-22 /audit of the mobile Email tab: the tab hid itself
 * behind `isEnabled('inbox')`, and its unit test passed only because the test
 * mocked `isEnabled`. There is no `inbox` module. The backend registry key is
 * `email` (core/modules.py; every Outlook route is `require_module("email")`),
 * and the composable answers `true` for any key the tenant has never named —
 * so a guard on `inbox` could never fire. The catalog entry now carries
 * `requires: 'email'`, which is what the sidebar pin, the More-drawer row and
 * the bottom-nav tab all inherit. This spec drives the real composable with a
 * mocked /api/settings/modules payload, so it can fail for the real defect.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const { apiGet } = vi.hoisted(() => ({ apiGet: vi.fn() }));

vi.mock('../useApi', () => ({
  useApi: () => ({ get: apiGet }),
  createApiClient: () => ({ get: apiGet }),
}));

// eslint-disable-next-line import/first
import { useTenantModules } from '../useTenantModules';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';

function modulesPayload(entries) {
  apiGet.mockImplementation(async (url) => (url === '/api/settings/modules' ? { modules: entries } : []));
}

describe('useTenantModules — the Inbox entry follows the email module', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    // The loader skips the fetch pre-auth; a token is enough to be "signed in".
    useAuthStore().accessToken = 'test.access.token';
    apiGet.mockReset();
  });

  it('email switched off: isEnabled("email") is false and Inbox leaves the catalog', async () => {
    modulesPayload([{ key: 'email', enabled: false }]);
    const tm = useTenantModules();
    await tm.loadTenantModules({ force: true });
    expect(tm.isEnabled('email')).toBe(false);
    expect(tm.allEnabledModules.value.some((m) => m.key === 'inbox')).toBe(false);
  });

  it('email on, or never stated: Inbox is in the catalog', async () => {
    modulesPayload([{ key: 'email', enabled: true }]);
    const on = useTenantModules();
    await on.loadTenantModules({ force: true });
    expect(on.allEnabledModules.value.some((m) => m.key === 'inbox')).toBe(true);

    modulesPayload([]);
    const unstated = useTenantModules();
    await unstated.loadTenantModules({ force: true });
    expect(unstated.allEnabledModules.value.some((m) => m.key === 'inbox')).toBe(true);
  });

  it('pins the trap: an unknown key reads as enabled, so "inbox" can never gate anything', async () => {
    modulesPayload([{ key: 'email', enabled: false }]);
    const tm = useTenantModules();
    await tm.loadTenantModules({ force: true });
    expect(tm.isEnabled('inbox')).toBe(true);
  });
});
