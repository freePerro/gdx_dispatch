/**
 * §11 delivery rail — frontend half (2026-08-08).
 *
 * The backend refuses to deliver unverified drafts (409 awaiting_verification).
 * Pinned here: the UI cooperates instead of surfacing raw 409s —
 *  1. Detail view: Send and Mark-as-Mailed run ensureVerifiedForDelivery,
 *     which offers verify-and-continue in one motion (single-invoice review
 *     stays one click; only BULK verify is deliberately impossible).
 *  2. Billing list: bulk send partitions out unverified drafts with a toast
 *     before confirming, so a Draft-filter sweep can't attempt a mass send.
 *  3. Billing list: the Copy-pay-link button hides on unverified drafts —
 *     the /pay page refuses drafts, so the link would be dead.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const DETAIL = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');
const BILLING = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');

describe('§11 delivery rail — InvoiceDetailView', () => {
  it('send and mark-as-mailed gate on ensureVerifiedForDelivery', () => {
    const fn = DETAIL.indexOf('async function ensureVerifiedForDelivery');
    expect(fn).toBeGreaterThan(-1);
    const span = DETAIL.slice(fn, fn + 2200);
    expect(span).toMatch(/confirmAsync\(/);
    // Matches the URL alone, not the whole call expression. The old pattern
    // pinned `api.post(\`…/verify\`, {})` on ONE line and went red the day the
    // call was reformatted to pass an options argument — a cosmetic edit
    // failing a test that was meant to guard the delivery rail. Behaviour for
    // this handler is asserted for real in InvoiceDetailView.spec.js
    // ("Send also surfaces the gate"), which mounts the component and clicks.
    expect(span).toMatch(/\/api\/invoices\/\$\{invoice\.value\.id\}\/verify/);
    expect(DETAIL).toMatch(/async function sendInvoice\(\)[\s\S]{0,600}?ensureVerifiedForDelivery\(\)/);
    expect(DETAIL).toMatch(/async function markAsMailed\(\)[\s\S]{0,200}?ensureVerifiedForDelivery\(\)/);
  });
});

describe('overpayment visibility — InvoiceDetailView (M11)', () => {
  it('renders the overpaid banner off amount_overpaid', () => {
    expect(DETAIL).toMatch(/invoice\.amount_overpaid > 0/);
    expect(DETAIL).toMatch(/data-testid="overpaid-banner"/);
    expect(DETAIL).toMatch(/amount_overpaid: Number\(payload\.amount_overpaid\) \|\| 0/);
  });
});

describe('typed $0 lines survive create — InvoiceCreateView', () => {
  it('the submit filter keeps described $0 lines (server policy decides)', () => {
    const CREATE = readFileSync(join(__dirname, '..', 'InvoiceCreateView.vue'), 'utf8');
    const idx = CREATE.indexOf('const lineItems = form.value.line_items');
    const span = CREATE.slice(idx, idx + 200);
    expect(span).toMatch(/toNum\(l\.unit_price\) >= 0/);
  });
});

describe('machine provenance — InvoiceDetailView', () => {
  it('autodraft invoices carry the auto-drafted tag', () => {
    expect(DETAIL).toMatch(/invoice\.origin === 'closeout_autodraft'/);
    expect(DETAIL).toMatch(/data-testid="invoice-autodraft-tag"/);
    expect(DETAIL).toMatch(/origin: payload\.origin \|\| null/);
  });
});

describe('§11 delivery rail — BillingView', () => {
  it('bulk send partitions out unverified drafts before confirming', () => {
    const fn = BILLING.indexOf('async function bulkSend');
    const span = BILLING.slice(fn, fn + 1400);
    expect(span).toMatch(/skipped = selectedInvoices\.value\.filter/);
    expect(span).toMatch(/!inv\.verified_at/);
    expect(span).toMatch(/unverified draft\(s\) skipped/);
    expect(span).toMatch(/for \(const inv of sendable\)/);
  });

  it('pay-link button hides on unverified drafts', () => {
    const idx = BILLING.indexOf("aria-label=\"Copy pay link\"");
    const before = BILLING.slice(Math.max(0, idx - 400), idx);
    expect(before).toMatch(/'draft' && !data\.verified_at/);
  });
});
