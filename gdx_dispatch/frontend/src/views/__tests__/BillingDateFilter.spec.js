/**
 * BillingView — date filter actually filters, KPIs follow it (2026-07-22).
 *
 * Doug: "if we filter invoices on billing for this year it shows none…
 * also there is no way to filter the ranges for total outstanding or
 * overdue."
 *
 * Root cause of "shows none": the date filter matches on
 * `inv.invoice_date || inv.created_at`, but normalizeInvoice dropped BOTH
 * fields — every row failed the window check, so every preset returned an
 * empty list. (Same disease as the sent_at column: the API sends the
 * field, the normalizer eats it.)
 *
 * Second fix: the KPI cards preferred the server summary, which is a
 * whole-book aggregate — no date filter could ever scope Total
 * Outstanding / Overdue. With a date filter active the cards now compute
 * client-side over the full loaded book (GET /api/invoices is
 * unpaginated), scoped by issue date; Paid becomes "Paid in Range"
 * scoped by paid_at.
 *
 * Pinned:
 *  1. normalizeInvoice maps invoice_date AND created_at.
 *  2. stampTime guards both day-walk-back traps: date-only strings parse
 *     local (parseLocalDateString) and QB midnight-UTC backfill stamps
 *     are treated as date-only (isDateOnlyStamp).
 *  3. filteredInvoices delegates to inDateWindow behind dateFilterActive.
 *  4. Every KPI computed bypasses the server summary when a date filter
 *     is active and draws from the window-scoped population.
 *  5. The Paid card retitles to "Paid in Range" under an active filter
 *     and scopes by paid_at, not issue date.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { stampTime } from '../../composables/useFormatters';

const SRC = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');

describe('BillingView — date filter fields survive normalization', () => {
  it('normalizeInvoice maps invoice_date and created_at', () => {
    const start = SRC.indexOf('function normalizeInvoice');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 1200);
    expect(span).toMatch(/invoice_date:\s*raw\.invoice_date/);
    expect(span).toMatch(/created_at:\s*raw\.created_at/);
  });

  it('filteredInvoices applies inDateWindow when a filter is active', () => {
    const start = SRC.indexOf('const filteredInvoices = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 700);
    expect(span).toMatch(/dateFilterActive\.value/);
    expect(span).toMatch(/list\.filter\(inDateWindow\)/);
  });

  it('issue date prefers invoice_date with created_at fallback, via stampTime', () => {
    const start = SRC.indexOf('function issueTime');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 200);
    expect(span).toMatch(/stampTime\(inv\.invoice_date \|\| inv\.created_at\)/);
  });
});

describe('stampTime — behavioral (the walk-back traps, not regexes)', () => {
  it('parses a date-only string as LOCAL midnight, not UTC', () => {
    // If this were new Date("2026-07-14") it would be UTC midnight — the
    // evening of Jul 13 anywhere in the US, so a "Today" preset on Jul 14
    // would exclude an invoice issued Jul 14.
    expect(stampTime('2026-07-14')).toBe(new Date(2026, 6, 14).getTime());
  });

  it('treats a QB midnight-UTC backfill stamp as its UTC calendar date, locally', () => {
    // A payment backfilled as Jan 1 UTC-midnight must land in Jan 1's
    // local day — NOT Dec 31 of last year.
    expect(stampTime('2026-01-01T00:00:00+00:00')).toBe(new Date(2026, 0, 1).getTime());
    expect(stampTime('2026-01-01T00:00:00Z')).toBe(new Date(2026, 0, 1).getTime());
  });

  it('keeps real datetimes as their actual instant', () => {
    const iso = '2026-05-06T14:03:00+00:00';
    expect(stampTime(iso)).toBe(new Date(iso).getTime());
  });

  it('returns null for empty or garbage input', () => {
    expect(stampTime('')).toBeNull();
    expect(stampTime(null)).toBeNull();
    expect(stampTime(undefined)).toBeNull();
    expect(stampTime('not-a-date')).toBeNull();
  });
});

describe('BillingView — KPI cards follow the date filter', () => {
  const kpiPins = [
    ['totalOutstanding', 'total_outstanding'],
    ['overdueAmount', 'overdue'],
    ['draftCount', 'draft_count'],
    ['draftTotal', 'draft_total'],
  ];

  for (const [name, summaryKey] of kpiPins) {
    it(`${name}: server summary only when NO date filter; window-scoped otherwise`, () => {
      const start = SRC.indexOf(`const ${name} = computed`);
      expect(start).toBeGreaterThan(-1);
      const span = SRC.slice(start, start + 700);
      // The summary short-circuit must be gated on the filter being inactive…
      expect(span).toMatch(new RegExp(`!dateFilterActive\\.value && billingSummary\\.value`));
      expect(span).toMatch(new RegExp(`billingSummary\\.value\\.${summaryKey}`));
      // …and the client-side path must draw from the window-scoped set.
      expect(span).toMatch(/kpiWindowInvoices\.value/);
    });
  }

  it('kpiWindowInvoices scopes by inDateWindow only when a filter is active', () => {
    const start = SRC.indexOf('const kpiWindowInvoices = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/dateFilterActive\.value/);
    expect(span).toMatch(/invoices\.value\.filter\(inDateWindow\)/);
  });

  it('Paid card: retitles under an active filter and scopes by paid_at ONLY', () => {
    expect(SRC).toMatch(/dateFilterActive \? 'Paid in Range' : 'Paid This Month'/);
    const start = SRC.indexOf('const paidThisMonth = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 1100);
    const branchStart = span.indexOf('if (dateFilterActive.value)');
    expect(branchStart).toBeGreaterThan(-1);
    // The date-filtered branch ends where the legacy server-summary branch
    // begins; the legacy fallback below it keeps its historical shape.
    const branch = span.slice(branchStart, span.indexOf('if (billingSummary'));
    // paid_at only: updated_at is never serialized on list rows — a
    // fallback to it is dead code that hides Paid rows with no real date.
    expect(branch).toMatch(/stampTime\(inv\.paid_at\)/);
    expect(branch).not.toMatch(/inv\.updated_at/);
  });

  it('client Outstanding excludes Void, matching the server aggregator', () => {
    const start = SRC.indexOf('const totalOutstanding = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 800);
    expect(span).toMatch(/inv\.status !== "Void"/);
  });
});
