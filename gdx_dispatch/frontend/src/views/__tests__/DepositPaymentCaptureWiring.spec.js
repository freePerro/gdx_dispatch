/**
 * Deposit payment capture — wiring pins across every accept surface (2026-08-13).
 *
 * Doug: "a customer hands a form of payment for the downpayment, so you go back
 * to the office and hit Accept on the estimate and it asks for a downpayment —
 * but it only creates the invoice and a link to pay. No way to record a payment
 * that has already been received."
 *
 * Source-pin style, matching the house convention for these views (EstimateView
 * and MobileEstimatesView have no mount harness). The form's own behaviour —
 * the cash confirmation, the payload, the tenant-zone date — is mount-tested in
 * components/__tests__/PaymentCaptureForm.spec.js. These pins hold the wiring
 * that connects it, and the ORDERING rules that make it safe.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const read = (...p) => readFileSync(join(__dirname, '..', ...p), 'utf8');
const ESTIMATE = read('EstimateView.vue');
const MOBILE_EST = read('MobileEstimatesView.vue');
const MOBILE_BILL = read('MobileBillingView.vue');
const PORTAL = read('CustomerPortalView.vue');
const QUOTE_DIALOG = read('..', 'components', 'MobileCustomerQuoteDialog.vue');
const ROUTER = read('..', 'router', 'index.js');

describe('EstimateView — the reported case: office accept dialog', () => {
  it('offers "already paid" beside the deposit amount, inside the ACCEPT dialog', () => {
    const dlg = ESTIMATE.indexOf('data-testid="accept-dialog"');
    expect(dlg).toBeGreaterThan(-1);
    const dlgEnd = ESTIMATE.indexOf('</Dialog>', dlg);
    const body = ESTIMATE.slice(dlg, dlgEnd);
    expect(body).toMatch(/data-testid="deposit-already-paid-toggle"/);
    expect(body).toMatch(/<PaymentCaptureForm/);
  });

  it('collects (and cash-confirms) BEFORE posting the accept', () => {
    // Ordering is the safety property: backing out of the confirmation must
    // leave no accepted estimate and no deposit invoice behind.
    const collectIdx = ESTIMATE.indexOf('acceptPayForm.value.collect()');
    const acceptPost = ESTIMATE.indexOf('/accept`, body)');
    expect(collectIdx).toBeGreaterThan(-1);
    expect(acceptPost).toBeGreaterThan(-1);
    expect(collectIdx).toBeLessThan(acceptPost);
  });

  it('bails out entirely when the operator cancels the confirmation', () => {
    expect(ESTIMATE).toMatch(/paymentPayload = await acceptPayForm\.value\.collect\(\);\s*\n\s*if \(!paymentPayload\) return;/);
  });

  it('posts the payment against the invoice id the accept returned', () => {
    expect(ESTIMATE).toMatch(/recordDepositPayment\(result\.deposit\.invoice_id, paymentPayload\)/);
    expect(ESTIMATE).toMatch(/api\.post\(`\/api\/invoices\/\$\{invoiceId\}\/payments`, payload\)/);
  });

  it('shouts when a confirmed payment has no deposit invoice to land on', () => {
    // deposit_skipped (no customer, bad amount) means result.deposit is null.
    // The operator has already confirmed handing over cash — silently dropping
    // it would leave them believing it was recorded.
    expect(ESTIMATE).toMatch(/summary: "Payment NOT recorded"/);
  });

  it('refuses to post more than the returned deposit actually owes', () => {
    // create_deposit_invoice is idempotent: a race or the retroactive path can
    // return a PRE-EXISTING, part-paid deposit. The form capped at the amount
    // the operator typed, which is not the same number.
    const fn = ESTIMATE.slice(ESTIMATE.indexOf('async function recordDepositPayment'));
    const head = fn.slice(0, fn.indexOf('try {'));
    expect(head).toMatch(/depositResult\.value\?\.balance_due/);
    expect(head).toMatch(/payload\.amount > owed/);
  });

  it('never unwinds the acceptance when the payment fails', () => {
    // The catch must report and stop — no re-POST, no status rollback.
    const fn = ESTIMATE.slice(ESTIMATE.indexOf('async function recordDepositPayment'));
    const body = fn.slice(0, fn.indexOf('\n}\n'));
    expect(body).toMatch(/Deposit created — payment NOT recorded/);
    expect(body).not.toMatch(/estimate\.value\.status\s*=/);
  });

  it('hides the pay link once the deposit owes nothing', () => {
    // Offering "send them a link to pay" for a check already in hand is the
    // wrong-state UI that produced this report.
    expect(ESTIMATE).toMatch(/depositResult\?\.pay_url && depositResultOwes/);
    expect(ESTIMATE).toMatch(/const depositResultOwes = computed/);
  });

  it('resets the toggle every time either dialog opens', () => {
    // Both dialogs share the ref; a toggle left on would re-arm a payment form
    // for a different estimate.
    const accept = ESTIMATE.slice(ESTIMATE.indexOf('async function acceptEstimate'));
    expect(accept.slice(0, 400)).toMatch(/depositAlreadyPaid\.value = false/);
    const req = ESTIMATE.slice(ESTIMATE.indexOf('async function openRequestDeposit'));
    expect(req.slice(0, 200)).toMatch(/depositAlreadyPaid\.value = false/);
  });

  it('offers the same capture on the retroactive Request Deposit path', () => {
    const dlg = ESTIMATE.indexOf('data-testid="request-deposit-dialog"');
    const dlgEnd = ESTIMATE.indexOf('</Dialog>', dlg);
    expect(ESTIMATE.slice(dlg, dlgEnd)).toMatch(/data-testid="request-deposit-already-paid-toggle"/);
    expect(ESTIMATE).toMatch(/requestPayForm\.value\.collect\(\)/);
  });
});

describe('MobileEstimatesView — tech accepts in the field', () => {
  it('renders the capture form on the deposit result', () => {
    expect(MOBILE_EST).toMatch(/<PaymentCaptureForm/);
    expect(MOBILE_EST).toMatch(/data-test="me-deposit-pay-open"/);
  });

  it('queues the payment so a dead-signal driveway cannot lose it', () => {
    expect(MOBILE_EST).toMatch(/api\.postQueued\(`\/api\/invoices\/\$\{inv\.invoice_id\}\/payments`/);
    expect(MOBILE_EST).toMatch(/actionType: 'invoice\.payment'/);
    // A payments 409 is a business refusal the tech must see, not a dedup
    // verdict to file as synced.
    expect(MOBILE_EST).toMatch(/conflictIsError: true/);
  });

  it('only offers cash/check on mobile', () => {
    expect(MOBILE_EST).toMatch(/:methods="\['Cash', 'Check'\]"/);
  });

  it('stops offering the pay link once the deposit is settled', () => {
    expect(MOBILE_EST).toMatch(/const depositOwes = computed/);
    expect(MOBILE_EST).toMatch(/depositResult\.pay_url && depositOwes/);
  });
});

describe('MobileCustomerQuoteDialog — customer signs, tech collects', () => {
  it('labels the cash/check half as the TECH step', () => {
    // A customer must never be able to attest their own cash payment.
    expect(QUOTE_DIALOG).toMatch(/data-testid="mobile-deposit-tech-pay"/);
    expect(QUOTE_DIALOG).toMatch(/Tech — did they pay you cash or a check\?/);
  });

  it('keeps the customer card path and the tech path separate', () => {
    expect(QUOTE_DIALOG).toMatch(/data-testid="mobile-deposit-pay"/);
    expect(QUOTE_DIALOG).toMatch(/data-testid="mobile-deposit-pay-form"/);
  });

  it('queues the payment like every other field surface', () => {
    expect(QUOTE_DIALOG).toMatch(/api\.postQueued\(`\/api\/invoices\/\$\{d\.invoice_id\}\/payments`/);
    expect(QUOTE_DIALOG).toMatch(/conflictIsError: true/);
  });
});

describe('MobileBillingView — the field safety net', () => {
  it('reads the tech-scoped endpoint when the caller has no invoices permission', () => {
    // A technician has NO invoices permission, so /api/invoices 403s — this
    // screen used to be unreachable for its only intended user.
    expect(MOBILE_BILL).toMatch(/const officeTier = computed\(\(\) => hasPermission\('invoices\.read_all'\)\)/);
    expect(MOBILE_BILL).toMatch(/api\.get\('\/api\/mobile\/invoices\/open'\)/);
  });

  it('does not ask for the office KPI summary as a technician', () => {
    const fn = MOBILE_BILL.slice(MOBILE_BILL.indexOf('async function loadBillingSummary'));
    expect(fn.slice(0, 300)).toMatch(/if \(!officeTier\.value\)/);
  });

  it('surfaces a business 409 instead of filing it as synced', () => {
    // Without conflictIsError the offline queue treats a 409 as "synced" — a
    // REFUSED payment would show success for money the server rejected. Pinned
    // on the other two surfaces already; this one was missing it.
    const fn = MOBILE_BILL.slice(MOBILE_BILL.indexOf('async function recordPayment'));
    const body = fn.slice(0, fn.indexOf('\n}\n'));
    expect(body).toMatch(/conflictIsError: true/);
    // ...and a duplicate is reported as already-recorded, not as a failure.
    expect(body).toMatch(/duplicate_payment/);
  });

  it('no longer stamps the UTC day on a payment', () => {
    // Central is behind UTC: the old `toISOString().slice(0,10)` booked an
    // evening payment on tomorrow, and at month-end into the wrong month. The
    // date now comes from the shared form's tenant-zone helper.
    //
    // Scoped to the payment path on purpose — an unrelated month-boundary KPI
    // fallback further down legitimately uses toISOString, and asserting over
    // the whole file would fail on it.
    const fn = MOBILE_BILL.slice(MOBILE_BILL.indexOf('async function recordPayment'));
    const body = fn.slice(0, fn.indexOf('\n}\n'));
    expect(body).not.toMatch(/toISOString/);
    expect(body).toMatch(/api\.postQueued/);
    expect(MOBILE_BILL).toMatch(/<PaymentCaptureForm/);
  });

  it('is no longer route-gated on a permission technicians do not have', () => {
    const line = ROUTER.split('\n').find((l) => l.includes("name: 'mobile-billing'"));
    expect(line).toBeTruthy();
    expect(line).not.toMatch(/invoices\.read_all/);
  });

  it('resolves permissions BEFORE the first fetch', () => {
    // Dropping requiresPermission also dropped the router guard's
    // `await loadPermissions()`. Without replacing it here, hasPermission()
    // answers false for everyone on a cold load — so an office user is served
    // the technician list, gets [], and stares at $0.00 with no re-fetch.
    const mounted = MOBILE_BILL.slice(MOBILE_BILL.indexOf('onMounted('));
    const body = mounted.slice(0, mounted.indexOf('})'));
    expect(body).toMatch(/await ensurePermissions\(\)/);
    const awaitIdx = body.indexOf('await ensurePermissions()');
    expect(awaitIdx).toBeLessThan(body.indexOf('fetchInvoices()'));
    expect(awaitIdx).toBeLessThan(body.indexOf('loadBillingSummary()'));
  });
});

describe('CustomerPortalView — honest about checks, records nothing', () => {
  it('tells the customer how to pay by check without pretending to record it', () => {
    expect(PORTAL).toMatch(/data-testid="deposit-pay-by-check"/);
    expect(PORTAL).toMatch(/We'll mark it paid when it arrives/);
  });

  it('offers no payment-capture form to the customer', () => {
    // The portal is the one surface that must NEVER be able to record money.
    expect(PORTAL).not.toMatch(/PaymentCaptureForm/);
  });
});
