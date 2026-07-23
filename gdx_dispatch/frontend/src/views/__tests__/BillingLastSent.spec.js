/**
 * Billing — "Last Sent" surfacing (2026-07-22).
 *
 * Invoices go out by email, but nothing in billing showed WHEN one was
 * last emailed. The server stamps invoice.sent_at on acknowledged sends
 * and GET /api/invoices already serializes it — the list normalizer just
 * dropped the field and neither view rendered it.
 *
 * Adversarial-audit catches folded in (2026-07-22):
 *  - QB-backfilled invoices carry sent_at as UTC MIDNIGHT of the invoice
 *    date (modules/quickbooks/sync.py). Naive datetime rendering walks the
 *    day back in every US timezone and invents a phantom evening time —
 *    and that's most of the historical book. Hence formatStampDate /
 *    formatStampDateTime + isDateOnlyStamp.
 *  - CSV exports must carry the column too, not just the DataTable.
 *
 * Pinned:
 *  1. BillingView has a sortable "Last Sent" column reading sent_at via
 *     the backfill-aware formatter; the time tooltip is suppressed for
 *     date-only stamps.
 *  2. BillingView.normalizeInvoice carries sent_at through — dropping it
 *     there silently blanks the column while the API keeps sending it.
 *  3. Both CSV exports (bulkExport + exportInvoices) include Last Sent.
 *  4. InvoiceDetailView header shows the stamp when present, via the
 *     backfill-aware datetime formatter.
 *  5. InvoiceDetailView.normalizeInvoice maps payload.sent_at.
 *  6. The formatter trio exists in useFormatters and midnight-UTC
 *     detection anchors to end-of-string (a real 00:00:05 send or a
 *     non-UTC-midnight offset must NOT be treated as date-only).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import {
  formatStampDate,
  formatStampDateTime,
  isDateOnlyStamp,
} from '../../composables/useFormatters';

const BILLING = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');
const DETAIL = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

describe('useFormatters — date-only stamp handling', () => {
  it('detects the QB backfill convention (UTC midnight), with and without offset', () => {
    expect(isDateOnlyStamp('2026-05-06T00:00:00+00:00')).toBe(true);
    expect(isDateOnlyStamp('2026-05-06T00:00:00Z')).toBe(true);
    expect(isDateOnlyStamp('2026-05-06T00:00:00')).toBe(true); // SQLite naive
  });

  it('does NOT flag real timestamps or non-UTC offsets', () => {
    expect(isDateOnlyStamp('2026-05-06T00:00:05+00:00')).toBe(false);
    expect(isDateOnlyStamp('2026-05-06T14:03:00+00:00')).toBe(false);
    expect(isDateOnlyStamp('2026-05-06T00:00:00-05:00')).toBe(false);
    expect(isDateOnlyStamp(null)).toBe(false);
    expect(isDateOnlyStamp('')).toBe(false);
  });

  it('renders a backfilled stamp on the RIGHT calendar day (no UTC walk-back)', () => {
    // The trap: new Date("2026-05-06T00:00:00+00:00") is May 5 evening in
    // every US timezone. The stamp formatter must show May 6 regardless of
    // the host timezone, and must not invent a time.
    expect(formatStampDate('2026-05-06T00:00:00+00:00', { locale: 'en-US' })).toBe('May 6, 2026');
    expect(formatStampDateTime('2026-05-06T00:00:00+00:00', { locale: 'en-US' })).toBe('May 6, 2026');
  });

  it('keeps time-of-day for real send stamps', () => {
    const out = formatStampDateTime('2026-05-06T14:03:00+00:00', { locale: 'en-US' });
    expect(out).toMatch(/May 6, 2026/);
    expect(out).toMatch(/\d{1,2}:\d{2}/);
  });

  it('placeholder for empty input', () => {
    expect(formatStampDate('')).toBe('—');
    expect(formatStampDateTime(null)).toBe('—');
  });
});

describe('BillingView — Last Sent column', () => {
  it('renders a sortable column bound to sent_at via the backfill-aware formatter', () => {
    const idx = BILLING.indexOf('header="Last Sent"');
    expect(idx).toBeGreaterThan(-1);
    const tag = BILLING.slice(BILLING.lastIndexOf('<Column', idx), BILLING.indexOf('</Column>', idx));
    expect(tag).toMatch(/field="sent_at"/);
    expect(tag).toMatch(/sortable/);
    expect(tag).toMatch(/formatStampDate\(data\.sent_at\)/);
    // Full timestamp on hover — but only when a real time exists.
    expect(tag).toMatch(/!isDateOnlyStamp\(data\.sent_at\)/);
    expect(tag).toMatch(/formatDateTime\(data\.sent_at\)/);
  });

  it('normalizeInvoice keeps sent_at on the row', () => {
    const start = BILLING.indexOf('function normalizeInvoice');
    expect(start).toBeGreaterThan(-1);
    const span = BILLING.slice(start, start + 900);
    expect(span).toMatch(/sent_at:\s*raw\.sent_at/);
  });

  it('both CSV exports carry the Last Sent column', () => {
    // bulkExport (selected rows, hand-rolled)
    const bulkStart = BILLING.indexOf('function bulkExport');
    expect(bulkStart).toBeGreaterThan(-1);
    const bulkSpan = BILLING.slice(bulkStart, bulkStart + 700);
    expect(bulkSpan).toMatch(/"Last Sent"/);
    expect(bulkSpan).toMatch(/i\.sent_at/);
    // exportInvoices (filtered rows via useTableExport)
    const expStart = BILLING.indexOf('function exportInvoices');
    expect(expStart).toBeGreaterThan(-1);
    const expSpan = BILLING.slice(expStart, expStart + 700);
    expect(expSpan).toMatch(/field:\s*"sent_at",\s*header:\s*"Last Sent"/);
  });
});

describe('InvoiceDetailView — last-sent stamp', () => {
  it('shows the stamp in the header only when sent_at exists', () => {
    const idx = DETAIL.indexOf('data-testid="invoice-last-sent"');
    expect(idx).toBeGreaterThan(-1);
    const tag = DETAIL.slice(DETAIL.lastIndexOf('<p', idx), DETAIL.indexOf('</p>', idx));
    expect(tag).toMatch(/v-if="invoice\.sent_at"/);
    expect(tag).toMatch(/formatStampDateTime\(invoice\.sent_at\)/);
  });

  it('normalizeInvoice maps payload.sent_at', () => {
    const start = DETAIL.indexOf('invoice.value = {');
    expect(start).toBeGreaterThan(-1);
    const span = DETAIL.slice(start, start + 1600);
    expect(span).toMatch(/sent_at:\s*payload\.sent_at/);
  });
});
