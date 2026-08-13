/**
 * Payment date recording (2026-07-30 plan) — source contracts across the
 * four payment-entry surfaces.
 *
 * Pins:
 *  1. Both desktop Record Payment dialogs and bulk Mark Paid expose a date
 *     input capped at today (backdating allowed — 2025 corrections are the
 *     point; forward-dating is not).
 *  2. Every payment submit path sends the picked date; NO payment path
 *     stamps a UTC-sliced "today" anymore (toISOString().slice dates
 *     evening payments tomorrow — the off-by-one-day family).
 *  3. Defaults come from the tenant-zone day (zonedDateKey), not UTC.
 *  4. Mobile keeps no picker but stamps the tenant-zone capture day and
 *     flags its queue writes conflictIsError so payment 409 refusals
 *     surface instead of reading as "recorded".
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const read = (rel) => readFileSync(join(__dirname, '..', '..', rel), 'utf8');

const BILLING = read('views/BillingView.vue');
const DETAIL = read('views/InvoiceDetailView.vue');
const PAYMENTS = read('views/PaymentsView.vue');
const MOBILE = read('components/MobileInvoiceDialog.vue');

describe('BillingView — Record Payment dialog', () => {
  it('has a date input capped at today, defaulted on open', () => {
    expect(BILLING).toMatch(/data-testid="payment-date"/);
    const tag = BILLING.slice(
      BILLING.lastIndexOf('<InputText', BILLING.indexOf('data-testid="payment-date"')),
      BILLING.indexOf('data-testid="payment-date"'),
    );
    expect(tag).toMatch(/type="date"/);
    expect(tag).toMatch(/:max="todayKey\(\)"/);
    // The date default is what this test guards; the amount default moved
    // from a hard 0 to the invoice's balance_due (2026-08-13) because making
    // the operator retype a known number is how a deposit gets under-recorded.
    expect(BILLING).toMatch(/date: todayKey\(\),/);
    expect(BILLING).toMatch(/amount: balance > 0 \? balance : 0,/);
  });

  it('sends the picked date and requires it', () => {
    expect(BILLING).toMatch(/date: newPayment\.value\.date \|\| todayKey\(\)/);
    expect(BILLING).toMatch(/:disabled="!newPayment\.amount \|\| !newPayment\.method \|\| !newPayment\.date"/);
  });
});

describe('BillingView — bulk Mark Paid', () => {
  it('has one batch date input, seeded to today on open', () => {
    expect(BILLING).toMatch(/data-testid="bulk-paid-date"/);
    expect(BILLING).toMatch(/bulkPaidDate\.value = todayKey\(\)/);
    expect(BILLING).toMatch(/:disabled="!bulkPaidMethod \|\| !bulkPaidDate"/);
  });

  it('applies the batch date to every POST', () => {
    expect(BILLING).toMatch(/const payDay = bulkPaidDate\.value \|\| todayKey\(\)/);
    expect(BILLING).toMatch(/date: payDay/);
  });
});

describe('InvoiceDetailView — Record Payment dialog', () => {
  it('has a date input capped at today, defaulted via openPaymentDialog', () => {
    expect(DETAIL).toMatch(/data-testid="payment-date"/);
    expect(DETAIL).toMatch(/@click="openPaymentDialog"/);
    expect(DETAIL).toMatch(/date: todayKey\(\),/);
    expect(DETAIL).toMatch(/amount: balance > 0 \? balance : 0,/);
  });

  it('sends the picked date and requires it', () => {
    expect(DETAIL).toMatch(/date: newPayment\.value\.date \|\| todayKey\(\)/);
    expect(DETAIL).toMatch(/:disabled="!newPayment\.amount \|\| !newPayment\.method \|\| !newPayment\.date"/);
  });
});

describe('PaymentsView — existing picker', () => {
  it('defaults to the tenant-zone day and caps at today', () => {
    expect(PAYMENTS).toMatch(/date: todayKey\(\)/);
    expect(PAYMENTS).toMatch(/:max="todayKey\(\)"/);
    expect(PAYMENTS).not.toMatch(/date: new Date\(\)\.toISOString\(\)/);
  });
});

describe('MobileInvoiceDialog — capture-day stamp + 409 surfacing', () => {
  it('stamps the tenant-zone day, not a UTC slice', () => {
    expect(MOBILE).toMatch(/date: zonedDateKey\(new Date\(\)\)/);
    expect(MOBILE).not.toMatch(/toISOString\(\)\.slice\(0, 10\)/);
  });

  it('flags its payment queue writes conflictIsError', () => {
    expect(MOBILE).toMatch(/conflictIsError: true/);
  });
});

describe('no payment path UTC-slices "today" anymore', () => {
  it.each([
    ['BillingView', BILLING],
    ['InvoiceDetailView', DETAIL],
    ['PaymentsView', PAYMENTS],
  ])('%s has no toISOString date stamp in a payment payload', (_name, src) => {
    // The CSV-filename and KPI-window usages are allowed; payment payloads
    // (a `date:` key fed to POST /payments) must not UTC-slice.
    const payloadStamp = /date:\s*new Date\(\)\.toISOString\(\)/;
    expect(src).not.toMatch(payloadStamp);
  });
});
