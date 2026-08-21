import { describe, expect, it } from 'vitest';

import { qbSyncLabel } from '../qbSyncLabel';

const when = (v) => `AT:${v}`;

describe('qbSyncLabel', () => {
  it('never promises a push that will not happen', () => {
    // The whole point of the 2026-08-21 change. QuickBooks is phased out, the
    // connection reads needs_reconnect, and 88 of 355 live invoices carry
    // qb_dirty=true. "changes pending" told the office to expect a sync.
    const out = qbSyncLabel(
      { qb_in_quickbooks: true, qb_synced_at: '2026-05-13T00:00:00Z', qb_dirty: true },
      when,
    );
    expect(out.label).not.toMatch(/pending/i);
    expect(out.label).toBe('Last synced AT:2026-05-13T00:00:00Z · changed since');
  });

  it('does not raise a warning the operator cannot act on', () => {
    const out = qbSyncLabel(
      { qb_in_quickbooks: true, qb_synced_at: '2026-05-13T00:00:00Z', qb_dirty: true },
      when,
    );
    expect(out.severity).toBe('secondary');
    expect(out.severity).not.toBe('warn');
  });

  it('reports a clean sync as a plain fact', () => {
    expect(
      qbSyncLabel(
        { qb_in_quickbooks: true, qb_synced_at: '2026-05-13T00:00:00Z', qb_dirty: false },
        when,
      ),
    ).toEqual({ label: 'Synced AT:2026-05-13T00:00:00Z', severity: 'success' });
  });

  it('treats a missing qb_dirty as changed-since, not as clean', () => {
    // qb_dirty defaults TRUE server-side; absent must not read as synced-clean.
    const out = qbSyncLabel(
      { qb_in_quickbooks: true, qb_synced_at: '2026-05-13T00:00:00Z' },
      when,
    );
    expect(out.label).toMatch(/changed since$/);
  });

  it('never claims "not in QB" from a missing timestamp alone', () => {
    // qb_synced_at is stamped only by the selective-push path and is
    // un-backfilled — a legacy/imported/manual record is in QB with a NULL
    // timestamp. Reading NULL as "not synced" was the original bug here.
    expect(qbSyncLabel({ qb_in_quickbooks: true, qb_synced_at: null }, when)).toEqual({
      label: 'Synced',
      severity: 'success',
    });
  });

  it('reports a record that was never pushed', () => {
    expect(qbSyncLabel({ qb_in_quickbooks: false, qb_synced_at: null }, when)).toEqual({
      label: 'Never synced',
      severity: 'secondary',
    });
  });

  it('survives an empty row without throwing', () => {
    expect(qbSyncLabel(undefined, when).label).toBe('Never synced');
  });
});
