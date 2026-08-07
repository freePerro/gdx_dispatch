/**
 * JobDetailView — Details-tab invoice strip (2026-08-07).
 *
 * The closeout autodraft means every closed-out job already has an invoice,
 * but the Invoices table hides two clicks deep in the Costing tab. Pinned:
 *  1. The strip renders on the DETAILS tab (the default), gated on live
 *     invoices existing.
 *  2. Void invoices are excluded from the strip.
 *  3. Drafts get the "Review invoice" verb; the click goes through the same
 *     openInvoice(id) → /billing/{id} navigation the Costing table uses.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'JobDetailView.vue'), 'utf8');

describe('JobDetailView — Details-tab invoice strip', () => {
  it('renders inside the details tab, gated on live invoices', () => {
    const detailsIdx = SRC.indexOf(`v-if="activeTab === 'details'"`);
    const stripIdx = SRC.indexOf('data-testid="job-invoice-strip"');
    const nextTabIdx = SRC.indexOf(`activeTab === 'schedule'`);
    expect(stripIdx).toBeGreaterThan(detailsIdx);
    expect(stripIdx).toBeLessThan(nextTabIdx);
    const before = SRC.slice(Math.max(0, stripIdx - 200), stripIdx);
    expect(before).toMatch(/v-if="liveInvoices\.length"/);
  });

  it('liveInvoices excludes void invoices', () => {
    const start = SRC.indexOf('const liveInvoices = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/relatedInvoices/);
    expect(span).toMatch(/status !== "void"/);
  });

  it('drafts get the Review verb and the shared openInvoice navigation', () => {
    const strip = SRC.slice(SRC.indexOf('data-testid="job-invoice-strip"'), SRC.indexOf('data-testid="job-invoice-strip"') + 1600);
    expect(strip).toMatch(/inv\.status === 'draft' \? 'Review invoice' : 'Open invoice'/);
    expect(strip).toMatch(/@click="openInvoice\(inv\.id\)"/);
  });
});
