# QB-Imported Invoices: Paid-Status & "Unknown" Repair — Research + Plan

**Status:** **OPERATIONAL, PARTIALLY RUN** — this is a production data-repair
plan, not a code plan, so "is it on main" is the wrong question for it.
Phases 1 and 3 were one-off supervised scripts and were deliberately not
committed (nothing matching them exists in `gdx_dispatch/tools/`).
**Phase 2 — the mark-paid backfill against bank records, ~26 invoices — is
still with the office.** Its precondition is met: the QB money-pull pause is
ON in prod (verified 2026-08-21), so corrections can no longer be reverted by
a sync.

> **PRIVATE DATA WARNING:** this doc names real customers and dollar amounts.
> Scrub before committing to the public repo (see public-repo hygiene rule).

Date: 2026-07-30. Researched read-only against prod (`gdx-db-1`, app
v1.33.0) and live QuickBooks (query-only; token healthy). Companion to
`payment-date-recording-plan.md` (v3) — that PR is the tooling this repair
uses; this doc is the data-repair worklist plus the importer defects that
caused it.

## What Doug reported

QB-imported invoices that should be marked paid are still unpaid, or show
"unknown".

## Findings (all verified 2026-07-30)

### 1. "Unknown" is identity loss, not an invoice status

`invoice_status` has no "unknown" value (draft/sent/paid/overdue/void — DB
enum rejects anything else). "Unknown" reaches the screen three ways:

