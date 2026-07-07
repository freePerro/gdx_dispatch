/**
 * CollectionsView — server-side AR aging wiring (PR1-billing-capture 2026-07-07).
 *
 * The audit found /api/collections/aging had ZERO consumers: the view built
 * its aging cards client-side from dunning entries only — invoices nobody
 * had logged a reminder for were invisible, and the server endpoint itself
 * returned $0 forever (capitalized-status filter). Both fixed; the view now
 * prefers the server report and falls back to the old client-side compute.
 *
 * Pinned:
 *  1. loadCollections fetches /api/collections/aging alongside /api/collections.
 *  2. agingBuckets prefers serverAging buckets when present.
 *  3. The client-side dunning-entry fallback is preserved (server failure
 *     must not blank the cards).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'CollectionsView.vue'),
  'utf8',
);

describe('CollectionsView — server aging preference', () => {
  it('fetches the server aging report with the collections load', () => {
    expect(SRC).toMatch(/api\.get\('\/api\/collections\/aging'\)/);
    const loadIdx = SRC.indexOf('async function loadCollections');
    expect(loadIdx).toBeGreaterThan(-1);
    const span = SRC.slice(loadIdx, loadIdx + 400);
    expect(span).toMatch(/loadServerAging\(\)/);
  });

  it('agingBuckets prefers server buckets when present', () => {
    const start = SRC.indexOf('const agingBuckets = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 900);
    expect(span).toMatch(/serverAging\.value\?\.buckets/);
    expect(span).toMatch(/toNum\(server\[i\]\?\.total\)/);
  });

  it('keeps the client-side dunning fallback when the server report is unavailable', () => {
    const start = SRC.indexOf('const agingBuckets = computed');
    const span = SRC.slice(start, start + 1600);
    expect(span).toMatch(/collections\.value\.forEach/);
    expect(span).toMatch(/days_overdue/);
  });

  it('server aging failure degrades to null (never throws the view)', () => {
    const fnIdx = SRC.indexOf('async function loadServerAging');
    expect(fnIdx).toBeGreaterThan(-1);
    const span = SRC.slice(fnIdx, fnIdx + 300);
    expect(span).toMatch(/catch/);
    expect(span).toMatch(/serverAging\.value = null/);
  });
});
