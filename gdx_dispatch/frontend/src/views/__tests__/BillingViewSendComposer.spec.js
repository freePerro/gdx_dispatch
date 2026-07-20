/**
 * BillingView — row Send goes through the composer (2026-07-20).
 *
 * The per-row Send used to fire POST /api/invoices/{id}/send immediately: a
 * server-rendered email left the building with no preview, no recipient
 * check, and (worse) the catch-block "fallback" just PATCHed the status to
 * Sent — an invoice could be marked Sent when NO email ever went out.
 *
 * Pinned:
 *  1. sendInvoice navigates to the invoice detail with ?compose=1 (the detail
 *     view auto-opens its composer: recipient + message + PDF preview, and
 *     nothing sends until the explicit Send click).
 *  2. sendInvoice no longer POSTs or PATCHes anything itself.
 *  3. Bulk send keeps the server-side path (it has its own confirm dialog);
 *     this pin documents that as a deliberate carve-out, not an oversight.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');

describe('BillingView — row Send opens the composer flow', () => {
  it('sendInvoice routes to the detail composer instead of firing the email', () => {
    const start = SRC.indexOf('function sendInvoice');
    expect(start).toBeGreaterThan(-1);
    const body = SRC.slice(start, SRC.indexOf('\n}', start));
    expect(body).toMatch(/router\.push\(`\/billing\/\$\{inv\.id\}\?compose=1`\)/);
  });

  it('sendInvoice no longer POSTs /send or optimistically PATCHes status', () => {
    const start = SRC.indexOf('function sendInvoice');
    const body = SRC.slice(start, SRC.indexOf('\n}', start));
    expect(body).not.toMatch(/api\.post/);
    expect(body).not.toMatch(/api\.patch/);
  });

  it('bulk send keeps the server-side path but reports delivery honestly', () => {
    expect(SRC).toMatch(/async function bulkSend/);
    const start = SRC.indexOf('async function bulkSend');
    const end = SRC.indexOf('async function bulkMarkPaid', start);
    const body = SRC.slice(start, end);
    expect(body).toMatch(/api\.post\(`\/api\/invoices\/\$\{inv\.id\}\/send`/);
    // Audit catch 2026-07-20: a send failure used to PATCH status to Sent and
    // count as success — an invoice could read Sent when NO email went out.
    expect(body).not.toMatch(/api\.patch/);
    // A 200 with email_sent=false is a non-delivery, not a success.
    expect(body).toMatch(/email_sent === false/);
  });
});
