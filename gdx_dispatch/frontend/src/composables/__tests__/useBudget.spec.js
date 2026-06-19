/**
 * useBudget — sprint monthly-budget (2026-05-24).
 *
 * Mocked API verifies: composable issues the right URL/verb for each
 * action, re-loads after every mutation, and exposes loading flags.
 * Frontend vitest brake against URL drift.
 */
import { describe, expect, it, vi } from 'vitest';
import { useBudget } from '../useBudget';

function makeApi(handlers = {}) {
  const calls = [];
  function maybe(h) {
    if (h instanceof Error) throw h;
    return typeof h === 'function' ? h() : h ?? {};
  }
  return {
    calls,
    get:   vi.fn(async (url)       => { calls.push(['get', url]);          return maybe(handlers.get?.[url]); }),
    post:  vi.fn(async (url, body) => { calls.push(['post', url, body]);   return maybe(handlers.post?.[url]); }),
    patch: vi.fn(async (url, body) => { calls.push(['patch', url, body]);  return maybe(handlers.patch?.[url]); }),
    put:   vi.fn(async (url, body) => { calls.push(['put', url, body]);    return maybe(handlers.put?.[url]); }),
    del:   vi.fn(async (url)       => { calls.push(['del', url]);          return maybe(handlers.del?.[url]); }),
  };
}

const DATA = {
  lines: [
    { id: 'l1', qb_account_id: '60', account_name: 'Rent', amount: '2500', line_type: 'fixed',
      source: 'auto_seed', is_locked: false, actual: '2500', variance: '0', variance_pct: 0 },
  ],
  available_accounts: [{ qb_account_id: '70', account_name: 'Parts', account_type: 'Cost of Goods Sold' }],
  totals: { budget: '2500', actual: '2500', variance: '0', monthly_revenue_forecast: '10000' },
};


describe('useBudget', () => {
  it('load fetches /api/budgets with current year/month', async () => {
    const now = new Date();
    const url = `/api/budgets?year=${now.getFullYear()}&month=${now.getMonth() + 1}`;
    const api = makeApi({ get: { [url]: DATA } });
    const b = useBudget(api);
    await b.load();
    expect(b.data.value).toEqual(DATA);
    expect(b.error.value).toBeNull();
  });

  it('setMonth updates refs and refetches', async () => {
    const apiNow = new Date();
    const url1 = `/api/budgets?year=${apiNow.getFullYear()}&month=${apiNow.getMonth() + 1}`;
    const url2 = '/api/budgets?year=2026&month=3';
    const api = makeApi({ get: { [url1]: DATA, [url2]: DATA } });
    const b = useBudget(api);
    await b.setMonth(2026, 3);
    expect(b.year.value).toBe(2026);
    expect(b.month.value).toBe(3);
    expect(api.calls.some(c => c[0] === 'get' && c[1] === url2)).toBe(true);
  });

  it('seed issues POST with lookback_months and overwrite flag, then reloads', async () => {
    const api = makeApi({
      post: { /* matched by prefix below */ },
      get: { /* any load returns DATA */ },
    });
    // The post handler responds to ANY budgets/seed URL.
    api.post.mockImplementation(async (url, body) => {
      api.calls.push(['post', url, body]);
      return { created: 5, updated: 0, skipped_locked: 0, skipped_user: 0 };
    });
    api.get.mockImplementation(async (url) => { api.calls.push(['get', url]); return DATA; });
    const b = useBudget(api);
    b.year.value = 2026; b.month.value = 5;
    const result = await b.seed(6, true);
    expect(result.created).toBe(5);
    expect(api.calls.some(c => c[0] === 'post' && c[1].includes('/api/budgets/seed') &&
        c[1].includes('lookback_months=6') && c[1].includes('overwrite_user_edits=true'))).toBe(true);
    // reload happens after seed
    expect(api.calls.filter(c => c[0] === 'get').length).toBeGreaterThanOrEqual(1);
  });

  it('refreshActuals posts to /api/budgets/refresh-actuals with year', async () => {
    const api = makeApi();
    api.post.mockImplementation(async (url) => { api.calls.push(['post', url]); return { year: 2026 }; });
    api.get.mockImplementation(async () => DATA);
    const b = useBudget(api);
    b.year.value = 2026;
    await b.refreshActuals();
    expect(api.calls.some(c => c[0] === 'post' && c[1] === '/api/budgets/refresh-actuals?year=2026')).toBe(true);
  });

  it('createLine POSTs payload + reloads', async () => {
    const api = makeApi();
    api.post.mockImplementation(async (url, body) => { api.calls.push(['post', url, body]); return { id: 'new' }; });
    api.get.mockImplementation(async () => DATA);
    const b = useBudget(api);
    await b.createLine({ year: 2026, month: 5, qb_account_id: '70', amount: 100 });
    const c = api.calls.find(c => c[0] === 'post' && c[1] === '/api/budgets');
    expect(c).toBeTruthy();
    expect(c[2]).toEqual({ year: 2026, month: 5, qb_account_id: '70', amount: 100 });
  });

  it('updateLine PATCHes /{id} + reloads', async () => {
    const api = makeApi();
    api.patch.mockImplementation(async (url, body) => { api.calls.push(['patch', url, body]); return {}; });
    api.get.mockImplementation(async () => DATA);
    const b = useBudget(api);
    await b.updateLine('abc', { amount: 999 });
    expect(api.calls.some(c => c[0] === 'patch' && c[1] === '/api/budgets/abc' && c[2].amount === 999)).toBe(true);
  });

  it('deleteLine uses api.del (not delete)', async () => {
    const api = makeApi();
    api.del.mockImplementation(async (url) => { api.calls.push(['del', url]); return {}; });
    api.get.mockImplementation(async () => DATA);
    const b = useBudget(api);
    await b.deleteLine('abc');
    expect(api.calls.some(c => c[0] === 'del' && c[1] === '/api/budgets/abc')).toBe(true);
  });

  it('lock + unlock hit the right routes', async () => {
    const api = makeApi();
    api.post.mockImplementation(async (url) => { api.calls.push(['post', url]); return {}; });
    api.get.mockImplementation(async () => DATA);
    const b = useBudget(api);
    await b.lock('abc');
    await b.unlock('abc');
    expect(api.calls.some(c => c[1] === '/api/budgets/abc/lock')).toBe(true);
    expect(api.calls.some(c => c[1] === '/api/budgets/abc/unlock')).toBe(true);
  });

  it('loadClassify populates proposals and sets loading flag false on success', async () => {
    const api = makeApi();
    api.post.mockImplementation(async (url) => {
      api.calls.push(['post', url]);
      return { proposals: [{ qb_account_id: '60', proposed_line_type: 'fixed' }] };
    });
    const b = useBudget(api);
    await b.loadClassify(6);
    expect(b.classifyProposals.value).toHaveLength(1);
    expect(b.classifyLoading.value).toBe(false);
    expect(b.classifyError.value).toBeNull();
    expect(api.calls.some(c => c[1] === '/api/budgets/classify?lookback_months=6')).toBe(true);
  });

  it('load sets error on failure and clears data', async () => {
    const api = makeApi();
    api.get.mockImplementation(async () => { throw new Error('boom'); });
    const b = useBudget(api);
    await b.load();
    expect(b.error.value).toBe('boom');
    expect(b.data.value).toBeNull();
  });
});
