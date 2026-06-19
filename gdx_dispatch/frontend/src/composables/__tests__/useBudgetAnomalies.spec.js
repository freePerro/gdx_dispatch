/**
 * useBudgetAnomalies — sprint fix-in-quickbooks (2026-05-25).
 *
 * Verifies the composable issues the right URL/verb for load + apply,
 * tracks applying/applied/failed maps reactively, and builds the
 * correct QBO deep-link in openInQB().
 */
import { describe, expect, it, vi } from 'vitest';
import { useBudgetAnomalies } from '../useBudgetAnomalies';

function makeApi(handlers = {}) {
  const calls = [];
  function maybe(h) {
    if (h instanceof Error) throw h;
    return typeof h === 'function' ? h() : h ?? {};
  }
  return {
    calls,
    get:   vi.fn(async (url) => { calls.push(['get', url]); return maybe(handlers.get?.[url]); }),
    post:  vi.fn(async (url, body) => { calls.push(['post', url, body]); return maybe(handlers.post?.[url]); }),
  };
}

const FAKE_RESPONSE = {
  year: 2026,
  accounts: [{ qb_account_id: '124', account_name: 'Vehicle gas & fuel', transactions: [] }],
  qb_accounts: [{ qb_account_id: '1', name: 'Service Income', account_type: 'Income', active: true }],
  accounting_method: 'Accrual',
};

describe('useBudgetAnomalies', () => {
  it('load() GETs /api/budgets/anomalies with year + optional account_id', async () => {
    const url1 = `/api/budgets/anomalies?year=${new Date().getFullYear()}`;
    const url2 = `/api/budgets/anomalies?year=${new Date().getFullYear()}&account_id=124`;
    const api = makeApi({ get: { [url1]: FAKE_RESPONSE, [url2]: FAKE_RESPONSE } });
    const a = useBudgetAnomalies(api);
    await a.load();
    expect(api.calls.some(c => c[0] === 'get' && c[1] === url1)).toBe(true);
    await a.load('124');
    expect(api.calls.some(c => c[0] === 'get' && c[1] === url2)).toBe(true);
    expect(a.data.value.accounts.length).toBe(1);
  });

  it('apply() POSTs /api/budgets/recategorize with the txn + target', async () => {
    const api = makeApi();
    api.post.mockImplementation(async (url, body) => {
      api.calls.push(['post', url, body]);
      return { txn_id: body.txn_id, after_account_id: body.new_account_id, after_account_name: 'Service Income' };
    });
    const a = useBudgetAnomalies(api);
    const txn = { txn_id: 'T1', txn_type: 'Deposit' };
    await a.apply(txn, '1');
    const c = api.calls.find(c => c[0] === 'post' && c[1] === '/api/budgets/recategorize');
    expect(c).toBeTruthy();
    expect(c[2]).toEqual({ txn_id: 'T1', txn_type: 'Deposit', new_account_id: '1' });
    expect(a.applied.value.get('T1').after_account_name).toBe('Service Income');
    expect(a.failed.value.get('T1')).toBeUndefined();
  });

  it('apply() captures failure into the failed map without re-throwing on success path', async () => {
    const api = makeApi();
    api.post.mockImplementation(async () => { throw new Error('SyncToken mismatch'); });
    const a = useBudgetAnomalies(api);
    await expect(a.apply({ txn_id: 'T2', txn_type: 'Deposit' }, '1')).rejects.toThrow('SyncToken mismatch');
    expect(a.failed.value.get('T2')).toBe('SyncToken mismatch');
    expect(a.applied.value.get('T2')).toBeUndefined();
    expect(a.applying.value.has('T2')).toBe(false);
  });

  it('openInQB() opens app.qbo.intuit.com deep-link with correct entity slug', async () => {
    const originalOpen = global.window?.open;
    const opens = [];
    global.window = global.window || {};
    global.window.open = (url, target, features) => { opens.push({ url, target, features }); };

    const a = useBudgetAnomalies(makeApi());
    a.openInQB({ txn_id: 'D1', txn_type: 'Deposit' });
    a.openInQB({ txn_id: 'P1', txn_type: 'Purchase' });

    expect(opens[0].url).toBe('https://app.qbo.intuit.com/app/deposit?txnId=D1');
    expect(opens[1].url).toBe('https://app.qbo.intuit.com/app/expense?txnId=P1');
    expect(opens[0].target).toBe('_blank');

    global.window.open = originalOpen;
  });

  it('load() sets error + clears data on failure', async () => {
    const api = makeApi();
    api.get.mockImplementation(async () => { throw new Error('rate limited'); });
    const a = useBudgetAnomalies(api);
    await a.load();
    expect(a.error.value).toBe('rate limited');
    expect(a.data.value).toBeNull();
  });
});
