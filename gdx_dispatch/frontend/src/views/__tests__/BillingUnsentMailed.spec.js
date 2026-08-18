/**
 * Billing — Unsent tab + paper-mail delivery (2026-08-05).
 *
 * sent_at is the delivery fact (2026-07-22), but the status tabs had no
 * view of its ABSENCE: an invoice finalized past Draft with no delivery
 * sat inside "Sent"/"Overdue" looking like the customer had it. And the
 * only way to stamp delivery was email — a paper invoice dropped in the
 * mailbox could never leave that bucket honestly.
 *
 * Pinned:
 *  1. BillingView has an "Unsent" status tab, derived (like Partial) via
 *     isUnsent: Sent/Overdue rows with an empty sent_at. Draft excluded
 *     (own tab); Paid/Void need no delivery.
 *  2. filteredInvoices and tabCount both route through isUnsent —
 *     a tab whose count and rows disagree is worse than no tab.
 *  3. normalizeInvoice carries sent_via so the channel survives the row
 *     mapping (the exact drop that blanked Last Sent in 2026-07).
 *  4. The Last Sent cell labels postal deliveries ("Mailed") — a mailed
 *     invoice's bare date otherwise reads like an email nobody can find.
 *  5. InvoiceDetailView has a Mark as Mailed action posting mark-sent
 *     with channel 'mail', hidden once mailed and on paid/void.
 *  6. The composer's two mark-sent calls (Outlook + mailto fallback) now
 *     declare channel 'email' — leaving them channel-less would misfile
 *     real emails as 'manual' forever.
 *  7. The detail header annotates a mailed stamp ("by mail").
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const BILLING = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');
const DETAIL = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

describe('BillingView — Unsent status tab', () => {
  it('statusTabs offers Unsent', () => {
    const start = BILLING.indexOf('const statusTabs = [');
    expect(start).toBeGreaterThan(-1);
    const span = BILLING.slice(start, BILLING.indexOf('];', start));
    expect(span).toMatch(/label:\s*"Unsent",\s*value:\s*"Unsent"/);
  });

  it('isUnsent = finalized past Draft, no delivery fact, and not a deposit', () => {
    const start = BILLING.indexOf('function isUnsent');
    expect(start).toBeGreaterThan(-1);
    const span = BILLING.slice(start, start + 300);
    expect(span).toMatch(/!inv\.sent_at/);
    expect(span).toMatch(/\["Sent",\s*"Overdue"\]\.includes\(inv\.status\)/);
    // Deposits are born 'sent' with sent_at empty BY DESIGN (portal pay
    // link is their delivery) — without this exclusion every unpaid
    // deposit is a permanent false positive in the tab.
    expect(span).toMatch(/billing_type !== "deposit"/);
  });

  it('filteredInvoices and tabCount both derive Unsent from isUnsent', () => {
    const filtStart = BILLING.indexOf('const filteredInvoices');
    expect(filtStart).toBeGreaterThan(-1);
    const filtSpan = BILLING.slice(filtStart, filtStart + 900);
    expect(filtSpan).toMatch(/=== "Unsent"/);
    expect(filtSpan).toMatch(/list\.filter\(isUnsent\)/);

    const countStart = BILLING.indexOf('function tabCount');
    expect(countStart).toBeGreaterThan(-1);
    const countSpan = BILLING.slice(countStart, countStart + 500);
    expect(countSpan).toMatch(/"Unsent".*filter\(isUnsent\)/);
  });

  it('normalizeInvoice keeps sent_via on the row', () => {
    const start = BILLING.indexOf('function normalizeInvoice');
    expect(start).toBeGreaterThan(-1);
    const span = BILLING.slice(start, start + 1600);
    expect(span).toMatch(/sent_via:\s*raw\.sent_via/);
  });

  it('Last Sent cell labels postal deliveries', () => {
    const idx = BILLING.indexOf('header="Last Sent"');
    expect(idx).toBeGreaterThan(-1);
    const tag = BILLING.slice(BILLING.lastIndexOf('<Column', idx), BILLING.indexOf('</Column>', idx));
    expect(tag).toMatch(/sent_via === 'mail'/);
    expect(tag).toMatch(/Mailed/);
  });
});

describe('InvoiceDetailView — Mark as Mailed', () => {
  it('offers the action, hidden on paid/void and once already mailed', () => {
    const idx = DETAIL.indexOf('data-testid="mark-mailed-btn"');
    expect(idx).toBeGreaterThan(-1);
    const tag = DETAIL.slice(DETAIL.lastIndexOf('<Button', idx), DETAIL.indexOf('/>', idx));
    expect(tag).toMatch(/!\['paid','void'\]\.includes/);
    expect(tag).toMatch(/invoice\.sent_via !== 'mail'/);
    expect(tag).toMatch(/@click="markAsMailed"/);
  });

  it('markAsMailed posts mark-sent with the mail channel and refetches', () => {
    const start = DETAIL.indexOf('async function markAsMailed');
    expect(start).toBeGreaterThan(-1);
    const span = DETAIL.slice(start, start + 900);
    expect(span).toMatch(/mark-sent`,\s*\{\s*channel:\s*"mail"\s*\}/);
    expect(span).toMatch(/fetchInvoice\(\)/);
  });

  it('only the mailto fallback still calls mark-sent — the composer send is server-stamped', () => {
    // Email overhaul 2026-08-18: sendComposer posts to /api/invoices/{id}/send,
    // which stamps status/sent_at/sent_via server-side in the same request.
    // The mailto fallback is the one remaining out-of-band handoff that needs
    // an explicit mark-sent with the email channel.
    const calls = DETAIL.match(/mark-sent`,\s*\{\s*channel:\s*"email"\s*\}/g) || [];
    expect(calls.length).toBe(1);
    expect(DETAIL).toMatch(/api\.post\(`\/api\/invoices\/\$\{route\.params\.id\}\/send`/);
  });

  it('normalizeInvoice maps payload.sent_via', () => {
    const start = DETAIL.indexOf('invoice.value = {');
    expect(start).toBeGreaterThan(-1);
    // Bound on the END of the object literal, not a byte count — a fixed
    // window fails whenever a field is added above the one being asserted
    // (job_id, 2026-08-12), which is a red test about the wrong thing.
    const end = DETAIL.indexOf('\n  };', start);
    const span = DETAIL.slice(start, end > start ? end : start + 4000);
    expect(span).toMatch(/sent_via:\s*payload\.sent_via/);
  });

  it('header annotates a mailed stamp', () => {
    const idx = DETAIL.indexOf('data-testid="invoice-last-sent"');
    expect(idx).toBeGreaterThan(-1);
    const tag = DETAIL.slice(DETAIL.lastIndexOf('<p', idx), DETAIL.indexOf('</p>', idx));
    expect(tag).toMatch(/sent_via === 'mail'/);
  });
});
