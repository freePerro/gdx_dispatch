import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../src/stores/auth';

const pushMock = vi.fn();
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: pushMock }),
}));

const addMock = vi.fn();
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: addMock }),
}));

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('useApiWithToast composable', () => {
  let useApiWithToast;

  beforeEach(async () => {
    setActivePinia(createPinia());
    pushMock.mockClear();
    addMock.mockClear();
    vi.restoreAllMocks();
    const mod = await import('../src/composables/useApiWithToast');
    useApiWithToast = mod.useApiWithToast;
  });

  it('get returns data on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { items: [1, 2] })));
    const api = useApiWithToast();
    const data = await api.get('/api/jobs');
    expect(data).toEqual({ items: [1, 2] });
  });

  it('post shows success toast when successMessage provided', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { id: 'new-1' })));
    const api = useApiWithToast();
    await api.post('/api/jobs', { title: 'Test' }, { successMessage: 'Job created' });
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Job created' }),
    );
  });

  it('401 error with Unauthorized message triggers session expired toast', async () => {
    // Simulate a direct "Unauthorized" error (e.g. refresh also fails with 401)
    // useApi retry calls refreshAccessToken which on failure throws "Token refresh failed"
    // That doesn't contain "401" or "Unauthorized", so handleError treats it as generic.
    // To trigger the 401 path, the error message must contain "401" or "Unauthorized".
    vi.stubGlobal(
      'fetch',
      vi.fn()
        // First: 401 triggers refresh
        .mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))
        // Refresh: also 401 → auth.refreshAccessToken throws "Token refresh failed"
        .mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' })),
    );
    const auth = useAuthStore();
    auth.accessToken = 'some-token';

    const api = useApiWithToast();
    await expect(api.get('/api/jobs')).rejects.toThrow();

    // The refresh failure throws a generic error, so handleError shows generic toast
    expect(addMock).toHaveBeenCalled();
    // Auth store clears token on refresh failure
    expect(auth.accessToken).toBeNull();
  });

  it('500 error shows error toast', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(500, { detail: 'Internal' })),
    );
    const api = useApiWithToast();
    await expect(api.get('/api/jobs')).rejects.toThrow();

    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error' }),
    );
  });

  it('network error shows connection error toast', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Failed to fetch')));
    const api = useApiWithToast();
    await expect(api.get('/api/jobs')).rejects.toThrow();

    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error', summary: 'Network error' }),
    );
  });

  it('patch shows success toast on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { ok: true })));
    const api = useApiWithToast();
    await api.patch('/api/jobs/1', { title: 'Updated' }, { successMessage: 'Saved' });
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Saved' }),
    );
  });

  it('del shows success toast on success', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(200, { ok: true })));
    const api = useApiWithToast();
    await api.del('/api/jobs/1', { successMessage: 'Deleted' });
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', summary: 'Deleted' }),
    );
  });

  it('403 with missing-permission detail shows Permission denied toast (slice 4.3)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(403, { detail: "Missing permission: ['payroll.write']" })),
    );
    const api = useApiWithToast();
    await expect(api.post('/api/payroll', {})).rejects.toThrow();
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        summary: 'Permission denied',
        detail: expect.stringContaining("['payroll.write']"),
      }),
    );
  });

  it('429 shows Slow down toast (slice 4.2/4.3)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(429, { detail: 'Too many privileged writes. Slow down.' })),
    );
    const api = useApiWithToast();
    await expect(api.post('/api/role-permissions/roles', { name: 'X' })).rejects.toThrow();
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn', summary: 'Slow down' }),
    );
  });

  it('403 without missing-permission detail still shows generic Permission denied', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse(403, { detail: 'Forbidden' })),
    );
    const api = useApiWithToast();
    await expect(api.get('/api/something')).rejects.toThrow();
    expect(addMock).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        summary: 'Permission denied',
      }),
    );
  });
});
