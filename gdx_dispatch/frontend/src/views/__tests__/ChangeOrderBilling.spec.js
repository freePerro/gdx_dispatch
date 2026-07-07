/**
 * Change orders reach the invoice (PR3-billing-capture 2026-07-07).
 *
 * Approved COs were captured, signed, then orphaned — no path to an invoice.
 * Pinned here:
 *  1. InvoiceCreateView renders the unbilled-CO checklist for the picked job
 *     and sends from_change_order_ids on create.
 *  2. Job change resets the CO selection (different job = different COs).
 *  3. ChangeOrdersView's dialog shows Subtotal + the tax note (Doug
 *     2026-07-07: COs handled like invoices, tax shown to the customer).
 *  4. MobileChangeOrderDialog tells the tech tax rides on top.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const CREATE_SRC = readFileSync(join(__dirname, '..', 'InvoiceCreateView.vue'), 'utf8');
const CO_VIEW_SRC = readFileSync(join(__dirname, '..', 'ChangeOrdersView.vue'), 'utf8');
const MOBILE_SRC = readFileSync(
  join(__dirname, '..', '..', 'components', 'MobileChangeOrderDialog.vue'),
  'utf8',
);

describe('InvoiceCreateView — change-order checklist', () => {
  it('renders the unbilled-CO checklist section', () => {
    expect(CREATE_SRC).toMatch(/data-testid="invoice-co-checklist"/);
    expect(CREATE_SRC).toMatch(/Approved change orders on this job/);
  });

  it('fetches unbilled COs for the picked job', () => {
    expect(CREATE_SRC).toMatch(/\/api\/change-orders\?job_id=\$\{encodeURIComponent\(jobId\)\}&unbilled=true/);
  });

  it('sends from_change_order_ids in the create payload', () => {
    const payloadIdx = CREATE_SRC.indexOf('from_part_ids: form.value.from_part_ids');
    expect(payloadIdx).toBeGreaterThan(-1);
    const span = CREATE_SRC.slice(payloadIdx, payloadIdx + 200);
    expect(span).toMatch(/from_change_order_ids: form\.value\.from_change_order_ids/);
  });

  it('job change resets the CO selection and reloads the checklist', () => {
    const fnIdx = CREATE_SRC.indexOf('function onJobChange');
    expect(fnIdx).toBeGreaterThan(-1);
    const span = CREATE_SRC.slice(fnIdx, fnIdx + 600);
    expect(span).toMatch(/from_change_order_ids = \[\]/);
    expect(span).toMatch(/loadJobChangeOrders\(/);
  });
});

describe('ChangeOrdersView — tax shown like an invoice', () => {
  it('dialog shows Subtotal (not a tax-less Total) plus the tax note', () => {
    expect(CO_VIEW_SRC).toMatch(/data-testid="co-subtotal"/);
    expect(CO_VIEW_SRC).toMatch(/data-testid="co-tax-note"/);
    expect(CO_VIEW_SRC).toMatch(/applicable tax/);
  });

  it('RENDERS the server-computed tax + tax-inclusive total (audit round 2: a tax the API returns but no view shows is theater)', () => {
    expect(CO_VIEW_SRC).toMatch(/data-testid="co-tax-amount"/);
    expect(CO_VIEW_SRC).toMatch(/data-testid="co-total"/);
    expect(CO_VIEW_SRC).toMatch(/serverTotals\.value = detail/);
    // openEdit fetches the detail endpoint that carries the totals.
    const idx = CO_VIEW_SRC.indexOf('function openEdit');
    const span = CO_VIEW_SRC.slice(idx, idx + 700);
    expect(span).toMatch(/api\.get\(`\/api\/change-orders\/\$\{co\.id\}`\)/);
  });
});

describe('MobileChangeOrderDialog — tech sees the tax expectation', () => {
  it('amount hint says tax is added on top', () => {
    expect(MOBILE_SRC).toMatch(/tax-inclusive total/);
  });
});