- **Billing/invoice views**: fallback label when the invoice has no
  resolvable customer (`BillingView.vue:1155`, `InvoiceDetailView.vue:88`).
  - **3 invoices have `customer_id = NULL`** — the QB importer created them
    unlinked. Live QB lookup recovers the owner: all 3 are **Y-Built
    Construction** (50010651 $15,476.93, 50010522 $7,337.73, 50279194
    $2,558.47 — **$25,373.13** of open AR). A live GDX customer `Y-Built
    Construction` (`f28c75a0-…`) already exists to relink to.
  - **12 customers were renamed "Redacted" + soft-deleted on 2026-04-08**
    via the GDPR delete endpoint (`routers/gdpr.py:359`), PII wiped, zero
    audit rows that day. Consistent with the auto-accepting confirm-dialog
    bug (issue #215). 4 of the 12 hold 7 real invoices, incl. two open ones
    worth **$16,638.79** (50118684, 50119322) — QB says **Triple Star
    Construction**, which also still exists live (`1587073d-…`). Another
    redacted customer owns draft INV-2026-0002 ($2,900.98, matching the
    2026-04-02 check payment).
- **Jobs list chip**: `job_display_state.py:193` renders "Unknown" for jobs
  with no live invoices and no lifecycle stage. QB-imported invoices are
  created with `job_id=None` (`sync.py:918`), so their jobs never see the
  money and can fall into this chip.

### 2. The unpaid QB invoices are unpaid *everywhere* — the payments were never entered in any system

32 QB-mapped invoices sit at `status=sent`, **$74,685.35**, dated Nov 2024 –
Mar 2026. Live QB query on all 32: **QB balances match GDX exactly on all 31
real ones**. QB has 3 customer payments since 2026-05-01 (two of them $0) —
the office never entered these payments into QB, so no sync could ever fix
them, and with QB being phased out none ever will. **The only evidence of
payment is the bank record + Doug's knowledge.** This is exactly the
GDX-side backfill the payment-date plan anticipates.

Breakdown of the 32:

- **27 real invoices ≥ $80, ~$74,669** — the actual worklist (below).
  13 of the 27 have no customer email (known dunning gap).
- **4 penny/partial residues** (QB-confirmed): 49664151 $11.00, 50022295
  $3.62, 50167703 $1.00, 49638051 $0.35 → write off (already
  `dunning_paused`).
- **1 oddity**: 49692699 (Lyle Martin) — GDX and QB agree $1,500 of
  $3,154.46 remains; QB holds a ~$1,654 payment GDX never imported
  (`amount_paid=0`, zero payment rows). Same importer gap as §4.
- **1 piece of test junk in prod**: INV-2026-0001 "Test Customer Flow" $100,
  status sent, a $108.50 cash payment attached, QB-mapped to an id that no
  longer exists in QB. Void invoice + payment, delete the stale map.

13 further **native** GDX invoices ($26,962, May–Jul 2026) are normal
current AR — not part of this repair.

### 3. Importer defect: multi-invoice QB payments over-allocated ~$69,900

`pull_payments` flattens QB's many-invoice Payment onto the **first** linked
invoice and charges it the **full `TotalAmt`** (`sync.py:1173-1183`,
single-FK `payments.invoice_id`). Prod damage: **14 payments with
`amount > invoice.total`, phantom excess $69,899.92** (worst: $31,321.82
attached to 49832859, a $4,104.58 invoice). Statuses are right (status came
from QB `Balance`, not these rows) but every payments-by-period report, GL
cash mapping, and future bank-feed reconciliation reading `payments` is
inflated — and `_recalculate_invoice` treats local payment rows as
authoritative, so any future edit of these invoices bakes the wrong numbers
into `balance_due`.

### 4. Importer defect: paid-status without payment substance

- **22 QB-mapped invoices are `paid` with zero payment rows** (~$59K) — QB
  `Balance` was 0 at import, but the Payment objects were skipped
  (unmapped-at-pull-time, no `LinkedTxn`, or CreditMemo-settled;
  `sync.py:1182/1195/1199` skip silently; `SalesReceipt`/`CreditMemo` are
  never pulled at all).
- **280 of 281 paid invoices have `amount_paid = 0.00`** — the importer and
  the recalc never write it, but it's still read by
  `job_display_state.py:169` (partial-paid chip), `reports.py:1157`,
  `mobile_invoicing.py:131`, `PaymentsView.vue:322`, and more.
- `paid_at` on imported invoices was stamped from the **invoice date**, not
  the payment date (`sync.py:900-907`) — known, handled by the payment-date
  plan.

### 5. How status is actually computed (why raw flips would be fragile)

- Import maps QB `Balance ≤ 0 → paid, else sent` (`sync.py:774/824/914`);
  missing `Balance` falls back to full total → false-unpaid.
- The app-side `_recalculate_invoice` (`routers/invoices.py:205-280`)
  recomputes `total` from local lines and `balance_due` from local payment
  rows, and auto-flips `sent → paid` when a payment zeroes the invoice. It
  runs on any invoice edit. Consequence: **statuses must be repaired by
  creating real payment/adjustment rows, never by raw `UPDATE
  invoices.status`** — a raw flip resurrects the moment anything touches
  the invoice. (Corollary: recording the backfill payments through the
  normal API flips statuses for free.)
- No periodic invoice/payment sync exists (beat task removed,
  `celery_app.py:24-28`); import is manual "Sync Now" + webhooks only.

## Repair plan

Sequencing rule (from the payment-date plan, unchanged): **deploy the
payment-date PR → flip `qb_money_pull_paused` ON → only then write any
money data**. The pause column doesn't exist in prod yet (v1.33.0), so
nothing below starts until that PR ships.

### Phase 0 — prerequisites

1. Build + ship `feat/payment-date-recording` (already READY TO BUILD).
2. Flip `qb_money_pull_paused` ON (Settings). QB customer/item sync may
   keep running; money pulls are dead from here on.

### Phase 1 — identity repair (kills the "Unknown"s) — one supervised script

> **STATUS 2026-07-30:** built as `tools/qb_identity_repair.py` on
> `feat/qb-paid-status-repair` (with the GDPR confirm-name hardening).
> Live prod **dry-run results** — bigger than the research numbers, which
> only counted unpaid invoices:
>
> - **14 unlinked invoices** auto-resolve (13 → Y-Built Construction incl.
>   49832859/50187675 — the two worst over-allocation carriers — plus
>   1 → Paul Fiedler).
> - **2 husks auto-recover**: 2c16e82a → Triple Star Construction (6 rows),
>   396b72cc → **Roxanne Lusty** (2 rows; the earlier Tim-Loose guess was
>   wrong).
> - **7 items held for Doug's --pick**: invoices 1111/1115/1137/1140 whose
>   QB "customer" names are job-style descriptions each matching **3
>   duplicate live GDX customers** (import minted dupes — pick which one,
>   or merge later); husks 45d63522 (1 estimate), 4aae2dc7 (1 job),
>   719aa632 (draft INV-2026-0002 $2,900.98 + 1 job — likely the
>   2026-04-02 $2,900.98 check's customer, identify from the check).
>
> Apply command (in gdx-app-1, after Doug reviews the dry-run):
> `python tools/qb_identity_repair.py --apply --operator doug`
> plus `--pick <id>=<target>` for any of the 7 held items he can resolve.

One idempotent script (dry-run first, then `--apply`; audit rows for every
write):

1. Relink the 3 `customer_id IS NULL` invoices → live `Y-Built
   Construction` (`f28c75a0-…`).
2. Repoint the 5 invoices on redacted customer `2c16e82a-…` → live `Triple
   Star Construction` (`1587073d-…`). (Watch the duplicate "Triple Star
   Construction Andy Mast" customer — pick with Doug if ambiguous.)
3. Sweep the other 10 redacted customers for FK references (jobs,
   estimates, payments, appointments). Recover identities via QB maps /
   payment references where they exist (e.g. the $125 Tim Loose match, the
   $2,900.98 draft), report what's unrecoverable.
4. Void test invoice INV-2026-0001 + its $108.50 payment; delete stale QB
   map (qb_id 2769).
5. Root-cause guards (small PR): server-side confirmation token on
   `POST /api/gdpr/delete-customer` (type-name-to-confirm), and fix its
   audit write — 12 deletions produced zero audit rows. Bump issue #215.

### Phase 2 — mark-paid backfill (Doug/office, using bank records)

The 27-invoice worklist (names + amounts in §2 query output; regenerate
with the SQL in the appendix). For each, check the bank record:

- **Paid** → record the payment in GDX via the new dated dialogs / bulk
  Mark Paid, dated to the **bank deposit date** (canonical per Doug's
  Decision 0). Status flips to paid automatically via the recalc.
- **Genuinely unpaid** → leave `sent`; it feeds dunning later.
- Write off the 4 penny residues as credit adjustments (not payments).
- Backfill Lyle Martin's missing ~$1,654 payment (real amount + TxnDate are
  one read-only QB query away), leaving the true $1,500 open.

Recommended mechanics: manual entry through the UI (27 rows is an
afternoon, and each entry gets human eyes on the bank statement). A CSV
bulk-import path is not worth building for this volume.

### Phase 3 — payment-substance repair (scripted, one-off, after Phase 2)

One script, dry-run first, driven by read-only QB queries (reads are
metered; total here is trivial):

1. **Split the 14 over-allocated payments**: re-fetch each QB Payment's
   `Line`/`LinkedTxn` allocations; replace the single inflated GDX row with
   one row per linked invoice at the per-invoice applied amount (sum must
   equal `TotalAmt`; abort the row on mismatch). This also surfaces any
   sibling invoices that were left looking unpaid by the flattening.
2. **Backfill payment rows for the 22 paid-no-payment invoices** from QB
   Payment/CreditMemo data where it exists (real dates, real refs); where
   QB has nothing (SalesReceipt-era, credits), synthesize one row dated
   `paid_at` with `reference='qb-backfill'`. Store QB `PaymentRefNum` in
   `payments.reference` for everything touched.
3. **Backfill `amount_paid`** = Σ(unvoided payments) on all invoices, and
   file the follow-up issue to retire the column's readers.
4. Do **not** blanket-run `_recalculate_invoice`: imported invoices drop
   SubTotal/Discount/Shipping lines, so a recalc can silently change
   totals. Verify arithmetic in the script instead.

### Phase 4 — importer code (mostly: don't)

QB money-pull is being phased out and is paused from Phase 0, so don't
invest in fixing `pull_payments` allocation, the missing-recalc-on-payment,
or SalesReceipt import. File one issue documenting the defects (with the
line refs above) in case money-pull is ever revived. Worth doing anyway:
the `job_display_state` "Unknown" chip and `amount_paid` readers (small,
GDX-side, survive the QB phase-out).

### Verification

- SQL invariants post-repair: no invoice with `customer_id IS NULL`; no
  live invoice on a "Redacted" customer; per-invoice Σ(payments) ≤ total;
  no `paid` invoice with zero payment rows; `amount_paid` = Σ(payments)
  everywhere; `balance_due = total − paid − credited` on every touched row.
- /billing shows no "Unknown" rows; headed browser check.
- Re-run aging; then re-open the dunning question (13 of the still-open
  invoices need customer emails first).

## Appendix — worklist query

```sql
SELECT i.invoice_number, i.invoice_date, i.due_date, i.total, i.balance_due,
       COALESCE(NULLIF(TRIM(c.name),''),'(none)') AS customer, c.email
FROM invoices i
LEFT JOIN customers c ON c.id = i.customer_id
JOIN qb_entity_maps m ON m.entity_type='invoice' AND m.local_id = i.id::text
WHERE i.deleted_at IS NULL AND i.status='sent'
ORDER BY i.balance_due DESC;
```
