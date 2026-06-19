import { describe, expect, it } from 'vitest';
import { buildBankingSyncSummary } from '../bankingSyncSummary.js';


describe('buildBankingSyncSummary', () => {
  it('returns a placeholder when result is missing', () => {
    expect(buildBankingSyncSummary(null)).toEqual({ summary: 'Banking synced', totalErrors: 0 });
    expect(buildBankingSyncSummary(undefined)).toEqual({ summary: 'Banking synced', totalErrors: 0 });
  });

  it('summarises a clean sync without errors', () => {
    const r = {
      accounts:  { created: 0, updated: 12, errors: [] },
      purchases: { created: 4, updated: 1, errors: [] },
      deposits:  { created: 2, updated: 0, deleted: 1, errors: [] },
      transfers: { created: 0, updated: 0, errors: [] },
    };
    const out = buildBankingSyncSummary(r);
    expect(out.totalErrors).toBe(0);
    // Order is fixed: accounts, purchases, deposits, transfers.
    expect(out.summary).toBe('Banking: accounts ~12, purchases +4/~1, deposits +2/-1');
  });

  it('includes all 8 entity types when present', () => {
    const r = {
      accounts:        { created: 0, updated: 1, errors: [] },
      purchases:       { created: 2, updated: 0, errors: [] },
      deposits:        { created: 0, updated: 0, deleted: 1, errors: [] },
      transfers:       { created: 1, updated: 0, errors: [] },
      bill_payments:   { created: 3, updated: 0, errors: [] },
      sales_receipts:  { created: 5, updated: 0, errors: [] },
      refund_receipts: { created: 0, updated: 1, errors: [] },
      journal_entries: { created: 2, updated: 0, errors: [] },
    };
    const out = buildBankingSyncSummary(r);
    expect(out.totalErrors).toBe(0);
    expect(out.summary).toContain('accounts ~1');
    expect(out.summary).toContain('purchases +2');
    expect(out.summary).toContain('deposits -1');
    expect(out.summary).toContain('transfers +1');
    expect(out.summary).toContain('bill pmts +3');
    expect(out.summary).toContain('sales rcpts +5');
    expect(out.summary).toContain('refunds ~1');
    expect(out.summary).toContain('journals +2');
  });

  it('flags totalErrors and includes per-entity err counts', () => {
    const r = {
      accounts:  { created: 0, updated: 5,  errors: [] },
      purchases: { created: 1, updated: 0,  errors: [{ qb_id: 'p1', error: 'boom' }] },
      deposits:  { created: 0, updated: 0,  errors: [{ qb_id: 'd1', error: 'x' }, { qb_id: 'd2', error: 'y' }] },
      transfers: { created: 0, updated: 0,  errors: [] },
    };
    const out = buildBankingSyncSummary(r);
    expect(out.totalErrors).toBe(3);
    expect(out.summary).toContain('1 err');
    expect(out.summary).toContain('2 err');
  });

  it('includes the since-date when provided', () => {
    const r = { accounts: { created: 0, updated: 1, errors: [] } };
    const out = buildBankingSyncSummary(r, '2026-01-01');
    expect(out.summary).toBe('Banking since 2026-01-01: accounts ~1');
  });

  it('shows "no changes" when nothing was touched', () => {
    const r = {
      accounts:  { created: 0, updated: 0, errors: [] },
      purchases: { created: 0, updated: 0, errors: [] },
      deposits:  { created: 0, updated: 0, deleted: 0, errors: [] },
      transfers: { created: 0, updated: 0, errors: [] },
    };
    expect(buildBankingSyncSummary(r).summary).toBe('Banking: no changes');
  });

  it('skips entities the backend omitted from the response', () => {
    const r = {
      accounts:  { created: 0, updated: 1, errors: [] },
      // purchases / deposits / transfers absent
    };
    const out = buildBankingSyncSummary(r);
    expect(out.summary).toBe('Banking: accounts ~1');
    expect(out.totalErrors).toBe(0);
  });
});
