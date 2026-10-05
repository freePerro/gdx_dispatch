/**
 * Billing — "Viewed" column (2026-10-02).
 *
 * Shows the date the customer last opened the view-and-pay link; blank
 * otherwise, because no record does not prove "not opened". Render proof
 * was a browser walk; this pins the plumbing the billing list has broken
 * before (normalizeInvoice eats any field it does not name — Last Sent and
 * the date filter were both blanked that way) and that the cell never
 * infers anything from sent_at.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const BILLING = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');

describe('BillingView — Viewed column', () => {
  it('normalizeInvoice carries both view fields', () => {
    const start = BILLING.indexOf('function normalizeInvoice');
    const span = BILLING.slice(start, BILLING.indexOf('\n  };', start));
    expect(span).toMatch(/customer_viewed_at:\s*raw\.customer_viewed_at \|\| null,/);
    expect(span).toMatch(/customer_view_count:\s*Number\(raw\.customer_view_count\) \|\| 0,/);
  });

  it('the cell makes no "not opened" claim', () => {
    const idx = BILLING.indexOf('header="Viewed"');
    expect(idx).toBeGreaterThan(-1);
    const tag = BILLING.slice(BILLING.lastIndexOf('<Column', idx), BILLING.indexOf('</Column>', idx));
    expect(tag).not.toMatch(/data\.sent_at/);
    expect(tag).not.toMatch(/[Nn]ot opened/);
  });

  it('both CSV exports carry the Viewed column', () => {
    const b = BILLING.indexOf('function bulkExport');
    expect(BILLING.slice(b, b + 700)).toMatch(/i\.customer_viewed_at/);
    const e = BILLING.indexOf('function exportInvoices');
    expect(BILLING.slice(e, e + 800)).toMatch(/field:\s*"customer_viewed_at",\s*header:\s*"Viewed"/);
  });
});
