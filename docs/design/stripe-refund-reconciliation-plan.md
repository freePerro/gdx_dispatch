# Stripe Refund Reconciliation — keying refunds so they can be recorded once

**Status:** **PLAN** — not built. Filed 2026-08-23 as the deferred half of
money-audit finding **M3**, whose void half shipped the same day. Nothing here
is scheduled; it becomes worth doing when card refunds stop being hypothetical
(3 card payments totalling $1,021 on prod, zero refunds ever).

## Why this is a separate piece of work

M3's headline bug — a partial Stripe refund voided the entire payment — is
fixed. What is *not* fixed is booking the refunded money automatically.

That was implemented first and then removed, because an adversarial review
demonstrated two ways it double-books, and neither can be closed without a
schema change:

1. **Cumulative amounts collide with the void branch.** Stripe's
   `amount_refunded` is cumulative across every refund on a charge. A $50
   partial followed by a full refund arrives as `amount_refunded == amount`, so
   it takes the full-void path — which knows nothing about the $50 adjustment
   already written. Net paid goes **negative** ($550 reversed on a $500 charge),
   and `POST /api/invoices/{id}/refund` then 422s on that invoice forever
   because its cap reads net paid.
2. **The office records refunds too.** Refunding in the Stripe dashboard and
   recording it in the app is the normal workflow. Nothing links the manual row
   to the webhook's row, so $50 returned is booked as **$100** — under the cap,
   with no warning. The screen where an operator would notice
   (`PaymentsView.vue`) lists `Payment` rows only; `routers/payments.py` never
   reads `InvoiceAdjustment`, so the webhook's row is invisible there.

Both reduce to the same missing thing: **refunds have no external identity.**
Deduping on a substring of the free-text `reason` — which is what the first
implementation did — is not a key. It also breaks on `LIKE` metacharacters:
every Stripe id contains `_`, which is a single-character wildcard.

## What already exists (do not rebuild)

| Piece | Where | State |
|---|---|---|
| Refund as a first-class record | `InvoiceAdjustment(kind='refund')`, `models/tenant_models.py:700` | Live. Append-only. `refund_method`, `created_by`, `reason`. |
| The money rule | `_recalculate_invoice`, `routers/invoices.py:538-551` | Live and correct: `balance = total − Σpayments − Σ(credit_memo + credit_applied)`. **Refunds deliberately excluded** — they are contra-revenue cash-outs, not receivables. |
| Office refund endpoint | `POST /api/invoices/{id}/refund`, `invoices.py:3480` | Live. Caps by `_net_paid` (payments − prior refunds), requires `refund_method` when ledger posting is on, posts 4910/4900 via `post_refund`, audits. |
| Ledger posting | `modules/ledger/rules.py:739 post_refund` | Live. Debits 4910, credits cash; never touches AR — a sales allowance, so the balance sheet still ties. |
| The webhook split | `core/payments.py _apply_charge_refund` | Live. Full → void; partial → leave the payment, log WARNING, write a `stripe_partial_refund_received` audit event. |

## What it would take

1. **A key.** `invoice_adjustments.external_ref` (String, nullable, indexed) plus
   a partial unique index `WHERE external_ref IS NOT NULL`, so recording the same
   Stripe refund twice is rejected by the database rather than by a `LIKE`.
   Store Stripe's **refund id** (`data.refunds.data[*].id`), not the
   PaymentIntent — one charge can carry several refunds. Migration must run on
   SQLite and Postgres; escape literal `%` as `%%`.
2. **Read the refund objects, not the running total.** `charge.refunded`
   carries `refunds.data[]`. Recording each refund id once makes the cumulative
   arithmetic unnecessary, and makes replay a no-op by construction.
3. **Teach the void branch about prior partials.** A full refund after a partial
   must not double-reverse: either void and retire the partial adjustments for
   that charge, or record the remaining delta and void nothing. Decide which,
   and say why in the code.
4. **Let the office see it.** `PaymentsView` shows `Payment` rows only, so a
   webhook-written refund is invisible where an operator would look for it.
   Surface refund adjustments alongside payments before automating the write,
   or the fix creates a second thing nobody can see.
5. **Reconciliation report.** `stripe_partial_refund_received` audit events with
   no matching adjustment are the backlog. Show it, so "we did not book it"
   cannot decay into "nobody knew".
6. **Revenue reporting.** `reports.py::_credits_by_period` nets **credit memos
   only**, so refunds do not reduce `/revenue-by-period` at all. Pre-existing for
   office refunds; automating the write would make it systematic. Decide whether
   a refund reduces revenue in that report before increasing the volume of them.

## Traps carried forward

- **`amount_refunded` is cumulative.** Treat it as a running total, never a
  delta. Prefer the refund objects.
- **Absent is not zero.** A payload without `amount_refunded` must fall back to
  the full-void path, not be read as a partial refund of nothing.
- **The cap is a signal, not a clamp.** Exceeding net paid means Stripe and the
  books disagree. Refuse and surface it; silently clamping money hides the
  disagreement.
- **A partial dispute has the same shape** and is deliberately NOT handled the
  same way — see **M15**. A dispute is provisional; splitting it before the
  `charge.dispute.closed` lifecycle exists just creates a second wrong state.
