/**
 * useForecasting — loads projection / recurring / settings independently.
 * Mocked API ensures: window param flows through, syncRecurring triggers
 * sync POST then list GET, saveSettings re-fetches projection.
 */
import { describe, expect, it, vi } from 'vitest';
import { useForecasting } from '../useForecasting';

function makeApi(handlers = {}) {
  const calls = [];
  return {
    calls,
    get: vi.fn(async (url) => {
      calls.push(['get', url]);
      const h = handlers.get?.[url];
      if (h instanceof Error) throw h;
      return typeof h === 'function' ? h() : h ?? {};
    }),
    post: vi.fn(async (url, body) => {
      calls.push(['post', url, body]);
      const h = handlers.post?.[url];
      if (h instanceof Error) throw h;
      return typeof h === 'function' ? h() : h ?? {};
    }),
    put: vi.fn(async (url, body) => {
      calls.push(['put', url, body]);
      const h = handlers.put?.[url];
      if (h instanceof Error) throw h;
      return typeof h === 'function' ? h() : h ?? {};
    }),
  };
}


describe('useForecasting', () => {
  it('loads projection with default 30-day window', async () => {
    const api = makeApi({
      get: { '/api/forecast/revenue?window=30': { expected_total: 1500 } },
    });
    const f = useForecasting(api);
    await f.loadProjection();
    expect(f.projection.value.expected_total).toBe(1500);
    expect(f.projectionError.value).toBeNull();
  });

  it('setWindow updates window ref and refetches', async () => {
    const api = makeApi({
      get: {
        '/api/forecast/revenue?window=30': { expected_total: 100 },
        '/api/forecast/revenue?window=60': { expected_total: 250 },
      },
    });
    const f = useForecasting(api);
    await f.loadProjection();
    expect(f.projection.value.expected_total).toBe(100);
    await f.setWindow(60);
    expect(f.window.value).toBe(60);
    expect(f.projection.value.expected_total).toBe(250);
  });

  it('loadRecurring extracts items from envelope', async () => {
    const api = makeApi({
      get: {
        '/api/quickbooks/recurring-transactions': {
          items: [{ qb_id: '1', name: 'Maintenance', amount: 250 }],
          total: 1,
        },
      },
    });
    const f = useForecasting(api);
    await f.loadRecurring();
    expect(f.recurring.value).toHaveLength(1);
    expect(f.recurring.value[0].name).toBe('Maintenance');
  });

  it('syncRecurring POSTs then refetches the cached list', async () => {
    const api = makeApi({
      post: { '/api/quickbooks/sync/recurring-transactions': { total: 2 } },
      get: { '/api/quickbooks/recurring-transactions': { items: [], total: 0 } },
    });
    const f = useForecasting(api);
    await f.syncRecurring();
    const urls = api.calls.map((c) => c[1]);
    expect(urls).toContain('/api/quickbooks/sync/recurring-transactions');
    expect(urls).toContain('/api/quickbooks/recurring-transactions');
  });

  it('saveSettings PUTs the patch and refetches projection', async () => {
    const api = makeApi({
      put: { '/api/forecast/settings': { collect_rate_0_30: 0.99 } },
      get: { '/api/forecast/revenue?window=30': { expected_total: 42 } },
    });
    const f = useForecasting(api);
    await f.saveSettings({ collect_rate_0_30: 0.99 });
    expect(f.settings.value.collect_rate_0_30).toBe(0.99);
    expect(api.put).toHaveBeenCalledWith(
      '/api/forecast/settings',
      { collect_rate_0_30: 0.99 },
      expect.any(Object),
    );
    expect(f.projection.value.expected_total).toBe(42);
  });

  it('records error on failed load without crashing', async () => {
    const api = makeApi({
      get: { '/api/forecast/revenue?window=30': new Error('boom') },
    });
    const f = useForecasting(api);
    await f.loadProjection();
    expect(f.projectionError.value).toBe('boom');
    expect(f.projection.value).toBeNull();
  });
});
