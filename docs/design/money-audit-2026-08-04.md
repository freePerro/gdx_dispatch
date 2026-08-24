# Money Audit — 2026-08-04

**Status:** **PARTIALLY FIXED** — see §0.6 for the authoritative list, and each
finding's own entry for whether it shipped.
Re-verified 2026-08-21; the §3 reporting cluster was then closed on 2026-08-22
(PRs #399, #400, #402, #403) and **RELEASED v1.75.0**, deployed to prod and demo
2026-08-23 and **walked on prod with real data**.

Prod-walk evidence (2026-08-23, v1.75.0, owner role, light + dark):
"Revenue by Period" renders labelled bars — Jul 2026 and Aug 2026 on a
$0-$35,000 axis — where before the release it drew an empty frame on a 0-1 axis
with no x labels. The KPI card and the chart now return the identical figure
($33,505.96); they disagreed by construction before. Credits net per period on
live data (Jul: gross $1,494.28 − credits $620.79 = $873.49; Aug: $32,809.13 −
$176.66 = $32,632.47). `/reports/outstanding-aging` and `/reports/cash-risk`
both report 19 invoices / $19,337.91, closing a $16,324.21 gap. The invoices CSV
header is `total` with real values, not a blank `total_amount` column. Invoice
50010651 ($15,476.93, paid) now returns `amount_paid = 15476.93` with
`total − paid − credits == balance_due`; the key did not exist before, which is
why MobileBillingView's "Paid" row had never rendered — it renders now. Fixed: the `core/invoice_invariants.py`
enforcement rail, migration `056_money_correctness_rails`, and findings M1, M2,
M4, M5, M6, M7, M9, M10, M11, M14, M24, M26, M37, **M8**, **M35**, **M19** and **M20** (and half of **M18**).
§4's two HIGH conversion findings closed 2026-08-23: **M22** (proposal-mode
estimates billed the sum of all tiers) was already resolved by the accepted-tier
copy at `invoices.py:1355` plus the deletion of `create_invoice_from_job`;
**M23** (estimates billed without checking status) is fixed here — the canonical
path now 409s on a non-accepted estimate, matching the two conversion paths that
already refused.
**Not fixed:** M18's other half (a tax component on `invoice_adjustments` —
a money rule plus a migration, deliberately not guessed), the rest of §4 (M25),
§5 and §6, and the frontend items in §7 — except M8's frontend half, which
shipped with it. The GL findings are no longer "gated on CPA review" in the
sense of being unbuilt — the ledger is live on prod (see `gl-phase1-core-ledger.md`)
— but the CPA questions themselves are still unanswered.
✅ **The regression net this audit built now runs** (#390, merged 2026-08-21).
`tests/test_zz_money_correctness_probe.py` no longer carries
`pytestmark = pytest.mark.health`, so its ten probes execute on every merge
instead of sitting green and unrun — the condition §0.6 set for itself, met on
2026-08-04 and acted on two weeks late. Counterfactually verified before the
marker came off: reverting `Invoice.tax_rate` to the pre-fix `Numeric(6,4)`
turns the m9 probe red. A failure here is a proven money defect, not a flake.

Deep audit of every code path that touches money: invoices, payments, Stripe, the
GL, sales tax, reports, estimates/pricing, bank feeds, QuickBooks, vendor bills,
payroll/commission, and the frontend.

**Branch audited:** `feat/dashboard-next-actions` @ `7fa276e` (prod is v1.39.0).

**Method.** Eight parallel deep-read passes, one per money domain, each required to
trace call sites end to end and reproduce arithmetic with concrete numbers. Every
finding below was then re-verified first-hand against the code before being written
down. Findings that did not survive that second pass were dropped — see
[§10 Rejected](#10-rejected-findings-checked-not-bugs) for the ones worth recording as
*checked and clean*, so nobody re-investigates them.

**Then the important part: nine of the findings were made executable and run.** See
[§0.5](#05-empirical-verification-what-actually-ran). Reading code produces opinions;
the probe suite produces evidence. Every one of the nine reproduced.

**Confidence labels.** `CONFIRMED` = traced end to end in code, arithmetic
reproduced. `PLAUSIBLE` = the code path is real but triggering it needs a condition
I could not confirm from the repo alone (external event ordering, prod data shape).

**A note on blast radius.** GDX is single-tenant and MN garage-door work is a
construction contract, so the customer-facing sales-tax rate resolves to 0 in the
normal case. Several findings below are tax-math defects that are *real but
currently quiet at GDX* and would bite on taxable retail (parts-only sales, door
listings) or any future tenant. Each says so explicitly. Do not let the "quiet
today" note downgrade the fix priority for anything that also affects totals.

---

## 0. The short version

If you read nothing else, these are the ones that are moving real money or are about
to:

| # | Finding | Why now |
| --- | --- | --- |
| M1 | Recording a payment on a QB-imported invoice rewrites its total from bad lines | Fires during the Phase-2 backfill the office is doing **right now** |
| M2 | `/confirm` and the Stripe webhook can both insert the same payment | No DB constraint; the code's own comment says they race on every fast payment |
| M3 | A partial Stripe refund voids the **entire** payment | One goodwill refund re-opens the full invoice and restarts dunning |
| M4 | Client picks the currency; server records the number as dollars | A $500 invoice can be settled for ~$3 |
| M5 | Portal "Pay" on a settled invoice charges the full total again | `balance_due <= 0` falls back to `invoice.total` |
| M6 ⛔ | `/api/commissions/calculate` mints commission from client-supplied totals, no role gate | Role gate SHIPPED. Rest **superseded** — commission is becoming a plugin |
| M7 | Estimate discounts silently evaporate on the first invoice recalc | Customer billed more than the estimate they signed |
| M8 | ✅ **FIXED** — Revenue reports sum a column no code ever writes | Several reports read `$0` against real billed work |

Everything is grouped by system below, worst first within each group.

---

## 0.5 Empirical verification — what actually ran

`gdx_dispatch/tests/test_zz_money_correctness_probe.py` turns nine findings into
tests that assert **what should be true**. A failure is therefore a proven defect,
not an inference.

Run it:

```bash
docker run --rm --entrypoint python -e JWT_SECRET="<32+ chars>" \
  -v "$PWD":/app -w /app docker-app \
  -m pytest gdx_dispatch/tests/test_zz_money_correctness_probe.py -m health -v
```

Result on `7fa276e` (before fixes): **9 failed, 1 passed.**
Result after the fixes in [§0.6](#06-what-was-fixed): **10 passed.**

The `-m health` is required and deliberate. The file carries the `health` marker,
which `pytest.ini` already excludes from the default suite, so a deliberately-red
diagnostic file cannot turn CI red — verified both ways (default run: 10 deselected;
opt-in run: 9 failed, 1 passed). As each finding is fixed its probe flips green; when
all ten pass, drop the marker and let them join the default gate as the regression net
that stops the invariants rotting again.

The one that passes is the control — a canonical rate-mode invoice
($1,000 taxable + $400 labor at 7.38% → tax $73.80, total $1,473.80) holding the
totals invariant exactly. That matters: it proves the harness is sound, so the nine
failures are real behavior rather than fixture artifacts.

| Probe | Asserted | Actual | Finding |
| --- | --- | --- | --- |
| Imported invoice, duplicated lines | total stays `1471.84` | **`2943.68`** — exactly doubled, invoice re-opens | M1 |
| Imported invoice, no lines | total stays `650.00` | **`0.00`** — total destroyed outright | M1 |
| `payments` unique constraint | exists on `(invoice_id, reference)` | **none** | M2 |
| Same reference recorded twice | 1 payment row | **2 rows** | M2 |
| Estimate discount carried | invoice `4500.00` | **`5000.00`** — $500 overbill | M7 |
| Flat-tax invoice grows | tax follows subtotal | **frozen at `73.75`** while subtotal went 1000→1500 | M9 |
| Overpayment visible | some surfaced field | **none; balance clamps to `0.00`** | M11 |
| Labor taxability carried | labor stays non-taxable | **copied as taxable** | M24 |
| Delete draft holding a payment | 409 | **deleted; $200 orphaned** | M37 |

Two results changed my conclusions and are worth reading twice.

**M2 is worse than documented.** I wrote it up as a *concurrency race* between
`/confirm` and the webhook, needing simultaneous requests. The probe records the same
reference twice **sequentially** and still gets two rows — because `record_payment`,
the staff endpoint behind the Record Payment dialog, has no reference dedupe at all.
Only the Stripe path has the check-then-insert. So this is not an exotic race: an
ordinary double-click double-records a payment, and with the idempotency middleware
inert (M36) nothing catches it at any layer.

**M7 fires earlier than documented.** I described the discount as evaporating on the
first recalc. The probe shows the canonical `POST /api/invoices` path never applies it
at all — the invoice is **born** at $5,000 against an accepted $4,500. The recalc
spring-back is a second, separate way to lose it.

Two caveats on the probes. They run against SQLite via `TenantBase.metadata`, so the
unique-constraint probe proves the constraint is absent **from the ORM models and
migrations**; a hand-applied index on prod would not show up here — worth confirming
against prod DDL. And the harness logs two caught `tenant_settings` read failures on
the estimate paths; those are swallowed by the application, and both estimate probes
fail on their own assertions with real numbers, not on that error.

---

## 0.6 What was fixed

All ten probes pass. Regression check: **267 tests** across invoices, payments,
estimates, tax, deposits, portal, vendor invoices and payroll, plus **218** across
QuickBooks and GL posting — all green with the new enforcement guard active.

**The enforcement rail** — `gdx_dispatch/core/invoice_invariants.py`. A
`before_commit` guard that rejects any invoice persisting with
`total ≠ Σ active lines + tax`. This is the "stays correct" half: the audit's worst
findings were not bad arithmetic but *callers violating the invariant
`_recalculate_invoice` assumes*, and fixing five of them individually does nothing
about the sixth. The ledger has had exactly this guard for its balance rule since S4,
which is why the GL came back clean. Two design notes learned by running it:

- **Commit, not flush.** An invoice and its lines are written across several flushes
  (insert header → get id → add lines), so a flush-time check sees half-built
  invoices and fails on legitimate work. It broke 17 tests before being moved.
- **Line-less invoices are exempt.** They cannot contradict a line sum, and that
  shape belongs to `totals_locked`. The bug class this catches is a hand-set total
  sitting *alongside* lines that disagree.

`GDX_INVOICE_INVARIANT=log` downgrades it to a warning; treat needing that as an
incident, not configuration.

**M8 — one definition of revenue** (2026-08-22). Four surfaces summed
`Invoice.total_amount`, NULL on all 349 prod rows: `/revenue-by-period`,
`/revenue-analytics` (`by_period`, which contradicted its own `by_job_type`), and
both `/export` CSVs — the `invoices` register exported a *blank* total column to
whoever the office sends it to. Replaced with three shared helpers so the next
revenue surface inherits the whole rule instead of two thirds of it. Counterfactual
proof, not assurance: with the router reverted the new tests report
`assert 0.0 == 1500.0` and the invoices CSV row reads `,paid,,0.0,`.
`/revenue-by-period` — the endpoint behind the chart, and the one M8 surface
SQLite cannot execute (`date_trunc`) — is guarded by a real Postgres test
against `gdx-test-postgres`, which reports `assert 0.0 == 2000.0` pre-fix.
Run it with `--network host` or it skips.

**Migration `056_money_correctness_rails`** — validated against real Postgres with
seeded duplicate payments and a QB-imported invoice, not just written and hoped for:

| Change | Verified result |
| --- | --- |
| Widen 4 tax-rate columns to `numeric(9,6)` | `0.073750` stored exactly |
| `invoices.totals_locked` + backfill by import marker | QB row locked |
| Collapse duplicate payments (void, keep earliest) | 2 rows → 1 live |
| Partial unique index on `(invoice_id, reference)` | duplicate INSERT rejected; re-record after void allowed |
| Derive `tax_rate` for frozen-tax rows | NULL → `0.073750` |

**A new finding the probes surfaced that code reading missed.** `Invoice.tax_rate`
was `Numeric(6,4)`, which **cannot represent Minnesota's 7.375%** — `0.07375` stored
as `0.0737`. The creating request computed tax correctly from the in-memory Decimal
($73.75 on $1,000); every recalc afterwards read the truncated rate and got it wrong
($110.55 instead of $110.63 on $1,500). An invoice's tax silently *changed* between
creation and its first edit. Four rate columns were affected. This is the clearest
argument in the document for executing invariants rather than reasoning about them.

**A bug in the fix itself, caught by testing the migration.** The unique index was
first written `WHERE reference IS NOT NULL`. Voiding duplicates doesn't remove them
from that predicate, so the index creation **failed on exactly the data the collapse
step was meant to clear** — it would have aborted on prod. Worse, it would have
blocked re-recording a payment after a wrongful reversal, contradicting the M14 fix
in the same change. The predicate is now
`reference IS NOT NULL AND voided_at IS NULL`.

**Findings fixed** (probe-proven): M1, M2, M7, M9, M11, M24, M37 — plus M4 (currency
locked to USD server-side and enforced at the webhook), M5 (portal total-fallback
deleted, void/zero-balance 409), M6 (`payroll.write` gate on commission minting),
M10 (mobile stores its tax rate), M14 (voided payments no longer block re-recording),
M26 (`invariant_ok` substring check), and the QB importer's line filter.

**Fixed later, after this section was written** — §3 in full (PRs #399/#400/#402/#403,
v1.75.0), M23, M13, M14 (stale failure events), M15 (dispute lifecycle, migration 076),
and **M12** (stale PaymentIntents — stateless Stripe-side sweep, no migration). Each is
marked at its own entry below; this paragraph is a pointer, not a second source of
truth. Read the entry.

**Not yet fixed** — M16 (ACH double-payment window), M17 items 2-4, M18's tax half
(needs a money-rule decision), the recording half of M3, the rest of §4, §5 and §6,
and the frontend items in §7. The GL findings remain gated on the CPA review. §9's
ordering still applies to what's left.

---

## 1. Invoice totals and the recalculation chokepoint

`_recalculate_invoice` ([invoices.py:205-281](../../gdx_dispatch/routers/invoices.py#L205-L281))
is the single function that derives `subtotal`, `tax_amount`, `total` and
`balance_due`. Its invariant is:

```text
total = Σ(active line_total) + tax
balance_due = max(total − Σ(non-voided payments) − Σ(credit memos), 0)
```

The math inside it is correct — Decimal throughout, `quantize(0.01, ROUND_HALF_UP)`,
voided payments excluded, credits netted. **Every bug in this section is a caller
that violates the invariant the function assumes.**

### M1 — Recording a payment on a QB-imported invoice rewrites its total from wrong lines `CRITICAL` `CONFIRMED`

**This is the most urgent finding in the audit, because the office is triggering it
right now.**

Two facts combine:

1. The QB invoice **create** path inserts *every* line QB returns, including
   `SubTotalLine` and `DiscountLine`, which QB already folded into `TotalAmt`
   ([sync.py:948-958](../../gdx_dispatch/modules/quickbooks/sync.py#L948-L958)) —
   there is no `DetailType` filter.
2. `_resync_invoice_lines` — a *different* function — does filter them, and its
   comment documents the exact prod damage:

   > *"Confirmed on prod invoice #1111 2026-05-09 — lines summed to $2,741.50,
   > persisted total $1,471.84."* ([sync.py:450-467](../../gdx_dispatch/modules/quickbooks/sync.py#L450-L467))

So the fix went into the resync path and never into the create path — and create is
the branch every first-time import takes. Those invoices are sitting in prod with a
correct stored `total` and a line set that sums to roughly double it.

The stored total is safe only while nothing recalculates. But recording a payment
calls `_recalculate_invoice` ([invoices.py:1997](../../gdx_dispatch/routers/invoices.py#L1997)),
which overwrites `invoice.total` from the line sum.

Concretely, on the real prod invoice named in that comment: total $1,471.84, lines
sum $2,741.50. The office records the $1,471.84 payment as part of the Phase-2
backfill. Recalc sets total to $2,741.50 and balance to $1,269.66. **An invoice that
was just paid in full re-opens owing $1,269.66**, re-enters aging, and would re-enter
dunning if dunning were on.

Two more shapes of the same bug:

- **Discount dropped:** QB invoice with items $1,000 and a −$100 discount line, total
  $900. Only the $1,000 item survives the filter on resync (the discount line is
  correctly excluded there but nothing replaces it), so paying $900 leaves a phantom
  $100 balance.
- **Line-less imported invoice:** total $650, zero lines (there were 282 of these
  historically). Paying $650 sets subtotal to 0 and destroys the $650 total —
  revenue reports lose the invoice entirely.

**Fix.** Two parts, and do the first one today:

1. **Stop the bleeding:** make `_recalculate_invoice` refuse to recompute
   `subtotal`/`total` for imported invoices — recalc `balance_due` and status only.
   The imported total is the source of truth; the local lines are known-lossy. Gate
   on a mapped QB entity or add an explicit `totals_locked` column (preferred — it
   states the intent and survives the QB phase-out).
2. Apply `_ITEM_LINE_TYPES` in the create branch too, or just have it call
   `_resync_invoice_lines`.

**Before shipping:** find out how many invoices are already mis-shaped, and whether
any have *already* been recalculated by a backfill payment. A read-only query over
imported invoices comparing `total` against `SUM(line_total)` will tell you both, and
the ones that already drifted need their totals restored from QB.

### M7 — Estimate discounts evaporate on the first recalc `HIGH` `CONFIRMED`

Two independent audit passes found this from opposite directions, and the code
comments admit it.

`Estimate.discount` is a flat dollar amount
([proposals/models.py:27](../../gdx_dispatch/modules/proposals/models.py#L27)).
`Invoice` **has no discount column at all** — I checked the model directly. And no
conversion path materializes the discount as a line.

The one-click job path bakes the discounted figure into `invoice.total` by hand and
deliberately skips recalc, with a comment saying exactly why:

> *"this path deliberately never calls `_recalculate_invoice` (estimate-derived
> totals carry discounts the line-sum recompute would drop)"*
> ([jobs.py:2888-2891](../../gdx_dispatch/routers/jobs.py#L2888-L2891))

That works until *anything else* touches the invoice — and eleven other call sites
run recalc, including recording a payment, editing any line, and issuing a credit
memo. Estimate subtotal $5,000 with a $500 discount produces a $4,500 invoice whose
lines sum to $5,000. The customer pays $2,000; `record_payment` recalculates; the
total springs back to **$5,000** and the balance reads $3,000 instead of $2,500. The
customer is billed $500 more than the estimate they accepted, with no audit trail.

The canonical `POST /api/invoices` path is worse — it never applies the discount at
all, so the invoice is born at $5,000. **The probe confirms this directly**: an
accepted estimate of $4,500 (a $5,000 gross with a $500 discount) produces an invoice
whose total is $5,000 at creation, before any recalc runs. The spring-back on recalc
is a second, independent way to lose the same discount.

**Fix.** Materialize the discount as a negative, non-taxable `Discount` line at
conversion, exactly the way deposit netting already does it. That makes
`total == Σlines + tax` a true invariant instead of something individual callers
hand-maintain and recalc destroys. Hand-adjusted totals cannot survive this codebase.

### M9 — Two of three invoice-creation paths leave `tax_rate` NULL, freezing tax `MEDIUM` `CONFIRMED`

`_recalculate_invoice` is rate-driven when `tax_rate` is set and falls back to
preserving a flat stored `tax_amount` when it is NULL. Only the canonical create path
sets it ([invoices.py:841](../../gdx_dispatch/routers/invoices.py#L841)):

- [mobile_invoicing.py:457-468](../../gdx_dispatch/routers/mobile_invoicing.py#L457-L468) — no `tax_rate`
- [jobs.py:2678-2695](../../gdx_dispatch/routers/jobs.py#L2678-L2695) — no `tax_rate`

So on a mobile or one-click invoice, editing a line moves subtotal and total but
leaves tax frozen. Subtotal $1,000 at 7.375% gives tax $73.75; adding a $500 line
makes the subtotal $1,500 while tax stays $73.75 instead of $110.63 — under-collecting
$36.88.

The `jobs.py` path has a second defect: it computes tax with float `round()`
(`round(subtotal_value * _rate, 2)`) rather than the Decimal `ROUND_HALF_UP` used
everywhere else, so the same inputs give a different answer by a cent on half-way
cases (`round(2.675, 2)` is 2.67 in float; Decimal half-up gives 2.68).

*Quiet at GDX today* (rate 0), real for taxable retail.

**Fix.** Set `tax_rate` on all creation paths and route the `jobs.py` math through
`_money()`. Better still, drop the legacy NULL branch once no rows need it.

### M10 — Mobile invoicing stamps tax but excludes it from the total `MEDIUM` `CONFIRMED`

The mobile path writes `tax_amount` from the estimate, then both estimate branches
overwrite `invoice.total` and `balance_due` with the line sum alone, leaving the tax
stamped but unbilled. Accepted tier $1,100 with $81.18 tax yields an invoice showing
total $1,100. The customer pays $1,100 in full; the office records it; recalc takes
the legacy branch (`tax_rate` NULL, per M9) and adds the stamped tax back — the total
becomes $1,181.18 and **the invoice re-opens with an $81.18 balance the customer was
never asked for.**

**Fix.** Recompute `total = subtotal + tax_amount` after the overwrite, or zero
`tax_amount` if this path genuinely means tax-free.

### M11 — Overpayment is clamped to zero and disappears `MEDIUM` `CONFIRMED`

`balance_due = max(total − paid − credited, 0)`. There is no customer-credit concept
outside the GL, and the GL overpayment gate only runs when
`ledger_posting_enabled(...)` — which
[defaults to off](../../gdx_dispatch/modules/ledger/service.py#L209-L212) ("No
settings row = off") and is off in prod.

So every double-collection in this document lands invisibly: balance reads `0.00`,
status reads `paid`, and the excess exists only as raw `Payment` rows summing above
the total. Nothing flags it, no refund workflow triggers.

**Fix.** Independent of the GL rollout, surface
`amount_overpaid = max(paid + credited − total, 0)` on the invoice payload and add a
report for non-zero values. This is the detection net for M2, M3, M5 and M12 — worth
building first because it tells you whether they have already fired.

### M12 — Stale PaymentIntents outlive the balance they were minted for `MEDIUM` `FIXED`

The amount is frozen when the intent is created, and `confirm` deliberately runs with
`require_balance=False`. Balance $500, customer opens `/pay`, office records a $300
check, customer's still-open tab confirms the original $500 intent → $800 collected
on a $500 invoice, clamped invisible.

**Fix.** Cancel outstanding intents when a payment or credit is recorded, and at
webhook time compare the amount against the remaining receivable, routing any excess
to a visible credit plus an alert.

**FIXED — MERGED #424, RELEASED v1.84.0; shipped INERT and repaired in v1.84.1. No migration.** Two halves, as prescribed:

*Close the open tab.* A Celery task, `payments.sweep_stale_intents`
(`priority:high`), runs whenever an invoice is settled some other way —
`record_payment`, `issue_credit_memo`, `apply_customer_credit`, `void_invoice`
and `_mark_invoice_paid`. It cancels intents in `requires_payment_method`,
`requires_confirmation`, `requires_action` and `requires_capture`
([Stripe cancel API](https://docs.stripe.com/api/payment_intents/cancel), read
2026-08-24) with `cancellation_reason="duplicate"`.

*Only intents that would actually overcharge, judged cumulatively.* The sweep
is told what the invoice still owes **after** the money that triggered it, sorts
the open intents newest-first, and keeps them while their **running total** fits
inside that figure. Killing a customer's live checkout that was never going to
overcharge them is a worse bug than the one being fixed — but a per-intent test
leaves a hole it cannot see.

Every mint here is sized to the full remaining balance (`_amount_cents`, and the
portal does the same — nothing mints a partial amount), so two open intents
exist only when the balance MOVED between them, which is exactly what happens
when a payment is **voided** and the balance goes back up. Judged one at a time,
an old $100 intent and a new $300 intent both "fit" under a $300 balance and
both survive; confirmed together they collect $400 on $300. Newest-first keeps
the tab the customer is actually looking at and cancels the stale one. A void
passes `remaining_cents=0`, so every open intent exceeds it and goes.

*Stripe is the register, not us.* The scan is **stateless**: it asks
`stripe.PaymentIntent.list` over a bounded recent window and filters on
`metadata.invoice_id`, which every **invoice-scoped** mint site stamps because
`/confirm` refuses to record a payment without it. "Complete by construction"
means complete for intents bound to an invoice — an endpoint that mints an
intent bound to no invoice at all (issue #421) is outside the scan by
definition, not covered by it. A first implementation kept a
`payment_intent_mints` table (migration 077); an adversarial review killed it.
That table needed four mint sites wired by hand, had no retention policy, no
Connect column, and — worst — a `canceled_at` that could not distinguish
"Stripe refused, it already succeeded" from "the network blipped", so a single
transient failure permanently marked a live intent as handled. The table, its
model and its migration were deleted; **the alembic head stays at 076**, so this
ships with no schema change and no rollback step.

**Probed against LIVE Stripe, not just read in the docs** (prod key, read-only
`list`, 2026-08-24). `PaymentIntent.list(limit=100, created={"gte": ...})`
returned successfully; `has_more` was present (`False`); every field this code
reads was present on the returned objects (`id`, `status`, `amount`, `created`,
`metadata`); and the binding the whole design rests on held — the only metadata
keys in use are `invoice_id` and `tenant_id`, and **`invoice_id` was stamped on
2 of 2** intents in the window. All were `succeeded`, so the sweep would
correctly find nothing to cancel on prod today.

`list`, not `search`: the Search API supports `metadata[...]` and would be a
one-liner, but its data is only "searchable in under 1 minute"
([Stripe search](https://docs.stripe.com/search), read 2026-08-24) — and this
bug lives inside that minute.

*A task, not an inline call.* The sweep makes two to six Stripe calls and
stripe-python 11.6.0 retries twice by default. A second adversarial review
caught the first version doing that **inside** the money transaction, holding
invoice, payment and ledger locks across a third-party outage — the two-commit
silent-write window this repo ranks highest — and, in the webhook, risking
Stripe's own timeout and a retry storm on top of the outage. The money commits
first; the sweep runs after, on its own. When the worker is down the sweep does
not happen and a stale intent can still overcharge: that is covered by the
backstop below, not by silence.

*Queueing it had to be made non-blocking too, which was not free.* Measured on
the app image, `.delay()` against an unreachable Redis took **19.1s** — and
`retry=False` alone did not fix it, because the stall was the **result
backend** reconnecting, not the broker ("Retry limit exceeded while trying to
reconnect to the Celery result store backend"). Nothing reads this task's return
value, so it is declared `ignore_result=True`, with `retry=False` and a bounded
write connection. Same measurement after: **0.06s**. Without that, moving the
work onto a task would have moved the latency from the Stripe call to the
enqueue and changed nothing.

*The balance is read by the task, not carried from the caller.* An earlier
version passed a `remaining_cents` computed at enqueue time and took
`min(passed, fresh)`, defending it as "safe in the dangerous direction". It was
not: a balance that moves UP between enqueue and execution — a payment deleted,
a line added, a credit reversed, none of which enqueue a sweep — made the task
trust the stale LOW figure and cancel a correctly-sized live checkout. The only
thing a caller knows that the task cannot read is whether the invoice was
**settled outright**, which is one bit, so it is passed as one bit.

*One deploy-window caveat, on the release that first ships this — and it is a
false record, not just a lost sweep.* `update.sh` starts the new app and
health-gates it BEFORE recreating the celery containers, so during that gate a
new web app can enqueue `payments.sweep_stale_intents` at a worker running the
previous image, which has never heard of that task. Celery logs it unregistered
and drops it.

This is **worse than a worker being down**, and an earlier draft of this entry
wrongly called them equivalent. A dead broker makes the enqueue raise, so the
audit row honestly records `stale_intent_sweep_queued: false`. Here the broker is
healthy, the enqueue succeeds, and the row records `true` for a sweep that will
never run. The `payment_exceeds_receivable` backstop still catches the money;
the trail is wrong for the length of that gate. Mitigation is operational:
recreate the celery containers with the app on that release, or know why those
rows may lie.

*Look where the object lives — resolved once, not asked of four callers.* A
platform-account scan for an intent minted on a connected account returns
nothing, which is indistinguishable from "all clear". The Stripe-driven paths
(webhook envelope, `/confirm`, ACH, portal charge) know the account and pass it;
the four **office** paths had no reason to and did not, so on a Connect tenant
the sweep silently no-opped on exactly the call site this fix exists for. The
task now resolves the account itself from the invoice's company and treats an
explicitly-passed one (the webhook envelope, the freshest source) as an
override.

*In-flight ACH is attempted, not excused.* Stripe permits cancelling a
`processing` intent for the bank-debit family — ACH, ACSS, AU BECS, BACS, NZ
BECS, SEPA — though "cancellation might fail due to a limited and varying
cancellation time window"
([lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle), read
2026-08-24). A first version skipped `processing` and logged that the money
"cannot be cancelled": wrong on the documentation, and refusing to try
guaranteed the overcharge it was reporting. It is attempted now; when Stripe
refuses, `stale_intent_in_flight_uncancellable` says the debit will overcharge on
settlement and that the backstop is what covers it. `requires_capture` cannot
arise here at all — no mint site sets `capture_method="manual"`.

*Say so when the sweep loses the race.* An intent that succeeded microseconds
earlier still overcharges. The money moved, so it is recorded in full —
discarding it would be a different lie — and `_mark_invoice_paid` logs at ERROR
and writes a `payment_exceeds_receivable` audit event carrying `charged`,
`remaining_before` and `excess`. It is filed against the **invoice**, because
that is the trail an operator reads when reconstructing a bill, and attributed
to the surface that recorded it (`stripe-webhook`, `stripe-confirm`,
`stripe-ach-charge`, `portal-charge-method`) rather than one blanket system
identity. This is the "alert" half of the prescription; routing the excess to a
**visible credit** is NOT built — the excess sits on the invoice as an
overpayment.

*A bug the fix itself creates, closed in the same change — and the first
"closure" was theatre.* Idempotency keys here are `(invoice, amount, method)`
and Stripe prunes them once "at least 24 hours old". Cancel a stale intent, then
have the office void or delete that payment: the balance returns, the customer
reloads, and the same key replays the *cancelled* intent — a pay page that
cannot charge, for up to a day.

The first attempt checked the **create response** for `status == "canceled"`.
That is provably unreachable: Stripe's idempotency layer replays "the resulting
status code and body of the **first** request"
([idempotent requests](https://docs.stripe.com/api/idempotent_requests), read
2026-08-24), and a freshly created intent is never cancelled. The guard read as
a guard and could not fire, and its test fabricated a response the API cannot
return. `_create_usable_intent()` now **retrieves** the intent after creating it
and re-mints under a fresh key when the live status is unusable — the only way
to know is to ask.

**A fourth review caught the ordering wrong at one call site, in the committed
code.** `void_invoice` uses a SINGLE transaction on purpose — an earlier review
forced the void, its part/change-order releases and its audit row to land or roll
back together — and the M12 enqueue was pattern-matched into the same source
position as its three siblings, each of which already has a commit behind it
there. At the void it therefore fired *before* the commit: a `priority:high`
worker could cancel the customer's live PaymentIntent at Stripe before the void
was durable, and a failure in the audit write or the commit would leave the
invoice not-void with the intent already irreversibly cancelled — the money side
committed and the record rolled back, the exact inversion of "the money commits
first". Moved after the commit; the `stale_intent_sweep_queued` flag is gone from
that audit row because it cannot honestly be known before it. The call-site test
mocked the enqueue and asserted its arguments, so it passed either way — the new
test records the sequence instead.

**v1.84.0 shipped this feature completely inert, and only the prod walk found
it.** `STRIPE_SECRET_KEY` was declared **app-only** in `docker-compose.yml`, so
the celery services merged `<<: *app-env` without it. The sweep runs on a
worker, so every Stripe call raised:

```
stripe._error.AuthenticationError: You did not provide an API key.
```

The task caught it, logged `stale_intent_scan_failed`, degraded, and returned
`{"results": []}` — **indistinguishable from "there were no stale intents"** —
then reported `succeeded` to Celery. Nothing cancelled, nothing raised, nothing
red. All 57 unit tests passed, the full matrix passed, all 16 CI checks passed,
and the feature did nothing at all in production.

None of those tests could have caught it: every one of them mocks Stripe. It was
found by enqueueing the real task for a real prod invoice and reading the
worker log — the walk, not the suite.

Repaired in **v1.84.1**: the key moved into the shared `x-app-env` anchor (one
declaration, so no service can be missed), a test reads the compose file the
deploy actually uses and fails if it is ever moved back, and the task now
refuses up front with a distinct `stripe_unconfigured` error instead of
reporting success. Only this task calls Stripe from a worker, so nothing else
was affected.

**One guard is deliberately recorded as unproven.** The `payment_exceeds_receivable`
audit write happens inside a SAVEPOINT so that a failed flush cannot poison the
session and take the payment's own commit with it. On SQLite — every unit test
here — reverting that savepoint changes nothing, because SQLite does not poison a
session that way. So the test that looks like its guard passes either way, a
structural spy on `begin_nested` was vacuous too (something else on the path
calls it), and a real Postgres proof needs an FK-valid object graph that was not
built. The savepoint is correct by construction from SQLAlchemy's semantics; it
is **not** verified, and this line exists so nobody later reads a green suite as
proof that it is.

Guarded by `gdx_dispatch/tests/test_stale_intent_cancellation.py` (63 tests, plus `test_celery_stripe_env.py`),
counterfactually verified — 31 in all: removing any of the five call sites,
weakening the cumulative overcharge rule to a per-intent one, reversing the
newest-first order, dropping the `metadata.invoice_id` filter, the Connect
account, the `processing` branch, the re-mint, the celery include entry,
`ignore_result`, `retry=False`, the post-payment balance, the task's re-read of
that balance, the audit actor, or the scan-truncation log each fails a test.

Two of those guards were vacuous when first written and were rebuilt after a
counterfactual proved they could not fail: the celery one asserted on
`celery_app.tasks`, which this test file populates itself by importing the
module, so it passed with the worker's `include` entry deleted. It now asserts
on `conf.include`, which is what the worker actually reads.

*Filed, not bundled.* Two defects found alongside this one, both a different
class and neither smuggled into this change:

- **#421** — `POST /api/stripe-connect/payment-intent` mints a destination
  charge from a **client-supplied amount** with passthrough metadata and no
  invoice binding. Found by the sibling sweep for this shape ("a money amount
  frozen at creation into a customer-redeemable object").
- **#422** — a payment landing after a void **resurrects the invoice to paid**
  while its parts and change orders are already back on the unbilled checklist.
  Nothing on the recording path guards on void, and `transition_invoice_status`
  is a pass-through with no transition table, so `void → paid` is permitted.
  The void sweep above removes the common case (a customer on the pay page) but
  cannot help an intent that already succeeded.

---

## 2. Stripe and payment recording

The 2026-08-04 hardening (PR #268) holds where it was applied: token-scoped invoice
resolution, server-derived amounts, ACH SetupIntent binding, webhook signature
verification that fails closed, and a webhook that raises so Stripe retries. I
verified each of those. The findings below are what it did not reach.

The scope tell is in the tests: `test_payments_portal_authz.py` covers `charge_method`
ownership, amount and void — and nothing about currency, the `/intent` endpoint, or
refunds.

### M2 — `/confirm` and the webhook can both insert the same payment `HIGH` `CONFIRMED`

`_mark_invoice_paid` is idempotent by reading first and inserting second
([core/payments.py:298-306](../../gdx_dispatch/core/payments.py#L298-L306)):

```python
existing = db.scalars(_select(Payment).where(
    Payment.invoice_id == invoice.id, Payment.reference == external_ref)).first()
if existing is not None:
    return
```

There is **no unique constraint** on `(invoice_id, reference)` — I checked the model
and the migrations. And two callers race by design; the module's own comment says so:

> *"The signed webhook usually beats the browser's confirm call"*
> ([core/payments.py:239-240](../../gdx_dispatch/core/payments.py#L239-L240))

Two concurrent transactions both see no row and both insert. One $500 charge becomes
two $500 `Payment` rows: payments double-counted, GL posts twice, and the balance
clamp (M11) hides it.

**The probe found this is not limited to a race.** Recording the same reference twice
*sequentially* also produces two rows, because `record_payment` — the staff endpoint
behind the Record Payment dialog — performs no reference dedupe whatsoever. Only the
Stripe path has even the optimistic check. So the everyday version of this bug is a
double-click, not a thread race, and with the idempotency middleware inert (M36)
there is no protection at any layer of the staff payment path.

The codebase knows the fix — `with_for_update` is used in inventory, purchase orders,
bank-feed matching, and even for invoice *verification*, which
[its own comment calls a non-money mutation](../../gdx_dispatch/routers/invoices.py#L2290-L2296).
The locking pattern is applied to an approval flag and not to money.

**Fix.** Add a partial unique index —
`UNIQUE (invoice_id, reference) WHERE reference IS NOT NULL` — and catch
`IntegrityError` as the idempotent no-op. That closes it at the only layer where the
race genuinely cannot slip through. Add `with_for_update()` on the invoice row in
`_mark_invoice_paid` as well.

### M3 — A partial refund voids the entire payment `HIGH` `CONFIRMED` 🟡 HALF FIXED

**The void is fixed, 2026-08-23.** `charge.refunded` now splits: a full refund
(`amount_refunded >= amount`) still voids the payment and re-opens the invoice;
a **partial** one leaves the payment and the balance alone. Refunding $50 of a
$500 payment no longer voids the $500, no longer flips the invoice paid→sent,
and no longer puts a paid-in-full customer back into dunning.

**Deliberately still open: the partial refund is not auto-recorded as money.**
The first implementation did record it as an `InvoiceAdjustment(kind='refund')`,
and an adversarial review found two ways that double-books, both needing a
schema change to close:

1. `amount_refunded` is **cumulative**, so a partial refund followed by a full
   one arrives as `amount_refunded == amount` and takes the void branch — which
   knows nothing about the partial row already written. Net paid goes
   **negative**: $550 reversed against a $500 charge, and every later office
   refund on that invoice 422s forever.
2. If the office also records the refund by hand — the normal way to record one
   — nothing links the two rows, so $50 returned is booked as **$100**, under
   the cap, silently.

Both need refunds keyed on **Stripe's refund id in a real column**, not inferred
from free text. Until that exists the webhook records the FACT — a
`stripe_partial_refund_received` audit event plus a WARNING log naming the
amounts and the invoice — and leaves the money entry to
`POST /api/invoices/{id}/refund`, which caps by net paid and posts to the
ledger. Incomplete and visible beats wrong and silent on a money surface, and it
is strictly better than the void it replaces. Scoped in
`stripe-refund-reconciliation-plan.md`.

**Known consequence, written down rather than discovered later:** until the
office records it, the dashboard "collected" tile overstates cash by the
refunded amount (`reports.py:1539` now says so).

**Sibling found in the sweep, NOT fixed here:** a **partial dispute** has the
identical shape — a $50 dispute on a $500 charge voids all $500. It is left
alone deliberately, because a dispute is provisional and needs the lifecycle
**M15** describes (there is no `charge.dispute.closed` handler at all), not a
one-sided split. Recorded against M15.

Prod exposure when this was fixed: 3 card payments totalling $1,021 and zero
refunds ever recorded.

The original finding:

`charge.refunded` routes straight to `_reverse_recorded_payment`, which sets
`voided_at` on the whole `Payment` row
([core/payments.py:723-726](../../gdx_dispatch/core/payments.py#L723-L726)).
`amount_refunded` is read nowhere in the codebase — I grepped to confirm.

Stripe fires `charge.refunded` for partial refunds too. So refunding $50 of a $500
payment as a goodwill credit voids the full $500: balance returns to $500, the invoice
flips from `paid` back to `sent`, and dunning chases a customer who paid $500 and was
refunded $50. Books understate cash by $450.

**Fix.** Compare `amount_refunded` against the charge amount. Full refund → void.
Partial → record an `InvoiceAdjustment(kind="refund")` for the refunded portion only.

### M4 — The client picks the currency; the server records the number as dollars `HIGH` `CONFIRMED`

`CreateIntentRequest.currency` is a client field passed verbatim to Stripe
([core/payments.py:123](../../gdx_dispatch/core/payments.py#L123),
[:366](../../gdx_dispatch/core/payments.py#L366)). The webhook then records
`amount_received / 100.0` as dollars
([:714](../../gdx_dispatch/core/payments.py#L714)) without ever checking the currency.

The hardening correctly stopped trusting the client's *amount* but left the *unit*
under client control. A $500 invoice: the server derives `amount_cents = 50000`, the
payer requests `currency: "idr"`, Stripe charges Rp 50,000 (about **$3**), the webhook
records **$500.00**, and the invoice is settled. For a zero-decimal currency like JPY
the `/100` division is also arithmetically wrong regardless of intent.

The same client-controlled `currency` reaches Stripe on the portal `charge_method`
path ([routers/payments.py:53](../../gdx_dispatch/routers/payments.py#L53)).
`ach_charge` and `portal_invoice_pay` correctly hardcode `usd`.

**Fix.** Drop `currency` from both request models and hardcode `"usd"` server-side,
then refuse to record any webhook event whose `currency != "usd"`. Both halves —
otherwise an intent minted before the deploy still records wrong.

### M5 — Portal "Pay" on a settled invoice charges the full total `HIGH` `CONFIRMED`

[portal.py:523-533](../../gdx_dispatch/routers/portal.py#L523-L533):

```python
amount_due = Decimal(str(invoice.balance_due if invoice.balance_due is not None else invoice.total))
if amount_due <= 0:
    amount_due = Decimal(str(invoice.total or 0))
```

A zero balance means *paid*, and the code responds by charging the total again. There
is also no `status == "void"` check and no idempotency key. A fully-paid $1,200
invoice, tapped in the portal, mints a $1,200 intent; the webhook deliberately records
second genuine payments ([core/payments.py:705-709](../../gdx_dispatch/core/payments.py#L705-L709));
the clamp hides the double collection.

This endpoint predates the hardening and was not part of it — the fixes went to
`routers/payments.py` and `core/payments.py`.

**Fix.** Return 409 when `balance_due <= 0` or `status == "void"`, delete the fallback
entirely, and add a server-derived idempotency key.

### M13 — Portal `/payments/intent` still takes a client amount and arbitrary metadata `MEDIUM-HIGH` `CONFIRMED` ✅ FIXED 2026-08-23

`charge_method` on this router got the full treatment — `_require_own_unpaid_invoice`,
server-derived amount, void and ownership checks. Its sibling `/intent` on the same
router did not ([routers/payments.py:148-190](../../gdx_dispatch/routers/payments.py#L148-L190)):
`amount_cents` comes from the body and `metadata` is forwarded verbatim.

Since the webhook records against `metadata.invoice_id` with no ownership check, an
authenticated portal user can mint an intent carrying **any** invoice UUID and have
the payment recorded there — cross-customer misattribution. Combined with M4, the
caller controls amount, currency and target at once.

**Fix.** Give `/intent` the same treatment `charge_method` already has: require an
invoice reference, run the ownership check, derive the amount server-side, hardcode
the currency, and whitelist metadata keys.

**✅ Done 2026-08-23.** All five, plus three things the prescription did not
anticipate:

1. **The "gold standard" was itself two changes short.** `charge_method` still
   honoured `body.currency` and still merged an arbitrary client `metadata`
   dict onto a Stripe money object. Parity now runs both ways — both endpoints
   hardcode the currency and whitelist metadata to the resolved `invoice_id`.
2. **Neither endpoint refused a DRAFT.** `core/payments.py:_resolve_public_invoice`
   has refused drafts since the 2026-08-08 §11 rail — *"a machine-priced
   closeout autodraft nobody reviewed"* must not take money — but this router's
   `_require_own_unpaid_invoice` never learned it, so both portal money paths
   sat 15 days behind the resolver next door. The guard is now in the shared
   helper, which fixes both at once. 404, not 409: an un-issued invoice must
   not be confirmed to exist.
3. **The audit block on `/intent` had never written a row.** It read
   `locals().get('db')` on a handler that took no `db` parameter, so it was
   always `None` — and would have logged `entity_id=""`, `details={}` had it
   fired. Threaded through and given a subject. The same dead block on three
   siblings (`setup_intent`, `ach_setup`, `remove_payment_method`) was fixed
   in the same sweep; the repo's `KNOWN_DEAD_AUDIT_BLOCKS` ratchet is four
   entries shorter and **every audit block on this router is now live.**

**Exposure: latent, never exploitable.** `_require_stripe_customer` runs first
and `CustomerUser` has no `stripe_customer_id` column, so every real caller
400s before reaching the invoice logic; prod has **zero** `payment_intent`
audit rows and **nothing in the repo calls the endpoint**. The live customer
pay path is the token-scoped one in `core/payments.py`. Hardened because the
day that column is added the hole opens silently — not because anyone walked
through it. An adversarial review is what forced this paragraph to say so;
the first draft narrated the hole as live.

### M14 — A late failure event voids a genuinely collected payment `MEDIUM` `PLAUSIBLE` ✅ FIXED 2026-08-23 (and the review found a bigger one next to it)

`charge.failed` and `payment_intent.payment_failed` both reverse whatever is recorded
for that PaymentIntent. Stripe does not guarantee delivery order, and a card retry
reuses the same intent. First attempt declines, retry succeeds, `succeeded` is
processed first and records $500, then the delayed `charge.failed` from attempt one
arrives and voids the good payment.

Recovery is blocked too: the existence check in `_mark_invoice_paid` does not filter
`voided_at`, so a redelivered `succeeded` sees the voided row and returns early.

The handler cannot currently distinguish a genuine late ACH return (which *must*
reverse) from a stale pre-success decline.

**Fix.** On any failure event, retrieve the PaymentIntent live and only reverse when
it is not currently `succeeded`. Filter `voided_at.is_(None)` in the existence check

**✅ Done 2026-08-23 — both halves.**

The `voided_at` filter in `_mark_invoice_paid` had already landed, so a
redelivered `succeeded` can recover a wrongly-voided payment; there is now a
regression test pinning it.

The first half is new. `charge.failed` and `payment_intent.payment_failed` both
route through `_reverse_unless_superseded`.

**The prescription above says to key on `status != "succeeded"`.** The fix keys
on the **charge** instead: each attempt on an intent is its own charge, and the
right question is "is this event about the current attempt?" — answered by
comparing the failed charge id against the intent's live `latest_charge`.

**Scope, corrected after review.** An earlier draft justified this by an ACH
hazard: *"a status-based rule would silently stop reversing returned money."*
**That is false and has been removed.** An adversarial review checked Stripe's
documentation: a `us_bank_account` failure arriving after the intent reaches
`succeeded` raises a **dispute**, not `charge.failed`/`payment_intent.payment_failed`
— those two only fire while the intent is still `processing`, and the intent
stays `succeeded` through an ACH return. So the "genuine return vs stale
decline" dilemma never occurs on this path.

> **Source:** <https://docs.stripe.com/payments/ach-direct-debit> — read
> **2026-08-24**. Verbatim: *"If a payment fails after funds have been made
> available in your Stripe balance, Stripe immediately removes funds from your
> Stripe account. In rare situations, Stripe might receive an ACH failure from
> the bank after a PaymentIntent has transitioned to `succeeded`. If this
> happens, Stripe creates a dispute with a `reason` of: `insufficient_funds`,
> `incorrect_account_details`, `bank_cannot_process`."*

What the guard actually covers is **card retries**, which is where the ordering
hazard genuinely lives. Keying on the charge is still the better rule — it
answers the question being asked and does not depend on intent-status
transitions in any flow — but it is a robustness choice, not the closing of a
live ACH hole. The status version would have been safe too.

A superseded card decline names a charge the intent has moved on from, and is
ignored with a loud log.

**Disputes deliberately do NOT go through that check.** `charge.dispute.created`
leaves the intent `succeeded` while the money is held, so routing disputes
through the status check would refuse every one of them. A counterfactual test
pins that — the over-correction fails the suite.

**When it cannot be established** (no charge id in the event, Stripe
unreachable, key missing) it falls back to reversing — today's behaviour — and
logs `failure_event_unverified` at ERROR. A missed reversal is worse than a
reversal that has to be undone: the invoice would read paid, dunning would stop
chasing, and the cash would be gone with nothing on the record.

**The review's real find, and it is fixed here too: a dispute INQUIRY was
voiding real payments.** `charge.dispute.created` reversed unconditionally, and
Stripe's `warning_*` dispute statuses are inquiries — the bank is asking a
question and **no funds have been withdrawn**. ACH raises these routinely. So a
paperwork request re-opened the invoice, put a customer who had paid back into
dunning, and booked cash as gone that was still sitting there. There was no
`dispute.status` branching anywhere in the module. Now `warning_*` is noted and
not reversed; a real dispute (`needs_response`, `under_review`, `lost`) still
reverses; a missing status still reverses, because absent is not "inquiry".

> **Source:** <https://docs.stripe.com/api/disputes/object> — read
> **2026-08-24**. The `status` enum documents `warning_needs_response` as *"An
> inquiry that requires a response"*, `warning_under_review` as *"An inquiry
> under review after evidence submission"*, and `warning_closed` as *"An
> inquiry closed **without becoming a formal dispute**"* — against
> `needs_response`, *"A dispute that requires a response."*
> <https://docs.stripe.com/disputes> (same date) says a formal dispute
> *"immediately reverses the payment… Stripe debits your balance for the
> payment amount and dispute fee."* An inquiry is not a debit.

**Two traps found by the same review, NOT fixed, filed here:**

1. **No `api_version` is pinned anywhere.** `latest_charge` replaced `charges`
   on the PaymentIntent in Stripe's `2022-11-15` version. Webhooks render at
   the *account's* API version while the library retrieves at its own, so on an
   account pinned before that, `data.get("latest_charge")` is always empty and
   the `payment_intent.payment_failed` arm degrades to "unknown" — reversing on
   the event alone, i.e. pre-M14 behaviour, silently. Pinning the version is a
   repo-wide decision, not a payments-module one.
2. **Connect retrieves needed the account and did not have it** — fixed in
   passing (the envelope's `account` is now threaded into the live read), but
   it is worth naming: PaymentIntents are *created* with
   `**_stripe_extra(tenant)`, and any live read that omits `stripe_account`
   looks in the platform account, 404s, and degrades every event to "unknown".
   Any future live Stripe read from a webhook has the same trap.
so a legitimate re-record can heal a wrongful void.

### M15 — Dispute handling is one-directional `MEDIUM` `CONFIRMED` ✅ FIXED 2026-08-23

**Sibling added 2026-08-23 during M3's sweep:** disputes also void the payment
in FULL regardless of the disputed amount, so a $50 dispute on a $500 charge
reverses all $500 — the same shape M3 fixed for refunds. It was not split the
same way, because a dispute is provisional: the lifecycle below has to exist
first, or a partial split just creates a second wrong state to unwind.

`charge.dispute.created` voids the payment (correct), but there is no
`charge.dispute.closed` handler anywhere. Winning a dispute reinstates the money at
Stripe and nothing restores it in GDX: the payment stays voided, the invoice stays
open, dunning chases a customer whose charge stood, and $600 sits in the bank with no
`Payment` row.

**Fix.** Handle `charge.dispute.closed` with `status == "won"`.

**✅ Done 2026-08-23 — and NOT the way this line says.** Stripe publishes the
money movement itself, and these are the only two events that mean cash
actually moved because of a dispute:

| event | Stripe's wording |
|---|---|
| `charge.dispute.funds_withdrawn` | *"Occurs when funds are removed from your account due to a dispute."* |
| `charge.dispute.funds_reinstated` | *"Occurs when funds are reinstated to your account after a dispute is closed."* |

> **Source:** <https://docs.stripe.com/api/events/types> — read **2026-08-24**
> against the currently published docs. No `api_version` is pinned anywhere in
> this repo (see the trap below), so "current" is the operative caveat.
> **Vendor-stated, not live-proven:** there is no test-mode key wired here for
> webhook replay, so these events were not probed against a real account.

Keying on those rather than on `closed` + `status == "won"` is better in three
ways. It says *money moved* instead of *a case ended*; `closed` also fires for
`warning_closed`, an inquiry ending with nothing to reinstate; and it covers a
case `closed` cannot — an **inquiry that escalates**. The inquiry guard added
the same day correctly declines to reverse a `warning_*` dispute, but that
dispute can still become formal, and when it does the withdrawal arrives here.
`charge.dispute.created` had already fired and could never see it.

Reinstating **un-voids** the existing row rather than inserting one: a second
row would double-count against `core/invoice_paid.py`, which sums non-voided
payments, and the invoice would read as paid twice over. A reinstatement with
nothing reversed (an inquiry that closed in our favour) is a normal outcome,
not an error.

**Migration 076 — `payments.voided_reason` — is what makes it safe, and an
adversarial review is why it exists.** `voided_at` was the entire state, and
THREE things set it: a dispute reversal, a **full Stripe refund**, and the
office's own void-payment action. With only `voided_at` to go on, a
reinstatement un-voided whichever row it found — proven against a real
database: a refunded payment came back and the invoice read paid on cash that
had been returned. Money invented.

A first attempt guarded on "does this invoice carry a refund adjustment",
which **cannot fire on the path that matters**: `_apply_charge_refund`'s
full-refund branch is a bare `return _reverse_recorded_payment(...)` and
deliberately leaves the money entry to the office endpoint, so the guard was
checking for a row that path never writes — and the test agreed with it,
because the test built the refund by hand instead of driving `charge.refunded`
through the webhook. Both are fixed; the test now drives the real path.

Only `charge.dispute.created` and `charge.dispute.funds_withdrawn` may be
undone. A refund, an office void, or a **NULL from before the column existed**
is refused with a loud log and a `reinstate_needs_review` status. Prod carries
**3** voided payments, all of which will read NULL — the correct outcome for
rows whose reason nobody recorded.

Two more the review caught, both fixed: neither direction wrote an **audit
row** (invariant #1, in a change whose subject is money — a `logger.warning`
is not a record), and reinstating **re-stamped `paid_at`** to the dispute's
close date, silently restating a closed month on every report that groups by
it. The date now comes from the payment row, which is the surviving record of
when the money actually arrived.

**The sibling noted above is still open**: a dispute voids the payment in FULL
regardless of the disputed amount, so a $50 dispute on a $500 charge reverses
all $500. The lifecycle this fix adds is the precondition for splitting it —
which is exactly why it was not split first.

### M16 — ACH has a double-payment window `MEDIUM` `CONFIRMED` (gap) / `PLAUSIBLE` (occurrence)

Nothing is recorded while an ACH debit is `processing` — there is no
`payment_intent.processing` handler — so the balance stays full and the Pay button
stays live. Stripe's idempotency key expires after 24h, so a customer paying Friday
and again Monday mints a second intent; both settle.

**Fix.** Handle `payment_intent.processing` with a pending marker and suppress the pay
URL while an intent for the current balance is in flight.

### M17 — Smaller payment-path items `LOW`

- ~~**Unauthenticated tenant-wide `GET /api/payments`**~~ ✅ **DELETED 2026-08-23.**
  It had no auth dependency and returned the whole AR book, shadowed only
  because `ui_compat`'s authenticated version registers first. **The failure
  mode was proven, not theorised:** simulating the ui_compat import failure
  that `app.py` explicitly guards against turned an unauthenticated
  `GET /api/payments` into **HTTP 200 with 200 payment rows** — invoice
  numbers and amounts, no credentials sent. After the deletion the same
  simulation returns **404**.

  `authz_sweep.py` could never have caught it: it deliberately ignores
  shadowed duplicates as phantom findings, and cited this very route as its
  example. That reasoning was true only while the import it depended on kept
  succeeding. The sweep's comment now says so, and a regression test asserts
  on the ROUTER (where the twin is visible) rather than the assembled app
  (where the shadow hides it).
- **No idempotency fallback on `charge_method`**: the key is optional from both header
  and body, so a double-click makes two distinct intents and two full-balance charges.
  Derive a fallback server-side like the public path does.
- **`confirm` records `pi.amount` while the webhook records `amount_received`** —
  identical under auto-capture, divergent the moment manual capture appears.
- **`_next_invoice_number` is `COUNT(*) + 1`** ([invoices.py:177-179](../../gdx_dispatch/routers/invoices.py#L177-L179)),
  so concurrent creates collide on the unique constraint. Not a money error, but it
  500s the loser.

---

## 3. Sales tax and financial reporting

### M8 — Revenue reports sum a column nothing writes `HIGH` `CONFIRMED` ✅ FIXED

> **FIXED 2026-08-22.** All four surfaces now share one revenue definition
> (`_revenue_amount_sql` / `_revenue_where_sql` / `_revenue_orm_filters` in
> `routers/reports.py`): `COALESCE(total_amount, total)` and billed statuses
> (`sent`/`paid`/`overdue`). `_summary_window` and `/top-customers` were routed
> through the same helpers — a **behaviour-preserving refactor**, since both
> already carried exactly those two terms — so the definition cannot drift.
> Regression net: seven `test_m8_*` tests in `tests/test_reports.py`, each
> counterfactually verified to fail against the pre-fix code (`0.0 == 1500.0`),
> plus the first `ReportsView` spec.
>
> **Revenue also moved onto the billed date.** All four surfaces grouped and
> filtered on `created_at`, which for imported invoices is the import run: 278
> QB invoices spanning 2024-2026 all carry `created_at = 2026-03-29`, so the
> chart drew a **$607,419.52 spike on the import day** and emptied the months
> the work was actually billed in. `_summary_window` moved to `invoice_date` on
> 2026-04-27 (Phase D) and the raw-SQL siblings were missed — the same sibling
> gap as `total_amount`, a third time. Now shared as `_revenue_date_sql()` /
> `_revenue_date_expr()`. Prod after the fix, 2026 by month: Jan $13,020.96 ·
> Feb $11,116.00 · Mar $21,960.95 · Apr $6,033.72 · May $8,619.61 ·
> Jul $69,741.68 · Aug $32,809.13 (YTD $163,302.05, against an all-time billed
> book of $826,772.77 spanning 2024-2026). Guarded on Postgres by
> `test_m8_revenue_periods_use_the_billed_date_not_the_import_date`, which
> reports `0 == 4200.0` pre-fix.
>
> **Deposits COUNT — an earlier draft of this fix got that wrong.** It excluded
> `billing_type='deposit'`, reasoning that a deposit and its final invoice bill
> the same work twice. The adversarial audit refuted it from
> `modules/deposits/service.py` rule 2: *"the final invoice nets the deposit
> with a negative line … no 150% double-count"* (`service.py:503` writes
> "Less deposit paid — <n>"; prod INV-000354 carries `-2936.49` against
> INV-000353). De-duplication already happens at invoice creation, so the filter
> would have subtracted deposits a **second** time — and only ONE of the five
> non-void deposits on prod has a netting sibling, so the other four (including
> a **paid $6,793.04**) would have been erased from revenue. The wrong figure
> $811,407.85 appeared in an earlier revision of this doc; the correct billed
> total is **$826,772.77**.
>
> **This finding was incomplete.** It named only the backend. The chart was
> broken a SECOND, independent way: `ReportsView.vue` mapped `b.label` / `b.value`,
> fields `/revenue-by-period` has never emitted (it returns `period_start` /
> `revenue`), so both chart arrays were `[undefined, ...]`. **Fixing either half
> alone still left the chart blank** — which is why a prod browser walk on
> 2026-08-22 found an empty frame on a 0–1 axis with no x-axis labels, sitting
> beside a KPI card showing real money. Both halves shipped together.
>
> **Why it survived the 2026-08-04 audit and every test:**
> `docker/demo/seed_demo.py:302` writes `total_amount=total`, so the chart works
> on the demo stack — and every pre-existing test in `test_reports.py` seeded
> `total_amount` too. The new tests seed the PROD shape (`total_amount` NULL).
> Excluding voids and drafts moves prod revenue from a gross $829,164.66 to
> **$826,772.77** (2 void invoices, $2,391.89). Deposits stay in. Note that
> figure is **gross billed across 2024-2026**, not a year and not net: four
> credit memos totalling **$797.45** are not deducted, because netting
> adjustments per period is M18/M19/M20, not M8.


`Invoice.total_amount` is nullable
([tenant_models.py:479](../../gdx_dispatch/models/tenant_models.py#L479)) and **no
invoice-creation path writes it** — I grepped every writer; the hits belong to other
models entirely. The codebase already knows:

> *"`total_amount` (nullable, almost never populated by any insert path) … which is
> null on every prod row, so Dashboard Revenue read $0 against $712k of real billed
> work."* ([reports.py:70-75](../../gdx_dispatch/routers/reports.py#L70-L75))

Only `_summary_window` was fixed. Three surfaces still sum the bare column:

- `/revenue-by-period` ([reports.py:299-300](../../gdx_dispatch/routers/reports.py#L299-L300)) — `$0` revenue every period, with a real invoice count beside it.
- `/revenue-analytics` — `by_period` uses `total_amount` ($0) while `by_job_type` uses `COALESCE(total_amount, total)` (real dollars), so **the payload's own total contradicts its own detail rows**.
- `/reports/export` for `invoices` and `revenue` — blank and all-zero columns in the CSVs.

All three also lack a status filter, so drafts, voids and deposits count wherever the
column *is* populated.

**Fix.** `COALESCE(total_amount, total)` plus `status IN ('sent','paid','overdue')`
everywhere, and export `total`. Then delete `total_amount` — a column nothing writes
and five things read is a trap that keeps re-firing.

### M18 — Sales-tax report double-counts supersessions and books credited tax as collected `HIGH` `CONFIRMED` 🟡 HALF FIXED

> **Half fixed 2026-08-22.** `tax_collected` no longer keys off
> `paid_at IS NOT NULL`; it requires a real, non-voided payment. Six prod
> invoices carry `paid_at` with zero cash — two of them carry tax — so credited
> tax was sitting in the remittance-liability bucket as if collected. Guarded on
> Postgres by `test_m18_tax_is_collected_only_when_cash_arrived`, which reports
> `Obtained: 100.0` against the pre-fix code.
>
> **Still open — needs a decision, not code:** storing a tax component on
> `invoice_adjustments`. Today the table carries a flat `amount` with no tax
> split, so a credit cannot reduce the tax it originally charged. Prod exposure:
> **4 credited invoices carrying $570.79 of tax against $797.45 of credits.**
> Splitting a credit into tax and non-tax is a money rule (pro-rata at the
> invoice's rate? operator-entered? credits never reduce tax?) and needs a
> migration, so it is deliberately not guessed here.


The known limitation is still live: the report has no join to `invoice_adjustments`,
and `InvoiceAdjustment` carries a flat `amount` with no tax component. Two things make
it worse than the docstring admits:

1. `adjusts_invoice_id` is accepted on `POST /api/invoices` **today**
   ([invoices.py:388](../../gdx_dispatch/routers/invoices.py#L388),
   [:838](../../gdx_dispatch/routers/invoices.py#L838)) — replacement invoices are
   creatable now, not "when §12 lands".
2. A fully-credited invoice flips to `paid` with `paid_at` stamped
   ([invoices.py:275-280](../../gdx_dispatch/routers/invoices.py#L275-L280)), and
   `tax_collected` keys off `paid_at IS NOT NULL`. So credited tax lands in the
   remittance-liability bucket **with zero cash received**.

$2,000 at 7.38% is $147.60 tax. Credit it in full and re-issue at $1,500 ($110.70
tax): the report shows $258.30 liability and at least $147.60 "collected", against a
truth of $110.70 and $0.

**Fix.** Store a tax component on `invoice_adjustments` and net it per period; gate
`tax_collected` on actual non-voided payments rather than `paid_at`.

### M19 — Credit memos reduce no revenue report; deposits and finals overlap `HIGH` `CONFIRMED` ✅ FIXED

> **FIXED 2026-08-22 — but only half of what this finding prescribed.**
>
> DONE: revenue aggregates now subtract Σ(credit_memo) per period, attributed
> to the date the credit was ISSUED — matching the GL, which
> posts `post_credit_memo` at `effective_at = adjustment.created_at.date()`.
> The ledger is the book of record, so the reports follow it rather than
> inventing a second convention.
>
> NOT DONE, ON PURPOSE: "exclude `billing_type='deposit'` from billed revenue"
> is **wrong**, for the same reason M8's deposit prescription was. The final
> invoice already nets the deposit with a negative "Less deposit paid" line
> (`modules/deposits/service.py` rule 2), so excluding it subtracts the deposit
> twice. Tested against the one real pair on prod: deposit $3,112.61 + final
> $3,288.74 − credits $176.12 = **$6,225.23, exactly the job's true value**;
> excluding the deposit gives $3,288.74. Netting the credits is the whole fix —
> and it reproduces this finding's own worked example exactly
> ($2,000 + $9,500 − $1,500 = $10,000, where the finding reported $11,500).
> `test_m19_does_not_also_exclude_deposits` exists to stop it being "fixed" the
> prescribed way later.
>
> **`credit_applied` is excluded from the subtraction**, which the first pass of
> this fix got wrong. It is a settlement event, not a revenue reduction: the
> credit memo that minted the customer credit already reduced revenue, so
> subtracting the application counts it twice. The ledger draws the same line —
> `modules/ledger/reports.py::_cash_events` signs `credit_applied` **+1**,
> grouping it with payments and refunds as cash, while plain credit memos are
> not cash events at all. (`_recalculate_invoice` subtracts both, correctly,
> because it computes BALANCE, not revenue.) Zero `credit_applied` rows exist on
> prod, so nothing would have caught this;
> `test_m19_credit_applied_is_not_a_second_revenue_reduction` now does.


`Invoice.total` is never reduced by a credit memo (only `balance_due` is), and no
revenue aggregate joins `invoice_adjustments`. Deposit invoices are `sent`, so they
count as revenue at accept time, while the final only nets the *paid* portion of the
deposit and credit-memos the remainder — which flips the deposit to `paid` without
reducing its total.

A $10,000 job with a $2,000 deposit of which $500 was paid reports $2,000 + $9,500 =
**$11,500 against $10,000 of true revenue**.

**Fix.** Subtract Σ(credit_memo) per period in revenue aggregates and exclude
`billing_type='deposit'` from billed revenue, mirroring what the sales-tax report
already does. This and M18 are the same missing subtraction seen from two reports —
one shared "net adjustments per period" join fixes both.

### M20 — `/reports/outstanding-aging` windows AR on `created_at` and includes drafts `MEDIUM-HIGH` `CONFIRMED` ✅ FIXED

> **FIXED 2026-08-22.** All three defects: the `created_at` window is gone
> (aging is a point-in-time backlog, so a receivable older than the 30-day
> default no longer vanishes and the 91+ bucket can fill from real invoices,
> not just QB imports); drafts and voids are excluded; and it now ages from
> `due_date` — falling back to `invoice_date` only where due_date is null — so
> it agrees with `/reports/cash-risk` instead of structurally disagreeing.
>
> **Proven, not asserted.** An adversarial review measured the two views still
> $16,324.21 apart after the first pass — aging admitted `paid` invoices and had
> no not-yet-due skip, so future-dated receivables landed in the "0-30 days
> outstanding" bucket. Both were fixed. On prod now: aging **19 rows /
> $19,337.91**, cash-risk **19 rows / $19,337.91**.
>
> The endpoint also stopped accepting `start_date`/`end_date`. Removing the
> window without removing the parameters would have left a caller able to pass
> a range that silently does nothing — a worse defect than the one being fixed.
> (Zero frontend callers today; the endpoint is reachable but unrendered, so
> this fix is correctness-in-waiting rather than something a user sees.)


The query filters `created_at >= start_dt` with a 30-day default and has no status
filter ([reports.py:806-845](../../gdx_dispatch/routers/reports.py#L806-L845)).
A draft's `balance_due` equals its total at creation, so drafts appear as receivables.
And **any receivable created more than 30 days ago vanishes from aging entirely** —
the 91+ bucket can only ever fill from QB imports whose `created_at` is the import
date.

It also ages from `invoice_date` while `/reports/cash-risk` ages from `due_date` and
excludes paid/void/draft — two AR agings that structurally cannot agree.

**Fix.** Drop the `created_at` window (aging is a point-in-time backlog), exclude
draft and void, and anchor on `due_date` to match cash-risk.

### M21 — Smaller reporting items `LOW–MEDIUM`

- **Estimate tax uses float `round()`** ([proposals/totals.py:113](../../gdx_dispatch/modules/proposals/totals.py#L113))
  while invoices use Decimal `ROUND_HALF_UP`. Taxable $36.25 at 10% gives $3.62 on the
  estimate and $3.63 on the invoice.
- **Business dates come from the server's UTC clock**, so an invoice created at 7:30pm
  CDT on July 31 is dated Aug 1 and its tax is remitted in the wrong month.
- **Five reports window on `created_at` rather than `invoice_date`**, which for
  QB-imported rows is the import date — business-date ranges return all-or-nothing
  around the import day.
- **Partial-period buckets are presented as full periods**: with the default 30-day
  window, a "quarter" card holds 30 days of tax with nothing marking it partial.
- **`/api/exports/*` swallows all SQLAlchemy errors and returns `[]`**, so a schema
  mismatch produces a short CSV indistinguishable from "no data"; `_fetch_invoices`
  has no `deleted_at` filter, so soft-deleted invoices export as live.
- **`modules/reporting/service.py`** counts deleted and void invoices in
  `total_billed` — but I found no router callers, so it is dead-but-loaded.

---

## 4. Estimates, pricing and conversion

The pricing engine core is sound: `sell = cost/(1−margin)` correctly implemented and
labeled, margins validated to [0,1), tier ranges half-open with fail-loud on gaps or
overlaps. The problems are at the estimate→invoice boundary, where a discount-aware
totals function meets an invoice invariant that has no discount concept.

### M22 — Proposal-mode estimates bill the sum of all tiers `HIGH` `CONFIRMED` ✅ FIXED

**Closed, verified 2026-08-23.** Both halves are gone. `invoices.py:1355` now
resolves `accepted_tier_id` and bills `tier_contract_lines(...)` for that tier
alone, collapsing a flat tier to one package line at the tier price — the
comment there records that the MOBILE builder stores all three tiers' lines
untagged, which is what made the old unconditional copy bill Good+Better+Best
summed. The `jobs.py` half no longer exists: `create_invoice_from_job` was
DELETED in the 2026-08-08 audit (~200 lines with its own numbering and tax
resolution, and zero frontend callers).

Good/better/best tiers are stored as ordinary `EstimateLine` rows on one estimate, and
accepting a tier sets `accepted_tier_id` and the estimate total without touching the
lines. Both office conversion paths then copy **every** line with no tier filter —
[invoices.py:858-883](../../gdx_dispatch/routers/invoices.py#L858-L883) and
[jobs.py:2618-2637](../../gdx_dispatch/routers/jobs.py#L2618-L2637).

Tiers of $800/$1,100/$1,400 with "better" accepted produce an invoice of
**$3,300 + tax**. Every tier line also becomes a parts-needed row, so the job
over-orders threefold.

Mobile invoicing gets this right (one line for the chosen tier). The paths disagree.

**Fix.** When `proposal_mode` is set with an `accepted_tier_id`, restrict the copy —
invoice lines, job parts, and `_recalculate_total` — to the accepted tier, or collapse
to a single line the way mobile does.

### M23 — Estimates are billed without checking their status `HIGH` `CONFIRMED` ✅ FIXED

**Closed 2026-08-23.** `POST /api/invoices` now 409s when the named estimate is
not accepted, naming the current status so the operator knows what to do about
it. `estimate_id` means "copy this estimate's lines and ignore mine", so the
estimate it names IS the bill — and the path validated existence, soft-delete
and job scope while never asking about status.

The other two conversion paths already refused (`mobile_invoicing.py:447` and
`estimates.py`'s `/deposit-invoice`). The `jobs.py` one-click half was deleted
outright in the 2026-08-08 audit.

**Scope, stated precisely, because an adversarial review caught the first draft
of this note overstating it.** `estimate_id` has **no frontend caller today**:
the office create page deliberately sends `source_estimate_id` instead (see the
contract comment at `invoices.py:695-706`), and the mobile dialog posts to its
own already-gated endpoint. So this closes the **API surface**, not a live
office hole. It is worth closing regardless, because `estimate_id` copies lines
the operator never sees — the one shape where a wrong estimate bills silently.
The office prefill puts its lines in an editor the operator reads first, and is
filtered to accepted **client-side** at `InvoiceCreateView.vue:761` (added by
the 2026-08-08 audit). That client filter is the office surface's only guard;
if it moves, the prefill can draw from a declined estimate again — visibly, in
an editable editor, but without any server refusal. Worth a server-side answer
eventually; it is not what M23 described.

Guarded by five parametrized cases covering the whole non-accepted half of the
`estimate_status` enum — draft, sent, declined, rejected, expired — plus the
counterfactual (an accepted estimate still bills) and an explicit assertion that
`source_estimate_id` is deliberately NOT gated, because it copies nothing and a
counter sale whose lines originated in a later-revised quote is honest history.
Counterfactually verified: disabling the gate turns all five red.

**Prod exposure when it was closed:** none realised. All 6 estimate-linked
invoices came from accepted estimates, and 0 jobs carried both an accepted and a
non-accepted estimate — but 43 estimates sit in `sent` and 9 in `declined`, so
the shape appears the first time anyone quotes a revision.

The original finding, for the record:

The one-click path picks `order_by(created_at desc).limit(1)` with **no status
filter** ([jobs.py:2612-2624](../../gdx_dispatch/routers/jobs.py#L2612-L2624)), and
`POST /api/invoices` checks only that the `estimate_id` exists and matches the job.

A job with accepted estimate A ($1,400) and a later declined variant B ($2,100) bills
**B**. Mobile invoicing correctly filters `status == "accepted"`.

**Fix.** Filter on accepted (ordering by `accepted_at`) in `jobs.py`, and 409 on a
non-accepted `estimate_id` in the canonical path.

### M24 — Line taxability is lost on conversion `HIGH` `CONFIRMED`

The line copy forwards `category`, `cost_snapshot` and both margin snapshots — but not
`taxable`, which defaults to `True`. Estimates exclude labor from tax when the tenant's
`tax_labor` flag is false; invoices then tax it.

Materials $2,000 plus labor $1,000 at 7.38% quotes $147.60 of tax and bills $221.40 —
**$73.80 over the accepted total**. The per-estimate `tax_rate` override is dropped the
same way, so an estimate deliberately quoted at 0% re-acquires the tenant default.

*Quiet at GDX today* (rate 0), unconditional for any taxed tenant.

**Fix.** Copy `taxable` in the line loop and use `estimate.tax_rate` when present.

### M25 — Smaller pricing items `LOW–MEDIUM`

- **`/api/pricing/calculate` uses markup math on values named "margin"** —
  `cost * (1 + margin)` where the engine means `cost / (1 - margin)`. At 30% on a $100
  cost that is $130.00 versus the engine's $142.86, a 9% revenue gap. The router is
  mounted; its state is in-memory per worker.
- **An explicit 0% margin override is discarded** by
  `line.margin_pct_override or line.margin_pct_snapshot`
  ([estimates.py:1155](../../gdx_dispatch/routers/estimates.py#L1155)) — sell-at-cost
  silently reverts to the tier margin. The very next statement uses the correct
  `is not None` idiom.
- **Rolling-volume discount is configured, cached, previewed in admin — and never
  applied to a real estimate.** Only `price_estimate` consults it, and its sole caller
  is the admin preview.
- **Unit-price rounding drift**: the nested-create path stores an unquantized sell
  price, so `line_total ≠ qty × stored unit_price` and a later description-only PATCH
  moves the total by a cent per line.
- **Change orders**: a bare `amount` PATCH leaves stale line rows and billing follows
  the lines ($500 billed where $700 was approved); amount-only COs show $0 on the
  approval screen but bill amount + tax.
- **Archived labor-matrix rows still price new lines** — `db.get` ignores
  `active=False` and `effective_to`.
- **Double-accept and duplicate-deposit races**: accept and deposit creation are
  read-then-insert with no row lock or partial unique index, so two concurrent accepts
  can mint two deposit invoices and ask the customer to pay twice.

---

## 5. Cost side: vendor bills, payroll, commission

### M6 — `/api/commissions/calculate` mints commission from client input with no role gate `HIGH` `CONFIRMED` ⛔ SUPERSEDED — commission is becoming a plugin

Three money problems in one endpoint
([commission.py:215-266](../../gdx_dispatch/routers/commission.py#L215-L266)):

1. `parts_total` and `labor_total` come from the request body (up to $10M each) — the
   server never derives them from the job.
2. No idempotency and no unique constraint on `(user_id, job_id, period)`, so a
   double-click inserts two entries and `/summary` sums both.
3. The router's only dependency is `require_module("jobs")` — I checked
   ([commission.py:22-26](../../gdx_dispatch/routers/commission.py#L22-L26)). **No
   admin or manager role gate.**

Any authenticated technician can POST their own user id with a $10,000,000
`parts_total` and appear in the commission summary.

**Fix.** Server-derive the totals from the job's invoices, add a role gate, and make
it an upsert on `(user_id, job_id, period)`.

> ### ⛔ DO NOT BUILD THIS. Read before touching any commission code.
>
> **Doug's decision, 2026-08-23: commission is being DROPPED from core and
> rebuilt as a plugin.** Scoped in `commission-as-a-plugin-plan.md`. The
> decision was recorded there and on **M27** below, but not here — so a later
> pass reading this section top-down saw a live `HIGH` finding with a
> prescription and started building it. **That is why this banner exists.**
>
> **What is actually true:** the role gate (`require_permission("payroll.write")`)
> already shipped, so the "any technician pays themselves $10,000,000" attack
> is gone. The remaining two items are NOT to be fixed in core:
>
> - *Server-derive the totals* — needs the deposit basis decided, which
>   `commission-as-a-plugin-plan.md` §3 records as an open business question.
> - *Upsert on `(user_id, job_id, period)`* — **that key is wrong.** An
>   adversarial review proved it silently overwrites: the same person on the
>   same job calculated as `tech` (10%) then `lead` (50%) leaves ONE row at
>   $500 instead of two totalling $600, audited as an innocuous `update`.
>   `CommissionRule` is per-**role** and `CommissionEntry` has no role column,
>   so two genuine earnings collapse onto one key. One person selling *and*
>   installing a job is the ordinary owner-operator pattern here. **The right
>   grain is a money rule the plugin's owner decides — not a constraint.**
>
> **Nothing is at risk today:** prod carries **0 commission entries** and
> **0 commission rules**, and `/calculate` has no UI caller. The feature has
> never once run.
>
> A related sibling, unfixed and also for the plugin: `POST /rules` *does*
> have a button and is a check-then-insert on `commission_rules.role` with no
> unique index, and both `set_rules` and `calculate_commission` then call
> `scalar_one_or_none()` on that role.

### M26 — LLM-extracted bills that fail the arithmetic check report as passing `HIGH` `CONFIRMED`

The invariant check exists to guard untrusted LLM-extracted money. The review queue
reports it like this
([vendor_invoices.py:156](../../gdx_dispatch/routers/vendor_invoices.py#L156)):

```python
invariant_ok=not (invoice.notes or "").startswith("INVARIANT_MISMATCH")
```

But the service builds notes by joining parts with `"; "` and appends the LLM marker
**first** ([service.py:334-343](../../gdx_dispatch/modules/vendor_invoices/service.py#L334-L343)),
so an LLM-extracted bill that fails reads:

```text
LLM_EXTRACTED (llm:claude-haiku-4-5): verify against the PDF; INVARIANT_MISMATCH: header off by …
```

`startswith` returns false, so `invariant_ok` reports **true**. The guard is defeated
for precisely the extraction path it was built to protect — the parser path (which
needs it least) is the only one it works on. The upload response is correct; every
later detail fetch lies.

**Fix.** `invariant_ok = "INVARIANT_MISMATCH" not in (invoice.notes or "")`, and then
store a real boolean column rather than parsing prose for a money guard.

### M27 — Commission revenue counts voided, soft-deleted and draft invoices `HIGH` `CONFIRMED` 🔶 NOT FIXED — SUPERSEDED

**Researched 2026-08-23 and deliberately not repaired.** M27 is real, and it is
the *smallest* of four defects in a feature that has never once run. Fixing it
alone would be worse than leaving it: repairing the invoice predicate on a query
that cannot execute changes nothing, and repairing the query without the
predicate starts paying inflated commission.

What the research found, measured on prod:

1. `_fetch_tech_revenue` selects **`j.assigned_tech_id`, a column that exists in
   no schema here** — the jobs table has `assigned_to`. The resulting
   `OperationalError` was caught and turned into an empty dict, so every tech
   reported **$0.00 revenue and $0.00 commission as though calculated**.
2. It filters **`j.status = 'completed'`** while this tenant stores
   `'Complete'` (32 jobs) and `'Completed'` (17). Even with 1 fixed it matches
   nothing. The codebase has no canonical vocabulary here — `reports.py:856`
   matches only `'Complete'`, `jobs.py:3376` only `'Completed'`/`'completed'`.
3. M27 itself — no `deleted_at`, void or draft filter on the invoice join.
4. **The whole Payroll screen is 501.** `pay-periods`, `pay-stubs` and
   `run-current-period` are all `ui_compat` stubs.

Plus, in the data rather than the code: all 4 configured rates are
`percent = 0.01`, which computes **one hundredth of one percent** — $1.00 on a
$10,000 job. Never noticed, because defect 1 meant the number was never produced.

`commission_entries` = 0 and `payroll_entries` = 0. Nothing has ever been
generated, so no live data depends on any of it.

**Decision (Doug, 2026-08-23): commission belongs in a plugin, not core.**
Scoped in `commission-as-a-plugin-plan.md`. The deposit-basis question this
finding raised ("decide the deposit/final netting basis explicitly") moves there
with it — two prod jobs carry both a deposit and a standard invoice and they
disagree about whether summing is right.

**What shipped instead:** the surface stops lying. `_fetch_tech_revenue` raises
`RevenueBasisUnavailable` rather than returning `{}`, and all three consumers
return **503** naming the reason instead of a page of $0.00 rows. `PayrollView`
says payroll runs are not built, and the button that could only 501 is gone.
Three pre-existing tests that asserted the swallow — including one that "proved"
the CSV export worked while exporting a file derived from a raised query — now
assert the refusal.

The original finding:

`_fetch_tech_revenue` ([payroll.py:304-316](../../gdx_dispatch/routers/payroll.py#L304-L316))
joins invoices with no `deleted_at`, no void and no draft filter, while
`job_costing._invoiced_for_job` does filter. An $8,000 invoice voided and re-issued
counts twice: a 5% tech earns $800 instead of $400. Deposit plus final on one job
inflates the basis the same way.

**Fix.** Add `i.deleted_at IS NULL AND i.status NOT IN ('void','draft')`, and decide
the deposit/final netting basis explicitly.

### M28 — `job_costing` re-rates a deliberate $0 labor rate to $95/h `MEDIUM-HIGH` `CONFIRMED`

`rate = Decimal(str(r[1] or DEFAULT_LABOR_RATE))` — and `0 or 95` is 95
([job_costing.py:201](../../gdx_dispatch/routers/job_costing.py#L201)). `labor.py`
fixed this exact trap and says so in a comment ("stored rate wins — INCLUDING a
deliberate $0"); `job_costing` still has it.

A warranty job with a 3-hour entry at a deliberate $0 rate costs $0 on one endpoint
and **$285** on the other, and the profitability report ranks it a loser. Three
different default rates disagree across the codebase ($95 in job_costing, $65 in
labor.py, and two labor.py endpoints skip the tenant fallback entirely), so the same
entry can display three different costs.

**Fix.** `rate = fallback if r[1] is None else Decimal(str(r[1]))`, and unify the
fallback into one source.

### M29 — Voiding a vendor invoice after confirming its lines reverses nothing `MEDIUM` `CONFIRMED`

PATCH flips status to `void` with no check on confirmed lines and no compensating
action. Confirmed lines have already created `Expense` rows, incremented stock, and
created billing rows. Voiding a $3,120 bill leaves all three behind, and the dedup
index keys on `deleted_at` rather than status — so the corrected re-issue imports
cleanly and the costs exist twice.

**Fix.** Block the void while confirmed lines exist, or generate reversing expense and
stock adjustments.

### M30 — Smaller cost-side items `LOW–MEDIUM`

- **PO receive books full quantity unconditionally** — `line.quantity_received = line.quantity_ordered`, so a short shipment (6 of 10) overstates inventory by 4 × unit cost. Two of the three PO systems are dead code; the unmounted one's `receive_po` has no already-received guard at all.
- **Midwest statement parser silently drops credit rows**: `\$[\d,]+\.\d{2}` cannot match `-$123.45` or `($123.45)`, non-matching lines `continue` without error, and there is no printed-total reconciliation (unlike the invoice parser). A −$2,900.98 credit row vanishes and the payable is overstated by that amount.
- **Variance report sums estimate lines across all estimates of a job** — every revision and declined draft counts, so a thrice-quoted job compares actuals against triple the estimate.
- **Weekly overtime is computed only over the queried window**, so a 50-hour week straddling a month boundary splits as 30 regular / 0 OT instead of 40/10. The CSV's `gross_pay` column is commission only.
- **Labor-variance "actual" is unclamped wall-clock**: `completed_at − arrived_at` with a floor but no ceiling, so an assignment closed a week late books 168h × $38 = $6,384. This is the "elapsed is not evidence" failure mode again — report-only, but exactly the shape that once implied ~$180k on one job.
- **Budget quick-fill divides by the fixed lookback window** — the same "3× understated" bug the file's own comment flags as fixed elsewhere.
- **Dealer order totals trust client unit prices** and never consult the catalog or tier pricing; orders carrying a PO number crash on a dict-iteration bug.
- **Vendor-invoice confirm truncates fractional quantities** (`2.5 → 2`) and can write negative expenses the Expense API's `gt=0` forbids.

---

## 6. The general ledger

The ledger is the best-built money code in the repo, and I verified its core myself
rather than taking it on trust. `to_cents` rejects floats and bools loudly and rounds
`ROUND_HALF_UP`; `allocate` is sum-preserving by construction using exact `Fraction`
arithmetic with a deterministic tie-break, so a proration cannot create or destroy a
cent. The balance invariant is enforced in Python *and* by a deferred Postgres
constraint with immutability and txid sealing, and no code constructs journal entries
outside `engine.py`. Sign conventions are correct across issuance, payment, credit
memo, credit application, and the opening-balance events.

Everything below is **latent** — `ledger_posting_enabled` defaults off and is off in
prod. These need to be fixed before the CPA review, not before the next deploy.

- **Refunding an overpayment double-dips** `HIGH` — `post_refund` always debits
  contra-revenue and never touches the 2300 customer-credit liability. Refunding a
  $150 payment on a $100 invoice books revenue of −$50 *and* leaves a $50 spendable
  credit the customer can apply to their next invoice. That is $50 of real money out
  the door once the flag is on.
- **Cash-basis proration drops negative invoice lines** `MEDIUM` — deposit-netting
  lines are excluded from the weights, so weights exceed the invoice total. A $1,000
  job netted by a $300 deposit with 7.375% tax overstates cash-basis revenue by
  **$20.61** and under-attributes tax identically. Zero drift only when tax is 0.
- **Per-event allocation drifts per-account cents** `LOW` — each partial payment
  allocates independently rather than cumulative-minus-already-allocated, so
  individual accounts drift up to a cent. Totals stay exact.
- **`MAX_RESIDUAL_CENTS = 100`** absorbs up to $1.00 per invoice into 6990 Rounding
  Differences. For app-minted invoices the residual is structurally zero, so any
  non-zero value is a composition error being silently swallowed.
- **The payment-method role map accepts any role**, including AR — mapping "check" to
  AR posts a self-cancelling wash that hides the payment from the GL entirely.
- **Reversals default to the original entry's effective date**, silently restating a
  prior period unless it happens to be locked.
- **Backfill skips voided invoices**, losing the compensating entry for an opening-era
  payment that was voided after cutover.

---

## 7. Frontend

The important architectural result first: **no frontend surface submits a
client-computed grand total, subtotal or tax_amount.** Invoices, estimates, change
orders and purchase orders all POST `{lines[], tax_rate, discount}` and let the server
recompute. There is no `0.1 + 0.2` class of persisted error, no client-side
dollars↔cents conversion anywhere (portal and deposit payments use server-minted
hosted `pay_url` links), and no money `parseFloat`. `useFormatters.js` is solid —
`Intl`-based, with null and NaN rendering as an em dash, so `$NaN` cannot reach a
screen.

What is left is a narrower and more interesting class: **mutations applied at submit
time that make the persisted invoice differ from the total the operator approved on
screen.**

### M31 — A cleared quantity becomes 1 at submit, billing a line the on-screen total excluded `HIGH` `CONFIRMED`

[InvoiceCreateView.vue:481](../../gdx_dispatch/frontend/src/views/InvoiceCreateView.vue#L481):

```js
quantity: toNum(l.quantity) > 0 ? Number(l.quantity) : 1,
```

PrimeVue's `InputNumber` leaves the model **null** when the field is cleared — the
`:min="1"` constraint does not apply to empty, and the codebase documents this exact
behaviour elsewhere ("a transiently CLEARED price input (null, mid-retype)").

So a line "Opener install" with the quantity cleared and a unit price of $650 renders
its line total as `$0.00` and is excluded from the on-screen subtotal. The submit
filter keeps it (it has a description and a price above zero) and substitutes
`quantity: 1`. **The invoice is created $650 higher than the total the operator
approved.**

**Fix.** Refuse the submit, or visibly drop the line, when a kept line has no positive
quantity. Never substitute 1 silently.

### M32 — Bulk "Mark Paid" posts stale client-side balances `MEDIUM-HIGH` `CONFIRMED`

[BillingView.vue:758-771](../../gdx_dispatch/frontend/src/views/BillingView.vue#L758-L771)
posts `amount: balance` where `balance` comes from the row loaded into the browser,
possibly minutes earlier. Since the server-side overpayment gate is dark (M11), any
amount is accepted.

INV-0102 totals $1,000. User A records a $400 check. User B's billing tab still shows
a $1,000 balance and bulk Mark-Paid posts $1,000 → **$1,400 recorded against a $1,000
invoice**, clamped invisible. `MobileBillingView.vue` has the same pattern with a
smaller window.

**Fix.** Add a server-side "pay remaining balance" mode that takes no amount, or send
`expected_balance` and return 409 on mismatch. The first is better — it removes the
client from the arithmetic entirely.

### M33 — The submit filter drops lines the on-screen total includes `MEDIUM` `CONFIRMED` (filter) / `PLAUSIBLE` (negative source)

`.filter((l) => l.description && toNum(l.unit_price) > 0)` — used identically in
`InvoiceCreateView`, `EstimateView` and `ChangeOrdersView` — drops zero and negative
lines, while the displayed subtotal sums all of them. `prefillFromJobEstimate` copies
`unit_price` verbatim, so a −$50 coupon line prefilled from an estimate shows in the
on-screen total and vanishes from the payload: screen $450, invoice $500.

Related and latent: invoice edit mode clamps negative prices with
`Math.max(0, toNum(ln.unit_price))`
([InvoiceDetailView.vue:1436](../../gdx_dispatch/frontend/src/views/InvoiceDetailView.vue#L1436)),
and the `changed` diff then flags the line as modified even on an untouched save, so
fixing a typo elsewhere PATCHes a −$100 promo line to $0 and raises the balance. Only
the deposit-netting line is exempt — and its comment shows the hazard was understood
for that one case and not generalized. I verified that **the only in-app writer of a
negative `unit_price` is the deposit-netting line**, which is exempt, so this is
latent today; it becomes live the moment any other negative line exists (a QB-imported
discount line being the likely first).

**Fix.** Make the filter and the display agree — either both include zero/negative
lines or both exclude them — and restrict the clamp to genuinely new lines.

### M34 — Smaller frontend items `LOW–MEDIUM`

- **Invoice prefill hardcodes labor as non-taxable** rather than reading the tenant's
  `tax_labor` setting the way `EstimateView` does. Under-collects on a tax-labor
  tenant; irrelevant at GDX.
- **Mobile mark-paid's fallback balance ignores credit memos** —
  `balance_due ?? (total − amount_paid)` omits adjustments, unlike its desktop mirror,
  and leans on the deprecated `amount_paid` (see M35).
- **`ProposalsView` submits price fields as raw strings** from `InputText type="number"` —
  the only view in the app not using `InputNumber`. Empty sends `""`.
- **Estimate autosave can PATCH `quantity: null`** — the guard checks `unit_price` but
  not quantity.
- **Display-only rounding drift**: the create view rounds tax with JS `Math.round`
  while the server uses Decimal `ROUND_HALF_UP`, so half-cent cases preview a cent off
  from the invoice actually created.
- **Custom `$${n.toFixed(2)}` formatters** in six mobile components lack thousands
  separators and render negatives as `$-50.00` rather than `-$50.00`, contradicting
  the sign convention `money_format.py` establishes server-side. Cosmetic; all guard
  NaN.

### M35 — `amount_paid` is written by nothing and read by five surfaces `MEDIUM` `CONFIRMED` ✅ FIXED

> **FIXED 2026-08-22.** Every live reader now derives paid-to-date from the
> `payments` table via `core/invoice_paid.py`
> (`paid_to_date` / `paid_to_date_bulk` / `paid_amount_sq`), the same rule
> `_recalculate_invoice` already used: `Σ payments WHERE voided_at IS NULL`.
>
> This finding listed five surfaces; there were **seven**. The two it missed:
> `core/closeout_billing.py:291` — `is_untouched_autodraft`'s payment arm, a
> guard that could not guard because the column was always 0, so an autodraft
> carrying real money still looked like the machine's to void — and
> `job_display_state.py:160`, the `deposit_paid` badge (distinct from the
> `partially_paid` arm at :206 the finding did cover).
>
> Measured on prod 2026-08-22 before the fix: **24 invoices, $62,473.72 of
> drift, every one understating**; 24 of the 27 payments involved were recorded
> after the 2026-07-31 repair. Worst row: invoice 50010651, total $15,476.93,
> status `paid`, real payments $15,476.93, `amount_paid` **0.00**.
>
> **Correction.** An earlier draft of this entry claimed MobileBillingView
> rendered that invoice as "Paid $0.00" to a technician. It did not — the
> adversarial audit caught the claim as unverified and it was wrong. The screen
> loads from `/api/invoices/{id}`, whose serializer carried **no `amount_paid`
> key at all**, and the row is gated on `detail.amount_paid != null`, so it
> never rendered. That is a real defect too, and the same change fixes it: the
> detail payload now carries a true paid-to-date derived from the payments it
> already loads, plus `credit_total`, so `total − paid − credits == balance_due`
> reconciles on screen instead of leaving a credited invoice showing money
> unaccounted for. Nothing here was browser-walked before the claim was made;
> it is now stated only as far as the code shows.
>
> Guard: `tests/test_m35_amount_paid_retired.py`. Six behavioural tests record a
> payment WITHOUT touching the column and assert the surface reports it, plus a
> tokenizer-based scanner that fails if any live path reads the attribute again
> (it correctly flags all seven pre-fix sites when the source is reverted; it
> ignores comments, docstrings, and the API payload key of the same name, which
> is now sourced from payments).
>
> **The column is now dropped** — migration `073_drop_dead_money_columns`,
> which also takes `invoices.total_amount` (M8's column) and `jobs.dispatched_at`
> (defined once, never written, never read, NULL on all 275 prod rows). The
> audit's own words about total_amount applied to all three: "a column nothing
> writes and five things read is a trap that keeps re-firing."
>
> Rollback rebuilds `amount_paid` from the payments table rather than restoring
> the drift — a downgrade should leave the column better than it was. Verified
> upgrade → downgrade → upgrade on BOTH dialects against real engines; the first
> draft rolled back to all-zeros on SQLite because only the Postgres branch had
> the rebuild, which a guard now catches.


Not strictly frontend, but this is where it surfaces. `Invoice.amount_paid` is
deprecated — `_recalculate_invoice` deliberately ignores it
([invoices.py:246](../../gdx_dispatch/routers/invoices.py#L246)) and no live path
writes it. The QB repair tool set it correctly as of 2026-07-31; every payment
recorded since has left it stale.

Five surfaces still read it: [jobs.py:3284](../../gdx_dispatch/routers/jobs.py#L3284)
(`SUM(amount_paid)` as "total_paid"), [jobs.py:515-524](../../gdx_dispatch/routers/jobs.py#L515-L524),
[reports.py:1138](../../gdx_dispatch/routers/reports.py#L1138),
[mobile_invoicing.py:131](../../gdx_dispatch/routers/mobile_invoicing.py#L131), and
[job_display_state.py:115](../../gdx_dispatch/core/job_display_state.py#L115).

**Fix.** Either maintain it in `_recalculate_invoice` (one line:
`invoice.amount_paid = paid_amount`) or migrate the readers to Σ(non-voided payments)
and drop the column. Do not leave it half-alive — a column nothing writes and five
things read has now caused two separate findings.

---

## 8. More invoice-lifecycle findings

These belong with §1 by subject, but they are guards and plumbing rather than
totals math, so they are grouped here.

### M36 — The idempotency middleware is a permanent pass-through `HIGH` `CONFIRMED`

[core/middleware/idempotency.py:68-70](../../gdx_dispatch/core/middleware/idempotency.py#L68-L70)
bails out when `request.state.principal` is None:

```python
principal = getattr(request.state, "principal", None)
if principal is None:
    return await call_next(request)
```

I grepped every assignment of `state.principal` in the repo. **The only one is in
`tests/test_idempotency_middleware.py`.** No production code ever sets it, so the
middleware returns early on every request and the replay cache has never functioned.

This matters most for the mobile offline queue, which sends an `Idempotency-Key`
header on replayed requests ([useOfflineSync.js:169](../../gdx_dispatch/frontend/src/composables/useOfflineSync.js#L169))
in the belief that it is honored. A cash payment recorded from a truck, whose response
was lost, replays and is recorded twice.

Note this is a *different* layer from the Stripe idempotency keys (which are forwarded
to Stripe and do work) and from M2 (which is a database-level race). All three need
fixing; none substitutes for the others.

**Fix.** Assign `request.state.principal` in the auth dependency, or re-key the
middleware off the JWT it already has access to. Then add a test that asserts a
replayed POST is served from cache in a *production-wired* app, not a hand-built one.

### M37 — Deleting a draft invoice orphans its applied payments `MEDIUM` `CONFIRMED`

`delete_invoice` ([invoices.py:1270-1322](../../gdx_dispatch/routers/invoices.py#L1270-L1322))
checks only `status == "draft"`. It carefully releases parts and change orders back to
the unbilled pool — and never looks at payments. Meanwhile `record_payment` blocks
only `void`, so recording a partial payment on a draft is legal and leaves it a draft.

A $500 draft with a $200 check recorded is deletable. The invoice soft-deletes, the
`Payment` row survives, and every AR surface joins through non-deleted invoices —
**$200 of real cash disappears from the books** while staying in the database.

`void_invoice` has exactly the right guard four hundred lines away.

**Fix.** Mirror it: 409 the delete when non-voided payments exist.

### M38 — Mobile invoice creation has no double-billing guard `MEDIUM` `CONFIRMED`

The desktop create path 409s on an existing live invoice for the job
([invoices.py:678-699](../../gdx_dispatch/routers/invoices.py#L678-L699)) and the
one-click path has the same guard. The mobile path has neither. Parts are
stamp-protected and deposit netting has a prior-application guard, but the closeout
labor line is re-derived on every call.

A tech tapping Generate twice on a slow connection on a 3h × 2-tech service call
produces two drafts each carrying the same $600 labor line. Only the §11
office-verification gate stands between that and the customer — which is a review
step, not an invariant.

**Fix.** Mirror the desktop guard, or make the endpoint idempotent per
`(job, closeout)`.

### M39 — Endpoints that report success without doing anything `LOW` `CONFIRMED`

Not arithmetic errors, but they lie about money:

- `create_payment_plan` ([invoices.py:2571-2602](../../gdx_dispatch/routers/invoices.py#L2571-L2602))
  computes an installment schedule, **persists nothing**, and returns a `plan_id` that
  does not exist.
- `send_payment_receipt` ([invoices.py:2609-2642](../../gdx_dispatch/routers/invoices.py#L2609-L2642))
  returns `sent: True` without sending any email, and logs the deprecated
  `amount_paid` as the amount.
- `batch_create_invoices` mints random `INV-<hex>` numbers with $0 totals and no
  lines.

---

## 9. Suggested order of work

### Today, before the office records more backfill payments

- **M1** — lock totals on QB-imported invoices. Then survey how many invoices already
  drifted and restore them from QB.
- **M11** — surface overpayment. It is small, and it is the detection net that tells
  you whether M2, M5, M12, M32 and M36 have already fired in prod. Build it first and
  the rest of this list gets evidence instead of speculation.

### This week

- **M2** and **M36** — the partial unique index on `(invoice_id, reference)`, and
  wiring `request.state.principal` so the idempotency middleware stops being
  decorative. Different layers, same failure.
- **M4** and **M5** — hardcode the currency; delete the portal total-fallback. A few
  lines each, both live money.
- **M6** and **M26** — the commission role gate, and the one-character `invariant_ok`
  fix.
- **M3**, **M14**, **M15** — make reversal handling amount-aware and bidirectional.
- **M37** — the missing payment guard on draft delete. One condition, copied from
  `void_invoice`.

### Next

- **M7** with **M22**, **M23** and **M24** as one piece of work: a single
  "materialize estimate totals as invoice lines" helper carrying discount, taxability,
  rate and the accepted tier. These are four symptoms of one missing abstraction, and
  fixing them separately will leave the seams.
- ~~**M8**, **M18**, **M19**, **M20** — the reporting cluster, sharing one
  "net adjustments per period" join.~~ ✅ Done 2026-08-22. The shared join is
  `_credits_by_period` / `_credits_total` in `routers/reports.py`. Only M18's
  tax-component half remains, and that is a decision plus a migration.
- ~~**M35** — resolve `amount_paid` one way or the other.~~ ✅ Done 2026-08-22:
  readers migrated to Σ(non-voided payments); the column drop ships separately.
- The GL items in §6, gated on the CPA review rather than on a deploy.

### Does fixing all of this make the math correct?

No — and the distinction matters enough to state plainly.

Fixing these findings removes known defects. It does not establish correctness,
for three reasons:

1. **An audit is a search, not a proof.** Nine of the findings are now proven by
   executable tests. The other thirty are code-traced, and the whole set is bounded
   by what one day of reading happened to look at. Two candidate findings were
   rejected during verification (§10), which tells you the process has a nonzero
   error rate in both directions.
2. **Nothing was checked against production data.** For M1 especially, the code fix
   stops future damage and says nothing about invoices whose totals were *already*
   rewritten by a backfill payment. The code can be right while the books stay wrong.
3. **Nothing prevents the next violation.** This is the important one. The ledger has
   a real invariant — balance is enforced in Python *and* by a Postgres constraint, so
   an unbalanced entry cannot be written. The invoice side has no equivalent:
   `total = Σlines + tax` is an assumption `_recalculate_invoice` makes, not a rule
   the system enforces. That is precisely why five different callers ended up
   violating it. Fix all five and the sixth is still possible.

Some of it also isn't a math question. Whether deposits, credit memos and
supersessions are *accounted for* correctly — revenue recognition, cash versus
accrual, what belongs in a remittance bucket — is a domain judgment that belongs to
the CPA review, not to this document.

**What would move toward correctness**, in rough order of leverage:

- **Enforce the totals invariant** the way the ledger enforces balance — a check
  constraint or a flush guard that rejects any invoice where
  `total ≠ Σ active lines + tax`. This converts a whole bug class into an
  impossibility. Everything that has to bypass it (imported invoices, per M1) then
  has to say so explicitly, which is the honest design anyway.
- **Make errors visible** (M11). Overpayment detection is the cheapest instrument on
  this list and it retroactively answers "has this already happened in prod?" for
  M2, M5, M12, M32 and M36 — a question no amount of code reading can settle.
- **Grow the probe suite into a real gate.** Differential tests — the same estimate
  through all three invoice paths, asserting identical totals — would have caught M7,
  M9, M10, M22 and M24 as a group, without anyone needing to think of them
  individually. That is the difference between fixing five bugs and closing the hole
  that produced them.
- **Reconcile against prod** read-only: imported invoices where
  `total ≠ SUM(line_total)`, invoices where `Σpayments > total`, payments sharing a
  reference. Three queries, and they convert this document from prediction to
  inventory.

### One theme worth naming

Most of the serious findings are not arithmetic errors. The arithmetic in this
codebase is good — Decimal with explicit `ROUND_HALF_UP` on the invoice side, exact
integer cents with a sum-preserving allocator on the ledger side. The failures are
**invariant violations by callers**: `_recalculate_invoice` guarantees
`total = Σlines + tax`, and M1, M7, M9, M10 and M22 are all paths that store a total
that isn't that, then get corrected by the first recalc into something nobody
approved.

The durable fix for that whole class is to make hand-set totals impossible — represent
every adjustment (discount, deposit netting, credit) as a line, and let recalc be the
only writer of `total`. Deposit netting already works this way and is the one part of
the conversion story with no findings against it.

## 10. Rejected findings (checked, not bugs)

Recorded so nobody re-investigates them. Several of these looked like real bugs until
traced:

- **Frontend "floors fractional quantities"** — `Math.floor` on the invoice edit path
  looks like it truncates a 2.5-hour line to 2, but `InvoiceLine.quantity` is an
  **Integer** column ([tenant_models.py:499](../../gdx_dispatch/models/tenant_models.py#L499))
  and no path stores a fractional quantity, so the floor is a no-op. The
  `Math.max(1, …)` half of the same expression is real — see M31.
- **AR aging `amount_paid` fallback** ([reports.py:1157](../../gdx_dispatch/routers/reports.py#L1157)) —
  `inv.balance_due or (total − amount_paid)` looks like a falsy-zero trap, but the
  query already filters `balance_due > 0`, so the fallback is unreachable.
- **`void_invoice`** refuses while non-voided payments exist and reverses adjustments
  correctly. (Contrast M37 — `delete_invoice` is the one missing this guard.)
- **`issue_credit_memo`** recalculates first, caps at the remaining balance, and blocks
  drafts.
- **`collections.py` aging** uses `balance_due` only, with correct lowercase statuses.
- **Cents conversion across the Stripe paths** — `int(round(float(balance) * 100))` is
  correct for `Numeric(12,2)` values; there is no truncating `int(x * 100)` anywhere.
- **The statement-import tie-out** is genuinely thorough: signed integer cents
  throughout, no float, no amount tolerance in the matcher (exact equality only),
  complete summary/daily/ending reconciliation, and zero lines inserted on failure.
- **The `#268` hardening** holds where applied — token scoping, server-derived amounts,
  ACH SetupIntent binding, signature verification failing closed.
- **Deposit module** netting, capping and non-taxable netting lines are correct.
- **`hide_line_prices`** is purely presentational and never enters stored math.
- **Frontend formatters and totals submission** — no surface POSTs a client-computed
  grand total; `useFormatters.js` cannot emit `$NaN`; no money `parseFloat`; no
  client-side cents conversion.

## 11. Coverage and limits

Audited: invoices, payments/Stripe/portal, the GL, sales tax and reports, estimates
and pricing, change orders, deposits, bank feeds and statement import, QuickBooks,
vendor invoices/statements/orders, purchase orders, expenses, job costing, payroll,
commission, budgets, overhead, and the Vue frontend.

Nine findings (M1 ×2, M2 ×2, M7, M9, M11, M24, M37) are proven by the probe suite in
§0.5. The remaining thirty are code-level traces, and the two labeled `PLAUSIBLE`
(M14 and the negative-line half of M33) depend on conditions I could not confirm from
the repo — Stripe event ordering, and whether QB-imported discount lines carry
negative amounts.

**Nothing in this audit was verified against production data.** Not even the nine
proven ones: the probes prove the *code* misbehaves, not how many prod rows have
already been damaged by it.

Before acting on M1 in particular, query prod read-only to size the affected set: the
fix is safe either way, but the cleanup depends on how many imported invoices have
already been recalculated by a backfill payment.

Two areas I deliberately did not chase: the plugin money code
(`gdx-plugin-chi-pricing`, which is git-ignored by design and not part of the shipped
app) and the demo seed paths, beyond noting that
[onboarding.py:330](../../gdx_dispatch/routers/onboarding.py#L330) creates a `sent`
demo invoice with a $285 total and no `balance_due`, which reads as paid to every
report. Demo-only, but the same shape as a real bug.
