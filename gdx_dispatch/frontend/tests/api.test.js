import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { useAuthStore } from '../src/stores/auth';
import { createApiClient } from '../src/composables/useApi';

const jsonResponse = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('useApi composable', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.restoreAllMocks();
  });

  it('adds Authorization header when access token exists', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    const auth = useAuthStore();
    auth.accessToken = 'token-abc';
    const api = createApiClient();

    await api.request('/api/jobs');

    const options = fetchMock.mock.calls[0][1];
    expect(options.headers.Authorization).toBe('Bearer token-abc');
  });

  it('retries request after 401 by refreshing token', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))
      .mockResolvedValueOnce(jsonResponse(200, { access_token: 'token-new' }))
      .mockResolvedValueOnce(jsonResponse(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);

    const auth = useAuthStore();
    auth.accessToken = 'token-old';
    const api = createApiClient();

    const data = await api.request('/api/jobs');

    expect(data.ok).toBe(true);
    expect(auth.accessToken).toBe('token-new');
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/auth/refresh', expect.any(Object));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('throws network error transparently', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network offline')));
    const api = createApiClient();

    await expect(api.request('/api/jobs')).rejects.toThrow('Network offline');
  });

  it('returns null on 204 No Content (regression: delete-folder dialog)', async () => {
    // The delete-folder endpoint returns 204. Calling response.json() on an
    // empty body throws SyntaxError; without the 204 short-circuit the catch
    // in DocumentsView would fire even though the server-side delete worked,
    // showing a misleading "Delete failed" toast and leaving the dialog open.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 204,
      headers: { get: () => '0' },
      json: async () => { throw new SyntaxError('Unexpected end of JSON input'); },
    });
    vi.stubGlobal('fetch', fetchMock);

    const api = createApiClient();
    const data = await api.del('/api/document-folders/abc');
    expect(data).toBeNull();
  });
});
