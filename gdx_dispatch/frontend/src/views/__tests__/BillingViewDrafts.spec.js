/**
 * BillingView — "Unsent Drafts" summary card (PR1-billing-capture 2026-07-07).
 *
 * Drafts are excluded from Total Outstanding (they aren't receivables yet),
 * which made a never-sent invoice invisible to every billing KPI — it could
 * sit forever unbilled. The drafts card surfaces the count + dollar total
 * and clicking it filters the invoice list to Draft.
 *
 * Pinned:
 *  1. The card renders with its testid, title, and both stat bindings.
 *  2. Clicking the card sets the Draft status filter.
 *  3. draftCount/draftTotal prefer the server summary pair and fall back
 *     to client-side computation over the loaded list (same contract as
 *     the other three KPI computeds).
 *  4. Outstanding still excludes drafts — surfacing must not pollute it.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'BillingView.vue'),
  'utf8',
);

describe('BillingView — Unsent Drafts card', () => {
  it('renders the drafts card with count and total', () => {
    expect(SRC).toMatch(/data-testid="billing-draft-invoices"/);
    expect(SRC).toMatch(/Unsent Drafts/);
    expect(SRC).toMatch(/\{\{\s*draftCount\s*\}\}/);
    expect(SRC).toMatch(/\{\{\s*currency\(draftTotal\)\s*\}\}/);
  });

  it('clicking the card filters the list to Draft', () => {
    const cardIdx = SRC.indexOf('data-testid="billing-draft-invoices"');
    expect(cardIdx).toBeGreaterThan(-1);
    // The click handler lives on the same Card tag.
    const tag = SRC.slice(SRC.lastIndexOf('<Card', cardIdx), SRC.indexOf('>', cardIdx) + 1);
    expect(tag).toMatch(/@click="activeStatus = 'Draft'"/);
  });

  it('draftCount prefers the server summary and falls back client-side', () => {
    const start = SRC.indexOf('const draftCount = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 400);
    expect(span).toMatch(/billingSummary\.value\.draft_count/);
    expect(span).toMatch(/inv\.status === "Draft"/);
  });

  it('draftTotal prefers the server summary and falls back client-side', () => {
    const start = SRC.indexOf('const draftTotal = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 500);
    expect(span).toMatch(/billingSummary\.value\.draft_total/);
    expect(span).toMatch(/inv\.status === "Draft"/);
    expect(span).toMatch(/toNum\(inv\.total\)/);
  });

  it('Total Outstanding still excludes drafts (surfacing must not pollute receivables)', () => {
    const start = SRC.indexOf('const totalOutstanding = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 500);
    expect(span).toMatch(/inv\.status !== "Draft"/);
  });
});
