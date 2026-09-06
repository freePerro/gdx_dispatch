# Received vs deposited: money we hold but have not banked

**Status:** ⛔ **SUPERSEDED, SAME DAY, BY
`undeposited-funds-clearing-plan.md` — do not build this.**

Kept because how it was wrong is the useful part. This plan proposed *adding* a
`Dr 1050 Undeposited Funds / Cr AR` receipt posting as if it were new. It is
**already the live behaviour** (`modules/ledger/service.py:43-50` maps
cash/check/card/other to `ROLE_UNDEPOSITED`). The real missing piece is the
opposite leg — nothing ever credits 1050 — and this plan's "Backfill: None,
forward-only" would have **stranded the $80,291.99 already sitting there**.

Both errors have the same shape as the one this plan itself documents below:
asserting a mechanism without naming the code that serves it. That is twice on
one branch. An adversarial audit caught it before any of it was built.

The one idea worth carrying forward: the banked date should come **from the
bank line at match time**, not be typed by an operator — which removes the need
for the nullable `payment_date` this plan was built around.
**Decided (Doug, 2026-08-22):** the ledger does not claim cash in the bank
before it is in the bank; the split is **forward-only**, no re-dating of
existing entries; and **"it really only matters on year change"** — that
sentence is the scope control for this whole document.

## Why this matters, and exactly how much

A cheque handed to a tech on the 3rd and banked on the 8th is one economic
event with two dates. Within a month the five-day gap washes out of every
report anyone reads. **Across a period boundary it does not**, and across a
**year** boundary it moves revenue between tax years — which is the only place
the imprecision has ever cost anything.

So this plan is not "model payments more richly". It is: *make the period
boundary correct, and change as little else as possible.* Anywhere a choice
arises between elegance and a smaller diff, take the smaller diff.

## What already exists (do not rebuild)

| Piece | State |
|---|---|
| **`1050 Undeposited Funds`** | **Already in the CoA and live on prod.** `coa.py:49`, `ROLE_UNDEPOSITED`. This is the account the whole design hangs on and it is already there. |
| `1000` Operating Bank (+ `1010`, `1011` on prod) | live |
| GL posting engine, period locks, reversal | live — `modules/ledger/rules.py`, `gl_period_locks` |
| `payments.received_at` | ⛔ **never merged** — drafted on an abandoned branch; prod is on alembic 072 and the column does not exist |
| Bank statement matcher | live — pairs statement lines to payments within ±3 business days (`R2_BUSINESS_DAYS`) |
| Payment void | live — `POST /{invoice_id}/payments/{payment_id}/void` |

## What already went wrong (read this before designing anything)

The first attempt seeded `payment_date` from the received date at capture and
relied on "the office corrects it later through the #249 picker."

**There is no such picker.** No `PATCH`/`PUT` exists on a payment — void and
re-record is the only correction path — and
`payment-date-recording-plan.md` lists *"a dedicated edit-payment-date
feature"* under **non-goals**. An adversarial audit caught it before commit.
The consequence would have been permanent: every field-captured cheque posting
to the GL on the day it was taken, and falling outside the matcher's ±3-day
window. Worse than the bug it fixed.

Two things follow, and they are requirements, not preferences:

1. **A deposit action is mandatory.** Without a way to record the banking, a
   received payment never reaches `1000` and sits in `1050` forever. This is
   the endpoint the earlier plan called a non-goal; splitting the dates is the
   "revisit if it becomes painful" condition that plan named.
2. **Every claim about a correction path must name the endpoint that serves
   it.** That is what failed.

## The model

Two postings for one payment, which is ordinary double-entry, not an invention:

| Event | Date used | Posting |
|---|---|---|
| Payment received | `received_at` | **Dr 1050 Undeposited Funds** / Cr 1200 AR |
| Payment deposited | `payment_date` (banked) | **Dr 1000 Operating Bank** / Cr 1050 Undeposited Funds |

Consequences worth stating plainly:

- **AR clears on receipt.** The customer handed over money; they must not be
  dunned. `balance_due` already sums live payments regardless of date and keeps
  doing so — **no change to balance or dunning.**
- **The bank account only moves on a real deposit**, so the statement tie-out
  stays anchored to evidence.
- `1050` is the honest carrying value of "cheques in the truck".
- **Correction to an earlier framing:** the GL *does* post on receipt. What
  posts on the banked date is the **bank** leg. "GL posts on banked" was true
  of `1000` and is preserved.

## Scope, cut against the year-change rule

**In scope**

1. `payment_date` becomes **nullable** — an unbanked payment genuinely has no
   banked date, and today's NOT NULL forces a lie. 49 references; ~8
   dereference it directly and would `AttributeError`. Each gets an explicit
   NULL branch; **none may coalesce to `received_at`.**
2. A **deposit action** — `POST /api/invoices/{id}/payments/{pid}/deposit`
   taking the banked date, audited, money-permissioned, idempotent, refusing a
   date before `received_at` and refusing a locked period (409, per the
   existing `reverse_entry` rule).
3. **Two posting rules** + the reversal paths (§ Void).
4. **Matcher**: pair statement lines against the deposit, not the receipt.
5. **Period-boundary tests as the centre of gravity** — received Dec 30 /
   banked Jan 3 is the headline case, plus month boundaries against a period
   lock.

**Out of scope, deliberately**

- Re-dating the 188 posted entries. **Forward-only (Doug).** Existing payments
  are all already banked; they post exactly as they do today.
- Batch/deposit-slip grouping (several cheques, one bank line). Real, and the
  matcher's R3 sweep already contemplates it — but not needed for the year-end
  correctness this plan exists for.
- Any change to `balance_due`, dunning, or invoice status.

## Backfill

**None.** All 340 existing payments are banked; `payment_date` stays set and
`received_at` stays NULL. NULL means "not captured" and must never be
back-filled from the banked date — that manufactures evidence. New payments
recorded before the deposit action ships would strand in `1050`, so the deposit
action ships **in the same release** as the nullable column, not after.

## Void semantics

| State when voided | Required behaviour |
|---|---|
| Received, not deposited | Reverse the `1050`/AR entry only |
| Received and deposited | Reverse both legs; the bank leg reverses at its own date |
| Either, in a locked period | Reversal posts at the target date per `reverse_entry`'s escape hatch — locked amounts are countered in the open period, never edited |

## Rollback

Drop `received_at`; restore `payment_date` NOT NULL (requires every row to have
one — true if the deposit action has been run, which is why it ships together).
The second posting rule is flag-checkable and can be disabled independently,
leaving today's single-posting behaviour.

## Open — for the CPA, not for me

1. Is `1050` the account they expect, and does its balance need to appear on
   the year-end package as a named reconciling item?
2. A cheque received in December and banked in January: this plan recognises
   revenue in **December** (AR clears then). Confirm that is the intended tax
   treatment — it is the entire reason the split exists, and it is an
   accounting decision.
3. Does an uncleared cheque outstanding at year end need a separate disclosure?
