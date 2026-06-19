/**
 * useRecurringStreams — drives the /api/forecast/recurring/streams/* surface.
 * Mocked API ensures the right method+URL+payload flows for each verb, and
 * that list() with a status param appends ?status= correctly.
 */
import { describe, expect, it, vi } from 'vitest';
import { useRecurringStreams } from '../useRecurringStreams';

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
    post: vi.fn(async (url, body, opts) => {
      calls.push(['post', url, body, opts]);
      const h = handlers.post?.[url];
      if (h instanceof Error) throw h;
      return typeof h === 'function' ? h() : h ?? {};
    }),
    patch: vi.fn(async (url, body) => {
      calls.push(['patch', url, body]);
      return handlers.patch?.[url] ?? {};
    }),
    // useApi exposes the DELETE verb as `del` not `delete`.
    del: vi.fn(async (url) => {
      calls.push(['del', url]);
      return handlers.del?.[url] ?? {};
    }),
  };
}

describe('useRecurringStreams', () => {
  it('list() without status fetches the unfiltered endpoint', async () => {
    const api = makeApi({ get: { '/api/forecast/recurring/streams': { items: [{ id: 'a' }], total: 1 } } });
    const r = useRecurringStreams(api);
    await r.list();
    expect(api.calls).toEqual([['get', '/api/forecast/recurring/streams']]);
    expect(r.streams.value).toEqual([{ id: 'a' }]);
  });

  it('list("suggested") appends status query param', async () => {
    const api = makeApi({ get: { '/api/forecast/recurring/streams?status=suggested': { items: [], total: 0 } } });
    const r = useRecurringStreams(api);
    await r.list('suggested');
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams?status=suggested');
  });

  it('list() surfaces errors without throwing', async () => {
    const api = makeApi({ get: { '/api/forecast/recurring/streams': new Error('boom') } });
    const r = useRecurringStreams(api);
    await r.list();
    expect(r.error.value).toMatch(/boom/);
    expect(r.loading.value).toBe(false);
  });

  it('confirm() POSTs to /confirm with success toast', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.confirm('abc-123');
    expect(api.calls[0][0]).toBe('post');
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams/abc-123/confirm');
    expect(api.calls[0][3]?.successMessage).toMatch(/confirmed/i);
  });

  it('end() POSTs reason payload and reports the chosen reason', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.end('xyz', { reason: 'paid_off', ended_at: '2026-05-20' });
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams/xyz/end');
    expect(api.calls[0][2]).toEqual({ reason: 'paid_off', ended_at: '2026-05-20' });
    expect(api.calls[0][3]?.successMessage).toMatch(/paid off/i);
  });

  it('createFromTransaction() targets the /from-transaction endpoint', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.createFromTransaction({ qb_txn_id: 't1', cadence: 'monthly' });
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams/from-transaction');
  });

  it('patch() targets the stream ID', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.patch('s1', { label: 'New' });
    expect(api.calls[0][0]).toBe('patch');
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams/s1');
    expect(api.calls[0][2]).toEqual({ label: 'New' });
  });

  it('dismiss() soft-deletes via api.del (NOT api.delete)', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.dismiss('s2');
    expect(api.calls[0]).toEqual(['del', '/api/forecast/recurring/streams/s2']);
  });

  it('contract: only calls api methods that useApi actually exposes (no fictional methods)', async () => {
    // Auditor caught api.delete vs api.del. Pin this down so the mock can't
    // diverge from the real composable surface again. Source-scan rather than
    // instantiate (useApi needs Pinia which isn't booted in this test).
    const fs = await import('node:fs');
    const path = await import('node:path');
    const apiSrc = fs.readFileSync(
      path.resolve(__dirname, '../useApi.js'),
      'utf-8',
    );
    const composableSrc = fs.readFileSync(
      path.resolve(__dirname, '../useRecurringStreams.js'),
      'utf-8',
    );
    // Methods returned by useApi (the two return statements both share these keys)
    const exposed = new Set(['get', 'post', 'put', 'patch', 'del', 'postQueued', 'patchQueued', 'request']);
    const matches = [...composableSrc.matchAll(/\bapi\.([a-z][a-zA-Z]+)\s*\(/g)];
    for (const m of matches) {
      const name = m[1];
      expect(exposed.has(name), `useRecurringStreams calls api.${name} but useApi exposes none such`).toBe(true);
    }
    // Sanity: the apiSrc actually does export `del` (would catch a useApi rename).
    expect(apiSrc).toMatch(/\bdel\b/);
  });

  it('unlinkHit() targets the nested hit endpoint', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.unlinkHit('s1', 'h1');
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/streams/s1/hits/h1/unlink');
  });

  it('detectNow() triggers the on-demand detector', async () => {
    const api = makeApi();
    const r = useRecurringStreams(api);
    await r.detectNow();
    expect(api.calls[0][1]).toBe('/api/forecast/recurring/detect');
  });
});
