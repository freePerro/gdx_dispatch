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
});
