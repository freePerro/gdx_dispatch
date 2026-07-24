/**
 * MobileInvoiceDialog — deposit netting + final-invoice gate (2026-07-24).
 *
 * Gap audit: the truck never saw the netting result, and ANY invoice
 * (including a deposit) hid the Generate button so a deposit-taking job
 * couldn't be final-billed from the field. Pinned:
 *  1. The Generate button gates on hasFinalInvoice (deposit invoices are
 *     money BEFORE the work, not "the job is billed").
 *  2. The netting toast covers applied / superseded / voided and WARNS on
 *     the unapplied case (deposit exceeds final — a human must resolve it).
 *  3. Deposit invoices carry a visible tag in the invoice list.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'MobileInvoiceDialog.vue'),
  'utf8',
);

describe('MobileInvoiceDialog — deposit awareness', () => {
  it('Generate button gates on a FINAL invoice, not any invoice', () => {
    expect(SRC).toMatch(/v-if="!hasFinalInvoice"/);
    expect(SRC).toMatch(/billing_type\s*\|\|\s*'standard'\)\s*!==\s*'deposit'/);
  });

  it('netting toast covers applied, superseded, voided and unapplied', () => {
    expect(SRC).toMatch(/deposit_netting/);
    expect(SRC).toMatch(/deposit_paid_applied/);
    expect(SRC).toMatch(/superseded/);
    expect(SRC).toMatch(/voided unpaid/);
    expect(SRC).toMatch(/deposit_unapplied/);
    // The unapplied case must escalate severity.
    expect(SRC).toMatch(/unapplied \? 'warn' : 'info'/);
  });

  it('deposit invoices carry a tag in the list', () => {
    expect(SRC).toMatch(/data-testid="mid-deposit-tag"/);
  });
});
