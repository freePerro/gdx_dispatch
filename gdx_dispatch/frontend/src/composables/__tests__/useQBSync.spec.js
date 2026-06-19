/**
 * useQBSync — orchestrates the QuickBooks sync wizard's per-entity progress.
 * Covers: default step list (S103 added accounts + bank_transactions), counts
 * mapping for single-entity vs full-sync responses, error rollup, idempotent
 * start guard.
 */
import { describe, expect, it, vi } from 'vitest';
import { useQBSync } from '../useQBSync';


function makeFakeApi(responsesByUrl) {
  return {
    post: vi.fn(async (url) => {
      if (Object.prototype.hasOwnProperty.call(responsesByUrl, url)) {
        const r = responsesByUrl[url];
        if (r instanceof Error) throw r;
        return r;
      }
      throw new Error(`No fake response wired for ${url}`);
    }),
  };
}


describe('useQBSync', () => {
  it('exposes 6 default steps including accounts + bank_transactions (S103)', () => {
    const api = makeFakeApi({});
    const { steps } = useQBSync(api);
    const keys = steps.map((s) => s.key);
    // Order matters — wizard runs sequentially.
    expect(keys).toEqual([
      'customers',
      'invoices',
      'items',
      'vendors_payments',
      'accounts',
      'bank_transactions',
    ]);
    // Every step starts pending with zero counts.
    for (const s of steps) {
      expect(s.status).toBe('pending');
      expect(s.created).toBe(0);
      expect(s.updated).toBe(0);
      expect(s.errors).toEqual([]);
    }
  });

  it('runs each step in order, applying single-entity counts', async () => {
    const api = makeFakeApi({
      '/api/qb/sync/customers': { created: 3, updated: 1, adopted: 0, errors: [] },
      '/api/qb/sync/invoices': { created: 5, updated: 0, adopted: 0, errors: [] },
      '/api/qb/sync/items': { created: 0, updated: 2, adopted: 0, errors: [] },
      '/api/qb/sync/full': {
        customers: {}, invoices: {}, items: {},
        vendors: { created: 4, updated: 0, errors: [] },
        payments: { created: 2, updated: 1, errors: [] },
      },
      '/api/qb/sync/accounts': { created: 7, updated: 0, errors: [] },
      '/api/qb/sync/bank-transactions': { created: 12, updated: 0, errors: [] },
    });

    const { steps, start, overallStatus, running } = useQBSync(api);
    await start();

    expect(running.value).toBe(false);
    expect(overallStatus.value).toBe('done');
    expect(api.post).toHaveBeenCalledTimes(6);

    expect(steps[0]).toMatchObject({ key: 'customers', status: 'done', created: 3, updated: 1 });
    expect(steps[1]).toMatchObject({ key: 'invoices', status: 'done', created: 5 });
    // Vendors+payments step pulls only the vendors/payments slice of /sync/full.
    expect(steps[3]).toMatchObject({ key: 'vendors_payments', status: 'done', created: 6, updated: 1 });
    expect(steps[4]).toMatchObject({ key: 'accounts', status: 'done', created: 7 });
    expect(steps[5]).toMatchObject({ key: 'bank_transactions', status: 'done', created: 12 });
  });

  it('marks a step as error and reports partial overall status when one POST fails', async () => {
    const api = makeFakeApi({
      '/api/qb/sync/customers': { created: 1, updated: 0, errors: [] },
      '/api/qb/sync/invoices': new Error('502 Bad Gateway'),
      '/api/qb/sync/items': { created: 0, updated: 0, errors: [] },
      '/api/qb/sync/full': { vendors: {}, payments: {} },
      '/api/qb/sync/accounts': { created: 1, updated: 0, errors: [] },
      '/api/qb/sync/bank-transactions': { created: 1, updated: 0, errors: [] },
    });

    const { steps, start, overallStatus } = useQBSync(api);
    await start();

    expect(overallStatus.value).toBe('partial');
    expect(steps[1].status).toBe('error');
    expect(steps[1].message).toBe('502 Bad Gateway');
    // Subsequent steps still execute.
    expect(steps[4].status).toBe('done');
    expect(steps[5].status).toBe('done');
  });

  it('start() is a no-op while a previous run is still in flight', async () => {
    let resolveFirst;
    const blockingPromise = new Promise((r) => { resolveFirst = r; });
    const api = {
      post: vi.fn().mockImplementationOnce(() => blockingPromise),
    };
    const { start, running } = useQBSync(api);
    const first = start();
    // Re-entry while running. The composable returns immediately without
    // queuing a second pass — guards against double-clicking the Sync button.
    await start();
    expect(running.value).toBe(true);
    expect(api.post).toHaveBeenCalledTimes(1);
    resolveFirst({ created: 1, updated: 0, errors: [] });
    await first.catch(() => {});
  });
});
