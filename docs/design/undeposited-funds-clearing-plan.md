# The ledger's cash cycle is half-recorded — $80,291.99 stuck in 1050

**Status:** DIAGNOSIS + PLAN (2026-08-22). **Nothing here is built.** Three
drafts of this were killed by adversarial audits; every number below survived
the third one. Supersedes `payment-received-vs-deposited-plan.md`.

## The numbers (prod, read-only, 2026-08-22)

| | |
|---|---|
| **1050 Undeposited Funds** | **$80,291.99** — 22 entries, all `payment`, **never once credited** |
| GL bank cash (1000+1010+1011) | $5,882.15 |
| Real bank balances (3 mapped accounts) | $11,967.80 |
| Revenue / AR | $98,768.11 / $60,499.70 |
| Expenses entered as `expenses` | 6 / **$568.64** — and GL expense lines match **exactly** |
| **Vendor bills entered, never confirmed** | **11 / $45,385.74** (3 since July, $20,619.46) — **zero GL entries** |
| July bank debits, **real operating spend** | 38 / **$7,176.43** |
| July bank debits, transfers + owner draw | 9 / $15,000.00 |
| July bank debits, payroll / loan / financing | 11 / $12,900.96 |
| `bank_matches` | **0 rows** |

## The diagnosis

**Both sides of the cash cycle are half-recorded, in different ways.**

- **Inflows** post correctly to 1050 and are **never cleared to the bank** —
  nothing in the codebase credits 1050. The clearing leg was deferred
  (`bank-statement-import-plan.md:177`, slice 4) and never built.
- **Outflows** are partly *entered* and never *posted*: **$45,385.74 of vendor
  bills sit in `vendor_invoices` with zero GL entries**, because a bill only
  becomes an `Expense` — and only then posts — when it is **confirmed**, and
  none have been. The six `expenses` that do exist posted perfectly.

So the earlier framing "nothing was entered, the GL is starved" was **wrong for
the largest chunk of spend**. The spend is in GDX. It is sitting one
un-clicked confirm away from the ledger.

**And the direction matters, which an earlier draft never computed.** GL bank
($5,882.15) is **$6,085.65 BELOW** the real balance ($11,967.80). So:

- clearing 1050 first → GL bank ≈ $86,000 vs a real $11,968 (wildly high)
- posting outflows first → GL bank goes **negative**

**Neither single-sided sequence produces an honest balance sheet.** The two
legs have to land together, or close together, and the interim has to be
understood as interim.

## ⚠ The trap in the obvious next step

The natural move is to work July's bank debits through **create-expense** on
the Reconcile screen. **Do not do that first.** $45,385.74 of that spend is
already in GDX as vendor bills. Creating expenses from the bank debits *and*
later confirming those bills **double-counts the same money** — and unlike the
inflow side, there is no structural guard against it.

Correct order for the outflow leg: **confirm the vendor bills** (which creates
the Expense and posts it), *then* use create-expense only for debits that no
bill covers.

## What already exists (do not rebuild)

| Piece | State |
|---|---|
| Receipt leg `Dr 1050 / Cr 1200` | **LIVE** — `service.py:43-50` maps cash/check/card/other → `ROLE_UNDEPOSITED` |
| Vendor bill → Expense → GL | **BUILT** (`modules/vendor_invoices/confirm.py`, `post_expense_recorded`) — just never exercised |
| R3 deposit sweep (batched cheques) | **BUILT** — unique subset, n≤12, k≤6, ambiguity refused |
| `_apply_confirm_effects` | the seam where a confirm already mutates books |
| The clearing design itself | **specified** in `gl-phase2-reconciliation.md:104` |
| Statement evidence | 388 lines, Jan 2 – Jul 31 |

**Nothing credits 1050.** Zero consumers of `ROLE_UNDEPOSITED` in live code.

## Clearing design (build after the outflow leg exists)

On confirming a deposit match whose externals are payments, post at the
**statement line's date**, to the **bank account that line belongs to**
(`BankAccount.gl_account_id` is populated for all three):

    Dr 1000/1010/1011   Cr 1050

with these constraints, each from a specific failure found in review:

- **Amount = the sum of matched payments that actually hold a 1050 debit**, not
  the bank-line total. Payment `a59b9eca` ($345.21) posted `Dr 3950 / Cr 1200`
  and never touched 1050, yet is match-eligible; so are zelle/venmo/quickbooks,
  which map to `OPERATING_BANK`. A match containing any of them must refuse
  loudly, not silently over-credit.
- **Inert before the 2026-06-30 cutover lock.** The office's next real task is
  the Jan–Jun backlog (29 pre-cutover deposits); posting on confirm would make
  those 409 where they succeed today.
- **A locked period must refuse with a reason.** `reverse_entry` cannot be used
  here — it reverses an existing entry and cannot post a forward one. An
  earlier draft claimed otherwise.
- Idempotent per match; unconfirm reverses; no-op when `ledger_posting_enabled`
  is off.

## Evidence is smaller than the balance

July's 12 statement deposits ($41,268.44) include **$8,700 of internal
transfers**; August's 8 feed deposits ($36,412.47) include a $5,000 transfer
and rows from a **credit-card** account. Real customer deposits are materially
less than $80,291.99, so **the balance will not fully clear** — and August is
feed-only (`bank_feed_transactions`), which the matcher does not read, so
August cannot clear at all until its statement is imported.

The residue is the product, not a failure.

## Sequence

1. **Confirm the 11 vendor bills** → Expenses → they post. Biggest, safest win.
2. Work July's remaining debits via create-expense, skipping anything a bill
   covered.
3. Import the August statement.
4. Build the clearing effect; run suggest → review → confirm for July, watched.
5. Re-read 1050. Investigate the residue.

## Filed, not bundled

- `card → 1050` contradicts `gl-phase1-core-ledger.md:150` (spec says 1000).
  The method map is edited on the Accounting Settings page — **data, not
  code** — so this may be a toggle, not a build.
- A 1050 aging report, or this regrows silently.
- 7 feed accounts exist against 3 in the CoA; "real bank balance" needs a
  stated scope before it goes in any report.

## Open

1. Is spending deliberately kept outside GDX, or are the 11 bills simply
   unconfirmed? **Not yet asked.**
2. Are the 29 pre-cutover deposits meant to be reconciled at all?
3. CPA: is `card → 1050` right, and what treatment for processor payouts net of
   fees?
