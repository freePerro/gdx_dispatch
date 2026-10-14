/**
 * `loaded` says the module lists are the tenant's, not the optimistic full
 * catalog the app starts with. The phone's launcher shortcuts wait on it, so
 * a cold start does not shrink them (/audit 2026-10-14).
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

describe('useTenantModules — loaded', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    apiGet.mockReset();
  });

  it('is false until the modules answer, then true — also when the fetch fails', async () => {
    useAuthStore().accessToken = 'test.access.token';
    let answer;
    apiGet.mockImplementation((url) => (url === '/api/settings/modules'
      ? new Promise((r) => { answer = r; })
      : Promise.resolve([])));
    const tm = useTenantModules();
    const loading = tm.loadTenantModules({ force: true });
    await vi.waitFor(() => expect(answer).toBeTypeOf('function'));
    expect(tm.loaded.value).toBe(false);
    answer({ modules: [] });
    await loading;
    expect(tm.loaded.value).toBe(true);

    apiGet.mockImplementation((url) => (url === '/api/settings/modules'
      ? Promise.reject(new Error('down'))
      : Promise.resolve([])));
    await tm.loadTenantModules({ force: true });
    expect(tm.loaded.value).toBe(true);
  });

  it('waits for the plugin list too, since a plugin can be a favorite', async () => {
    const tm = useTenantModules();
    await tm.loadTenantModules({ force: true }); // signed out: resets the shared flag
    expect(tm.loaded.value).toBe(false);
    useAuthStore().accessToken = 'test.access.token';
    let plugins;
    apiGet.mockImplementation((url) => (url === '/api/plugins'
      ? new Promise((r) => { plugins = r; })
      : Promise.resolve({ modules: [] })));
    const loading = tm.loadTenantModules({ force: true });
    await vi.waitFor(() => expect(plugins).toBeTypeOf('function'));
    expect(tm.loaded.value).toBe(false);
    plugins([]);
    await loading;
    expect(tm.loaded.value).toBe(true);
  });

  it('signed out: back to false', async () => {
    useAuthStore().accessToken = 'test.access.token';
    apiGet.mockResolvedValue({ modules: [] });
    const tm = useTenantModules();
    await tm.loadTenantModules({ force: true });
    expect(tm.loaded.value).toBe(true);

    useAuthStore().accessToken = null;
    await tm.loadTenantModules({ force: true });
    expect(tm.loaded.value).toBe(false);
  });
});
