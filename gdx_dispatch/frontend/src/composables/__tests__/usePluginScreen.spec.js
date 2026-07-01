import { describe, expect, it, vi } from 'vitest';
import { usePluginScreen } from '../usePluginScreen';

const MANIFEST = {
  screens: [
    {
      type: 'list',
      title: 'Example Items',
      endpoint: '/api/plugins/example/items',
      columns: [
        { field: 'id', label: 'ID' },
        { field: 'name', label: 'Name' },
      ],
      create: {
        endpoint: '/api/plugins/example/items',
        fields: [{ name: 'name', label: 'Name', type: 'text', required: true }],
      },
    },
  ],
};

function fakeApi(items) {
  return {
    get: vi.fn(async (url) => (url.endsWith('/ui') ? MANIFEST : items)),
    post: vi.fn(async () => ({})),
  };
}

describe('usePluginScreen', () => {
  it('loads the manifest, then the list rows', async () => {
    const api = fakeApi([{ id: 1, name: 'Spring Kit' }]);
    const s = usePluginScreen('example', api);
    await s.load();
    expect(s.screens.value[0].title).toBe('Example Items');
    expect(s.rows.value).toEqual([{ id: 1, name: 'Spring Kit' }]);
    expect(api.get).toHaveBeenCalledWith('/api/plugins/example/ui');
    expect(api.get).toHaveBeenCalledWith('/api/plugins/example/items');
  });

  it('create posts to the manifest endpoint then refetches the rows', async () => {
    const api = fakeApi([]);
    const s = usePluginScreen('example', api);
    await s.load();
    api.get.mockResolvedValueOnce([{ id: 2, name: 'Cable' }]); // refetch result
    await s.create({ name: 'Cable' });
    expect(api.post).toHaveBeenCalledWith('/api/plugins/example/items', { name: 'Cable' });
    expect(s.rows.value).toEqual([{ id: 2, name: 'Cable' }]);
  });

  it('records an error when the manifest fetch fails', async () => {
    const api = { get: vi.fn(async () => { throw new Error('boom'); }), post: vi.fn() };
    const s = usePluginScreen('example', api);
    await s.load();
    expect(s.error.value).toBe('boom');
    expect(s.rows.value).toEqual([]);
  });

  // ── option-source guards ──────────────────────────────────────────────────
  it('only accepts options endpoints under this plugin namespace', () => {
    const s = usePluginScreen('demo', { get: vi.fn(), post: vi.fn() });
    expect(s.safePluginEndpoint('/api/plugins/demo/sizes')).toBe(true);
    expect(s.safePluginEndpoint('/api/plugins/other/secret')).toBe(false); // cross-plugin
    expect(s.safePluginEndpoint('/api/admin/users')).toBe(false);          // core
    expect(s.safePluginEndpoint('https://evil.example/x')).toBe(false);    // absolute
  });

  it('URL-encodes interpolated bindings (no param/path injection)', () => {
    const s = usePluginScreen('demo', { get: vi.fn(), post: vi.fn() });
    const url = s.interpolateEndpoint('/api/plugins/demo/sizes?model={model}', { model: 'a/b?c=d' });
    expect(url).toBe('/api/plugins/demo/sizes?model=a%2Fb%3Fc%3Dd');
  });

  it('fetchOptions refuses a foreign endpoint without calling the API', async () => {
    const api = { get: vi.fn(async () => [{ value: 'x' }]), post: vi.fn() };
    const s = usePluginScreen('demo', api);
    const opts = await s.fetchOptions(
      { name: 'size', options_endpoint: '/api/plugins/other/sizes' }, {},
    );
    expect(opts).toEqual([]);
    expect(api.get).not.toHaveBeenCalled();
    expect(s.error.value).toMatch(/refused/);
  });

  it('fetchOptions drops a stale (superseded) response — race guard', async () => {
    // first call resolves LAST; it must return null so the caller ignores it.
    let resolveFirst;
    const api = {
      get: vi.fn()
        .mockImplementationOnce(() => new Promise((r) => { resolveFirst = () => r([{ value: 'stale' }]); }))
        .mockImplementationOnce(async () => [{ value: 'fresh' }]),
      post: vi.fn(),
    };
    const s = usePluginScreen('demo', api);
    const field = { name: 'size', options_endpoint: '/api/plugins/demo/sizes?model={model}' };
    const p1 = s.fetchOptions(field, { model: 'A' });
    const p2 = s.fetchOptions(field, { model: 'B' });
    const fresh = await p2;        // newer request resolves first
    resolveFirst();
    const stale = await p1;        // older request resolves later
    expect(fresh).toEqual([{ value: 'fresh' }]);
    expect(stale).toBeNull();      // superseded → dropped
  });
});
