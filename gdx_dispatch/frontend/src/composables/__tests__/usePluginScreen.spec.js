import { describe, expect, it, vi } from 'vitest';
import { cellValue, usePluginScreen } from '../usePluginScreen';

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

/**
 * cellValue — PluginScreen took over `<Column>`'s #body slot so each cell can
 * carry its own label in the phone card layout (the header row is hidden
 * there). Taking the slot means resolving the field by hand, so this has to
 * keep the dotted-path support PrimeVue's resolveFieldData gave us for free —
 * otherwise a manifest declaring `field: "door.width"` renders blank cells.
 */
describe('cellValue', () => {
  const row = { id: 7, name: 'Spring Kit', price: 0, blank: null, door: { width: 96 } };

  it('reads a flat field', () => {
    expect(cellValue(row, 'name')).toBe('Spring Kit');
  });

  it('reads a dotted path', () => {
    expect(cellValue(row, 'door.width')).toBe(96);
  });

  it('preserves falsy values instead of blanking them', () => {
    // A $0 price and an empty cell are different facts on a quote.
    expect(cellValue(row, 'price')).toBe(0);
    expect(cellValue(row, 'blank')).toBeNull();
  });

  it('returns undefined for a missing field rather than throwing', () => {
    expect(cellValue(row, 'nope')).toBeUndefined();
    expect(cellValue(row, 'door.depth')).toBeUndefined();
    expect(cellValue(row, 'nope.deeper.still')).toBeUndefined();
  });

  it('survives a missing row or a malformed field', () => {
    expect(cellValue(null, 'name')).toBe('');
    expect(cellValue(row, '')).toBe('');
    expect(cellValue(row, undefined)).toBe('');
  });
});

describe('searchList — server-side search for list screens', () => {
  const screen = {
    type: 'list',
    endpoint: '/api/plugins/demoplugin/catalog',
    search: { param: 'q' },
  };

  it('sends the term to the server and replaces that screen rows', async () => {
    const api = {
      get: vi.fn(async (url) => {
        if (url.endsWith('/ui')) return { screens: [screen] };
        if (url.includes('q=9x8')) return [{ id: 1, description: '9x8 White' }];
        return [];
      }),
    };
    const s = usePluginScreen('demoplugin', api);
    await s.load();
    await s.searchList(screen, '9x8 white');

    const called = api.get.mock.calls.map((c) => c[0]);
    expect(called.some((u) => u.includes('q=9x8%20white'))).toBe(true);
    expect(s.rowsFor(screen)).toHaveLength(1);
  });

  it('refuses an endpoint outside the plugin namespace', async () => {
    const api = { get: vi.fn(async () => ({ screens: [] })) };
    const s = usePluginScreen('demoplugin', api);
    api.get.mockClear();
    await s.searchList({ type: 'list', endpoint: '/api/invoices', search: {} }, 'x');
    expect(api.get).not.toHaveBeenCalled();
  });

  it('does nothing for a screen that does not declare search', async () => {
    const api = { get: vi.fn(async () => []) };
    const s = usePluginScreen('demoplugin', api);
    api.get.mockClear();
    await s.searchList({ type: 'list', endpoint: '/api/plugins/demoplugin/quotes' }, 'x');
    expect(api.get).not.toHaveBeenCalled();
  });
});
