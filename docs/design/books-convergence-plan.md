# Books Convergence — from bank evidence to actual books

Status: **PARTIALLY BUILT** (verified on main 2026-08-21). Track 1 shipped as
#321: migration 065 `vendor_bill_payments`, `modules/vendor_invoices/payments.py`
with `effective_expense_date` + `sync_expense_dates`, and the confirm-effects
in `statement_matching.py`.
**Track 2 item 4 — BUILT, RELEASED v1.76.0** (#407, deployed to prod
2026-08-23 and walked). The Feed Transactions tab carries a Statement column:
`matched` / `statement_verified` / `ambiguous` / `unmatched` / `feed_only` /
`no_amount` / `unlinked` / `pending`.
Two things the plan did not anticipate, both recorded because they change what
"pair the way the tie-out does" means:
1. **There was no account pairing to reuse.** `bank_accounts` is keyed on
   institution + last4 and `bank_feed_accounts` on connection + external id,
   with no link between them, and on this tenant every SimpleFIN row carries an
   EMPTY `account_number_masked`. Migration 074 adds a nullable
   `bank_feed_accounts.bank_account_id`, set by an operator from a picker —
   inferring it from a renameable display name was rejected.
2. **"Amount + date ±1" as an existence test is wrong.** Answered per
   transaction it let two identical charges against ONE statement line both
   report `statement_verified` — a duplicate bank charge showing as two green
   tags. Pairing is an assignment over a bipartite graph; contested claimants
   report `ambiguous` rather than a coin-flip winner.
Live prod after linking: 149 `statement_verified`, 62 `feed_only`, 0
`unmatched`, 0 `ambiguous` across 211 posted transactions.
**⛔ Won't build (QB phase-out 2026-08-21):** Track 2 item 5, QB mirror rows as
transition-mode match candidates. The mirror stopped growing in May 2026 and
the connection is dead, so matching against it has a shrinking floor. Track 4
("QBO stays in sync") goes the same way — §5's decision that QBO is the tax
book *this year* is about filing, not about keeping a synced second ledger.
Track 3 is not complete; see `gl-phase1-core-ledger.md` for what the GL side
actually did ship.
Date: 2026-08-14

> Repo hygiene: no institution, supplier, or processor names in this doc or any
> implementation PR. Identifiers live in tenant DB settings.

## 1. The goal, stated plainly

Three sentences Doug said, translated to requirements:

1. **"Actual books that work"** — the internal GL becomes trustworthy and live: every
   economic event posts, bank balances reconcile against bank evidence, periods tie out
   and lock.
2. **"QuickBooks should still be able to sync and everything should match up"** — QBO
   remains a maintained second book through the transition (tax book), with GDX↔QBO
   divergence *detectable*, not assumed away.
3. **"We should be able to tell if something matches from our bank feeds and QuickBooks
   and how it is labeled as an expense"** — for any bank transaction, one screen answers:
   is it matched? To what (payment / expense / vendor bill / QB record)? What
   category/GL account is it labeled as?

## 2. Where we actually are (verified 2026-08-14)

Three bank stores, no cross-links:

| Store | Tables | State |
|---|---|---|
| Live feed (SimpleFIN/Banno) | `bank_feed_accounts/_transactions` | shipped v1.56.0; shared table, `provider` column |
| Statement evidence | `bank_statement_imports/_lines` | shipped v1.36.0, matcher + Reconcile tab live |
| QBO mirror | `qb_bank_transactions` etc. | populated by (now-paused) pulls |

What exists and is load-bearing:

- Statement matcher (`modules/bank_feeds/statement_matching.py`) — R2/R3/R5 rules,
  suggest-and-confirm, matches statement lines to **operational records**
  (Payment / Expense / VendorInvoice) via `bank_match_externals`.
- Feed↔statement **tie-out endpoint** (`simplefin_router.py:559`) + daily balance
  snapshots — the feed and the statement evidence already cross-check each other.
- GL through Phase 1 S11, dark behind `gl_settings.ledger_posting_enabled`. Posting
  rules exist for invoice / payment / credit / refund / adjustment / manual expense.
- Vendor bill → Expense bridge (`modules/vendor_invoices/confirm.py`) creates `Expense`
  rows with `vendor_invoice_lines.expense_id` FK.

The seams (each one is a work item below):

- **S1** Confirming a bank match mutates nothing (`statement_models.py:238` — metadata
  only, by design "in this phase"). No bill marked paid, no expense created, no entry
  posted.
- **S2** ~~No code path sets a vendor bill `paid`~~ (audit correction: the generic
  status PATCH + the detail-page Mark-paid button DO set it — free-string, no date, no
  amount, no record). The real gap: no payment *records*, no partial concept, and the
  historically-marked-paid population has zero payment children — any derived status
  must backfill or precedence-rule them or they all revert to open.
- **S3** `confirm.py` never calls `post_expense_recorded` — vendor-bill expenses skip
  the GL while manually keyed expenses post. Asymmetric on identical events.
- **S4** No bank event posts to the GL; no `bank_transaction` `source_type`;
  `bank_accounts.gl_account_id` migrated but unread. GL account 1000 is never
  reconciled against bank evidence.
- **S5** Feed Transactions tab and Reconcile tab show disjoint data — a feed
  transaction displays no match/label status.
- **S6** QB mirror rows are not match candidates; QB↔bank↔books comparison exists
  nowhere in UI.
- **S7** QBO push is incomplete: no `push_payment`, `push_invoice` create-only, no
  ItemRefs (per Phase 2 audit).

## 3. What is deliberately preserved

- **Feed never writes books** (SimpleFIN plan §3). The feed stays visibility +
  suggestions; the statement remains THE reconcile evidence. Nothing below changes this.
- **Suggest-and-confirm everywhere.** No rule auto-mutates books; confirms are explicit,
  reversible, and audited. Ambiguity refuses to manual.
- **Era-by-date GL semantics** and the Phase 1 cutover model.

## 4. The tracks

### Track 1 — Confirmed matches act on the books (no GL-flag dependency)

The single highest-leverage change: S1+S2+S3. Builds directly on the shipped matcher
and on `vendor-payment-visibility-plan.md` §A.

1. **PR: vendor bill payment concept.** `vendor_bill_payments` child records
   (amount, date, source: manual | statement_match | statement_diff), derived
   paid/partial status on the bill; bulk mark-paid. The statement-diff engine's
   settled/paid-down signals become one-click suggestions (plan A).
2. **PR: confirm-actions on bank matches.** Confirming a debit↔vendor-bill match
   records a `vendor_bill_payment` (source: statement_match, dated by the bank line).
   Confirming a debit with no candidate offers **"create expense from bank line"** —
   prefilled amount/date/description, category picker required (this is the "how is it
   labeled" moment). Unconfirm reverses what confirm created.
   Permissions (audit correction — the original claim here was wrong): match
   confirm/unconfirm stays `accounting.write` (its existing gate); bill-payment
   record/void goes under `vendor_invoices.write` (the current Mark-paid gate). NO
   new permission key — avoids the tenant_roles snapshot trap entirely.
   Audit conditions applied: mutation in the same transaction as the status flip;
   re-confirm is a no-op; `vendor_bill_payments.match_id` provenance + partial-unique
   guard; `create_manual_match` (born-confirmed) runs the same effects; auto-record
   only for single-bill, non-overpaying matches (multi-bill or over-balance →
   metadata-only with a loud note); voiding a match-created payment requires
   unconfirm-first; unconfirm voids the created payment (skip if already voided) and
   soft-deletes a created expense only if unmodified — modified expenses detach and
   survive; `broken_matches` learns the voided-created-payment case; existing paid
   bills get a backfill payment record in the migration so derivation never reverts
   them; PATCH status='paid' is retired in favor of the payment endpoint (single
   writer).
3. **PR: symmetric GL posting.** `confirm.py` calls `post_expense_recorded` for the
   expenses it creates (no-op while the flag is off). Audit conditions applied: the
   confirm path gets the `_post_or_409` error seam, pre-cutover-dated expenses skip
   posting (era-by-date, matching the backfill's rule).
   ~~CPA item: receipt vs payment timing~~ **RESOLVED (Doug, 2026-08-14): payment
   date, cash basis.** Implemented in the follow-up PR: vendor-bill expenses are
   dated by `payments.effective_expense_date` — the settlement date once the bill is
   fully paid (a bank-match payment carries the literal bank date, so a settled
   bill's P5 credit lands when cash left; an UNPAID confirmed bill still posts at
   the invoice-date placeholder, so GL Operating Bank differs from the real bank by
   exactly open confirmed A/P until settlement — the tie-out will show it), else
   the invoice date as a placeholder;
   `payments.sync_expense_dates` re-dates + reposts (flag-gated, era-guarded) on
   every payment record/void, so settlement moves the expense and un-settlement
   moves it back. Backfilled date-unknown payments never re-date (no fictional
   dates). Partial payments keep the placeholder until the bill settles — the
   recognition date is the settling payment's date (documented simplification).
   Locked periods: a settlement whose OLD entry sits in a locked month posts its
   reversal at the target date (reverse_entry's escape hatch — locked amounts are
   countered in the open period, never edited); recording a payment DATED into a
   locked month refuses as a 409. No history sweep is needed at deploy — VERIFIED
   on prod 2026-08-14: `vendor_bill_payments` does not exist there yet (065 is
   unreleased), so zero payments predate this rule. Caveat: that stays true only
   if Track 1 (#321) and the cash-basis PR ship in the SAME release; deploying
   #321 alone first and recording dated payments before this lands would create
   settled bills with invoice-dated expenses that nothing re-dates. Re-dating is the rule's writer of record for
   vendor-bill expense dates — a hand-edited date on one of these expenses is
   overwritten by the next payment event on its bill (edit the payment, not the
   expense, to move recognition).

Exit criterion: the office can work the Reconcile tab and the books actually change —
bills go paid with dates, bank-only debits become categorized expenses.

### Track 2 — One screen tells the truth (bank feed ↔ statements ↔ QB ↔ label)

4. **PR: match status on the feed.** Pair feed transactions to statement lines the way
   the tie-out already does (account pairing + amount + date ±1). Feed Transactions tab
   gains a status column: *statement-verified / matched → [record + category] /
   unmatched / feed-only (awaiting statement)*. No new FK; pairing is computed, like
   the tie-out.
5. **PR: QB mirror as transition-mode candidates** (Phase 2 §6.1). Canonicalized
   `qb_*` rows become secondary match candidates; a bank line can then show "also in
   QB as [type]" and — the reverse — QB mirror rows not traceable to anything in GDX
   surface as divergence items. This is what "tell if something matches from QuickBooks"
   means concretely.

### Track 3 — GL goes live (bank-anchored)

Blocked on the two Phase 2 pre-conditions, both non-code:

- §9.1 re-verification of the [SOURCED] claims (research runs).
- CPA review: Phase 1 account mapping, cash-basis timing items, loan-split cadence.
- Plus the owed headed walk of `/accounting-ledger` on real data.

Then, per Phase 2 §2–3 (already designed and audited):

6. **PR: posting-on-confirm** (P10/P12): R5 rules for fees/interest post entries;
   `bank_transaction` becomes a real `source_type`; wire `bank_accounts.gl_account_id`.
7. **PR: tie-out worksheet + period locks** (§2.2 state machine): statement ending
   balance = GL balance ± uncleared, per account-month; tied-out months lock.
8. Flag flip on the CPA's go, oldest-first historical tie-outs after the (likely no-op)
   processor catch-up check (§4.4).

Vendor-bill A/P accrual stays **out of scope** (cash-basis via Track 1's expense-at-
confirm), unless the CPA asks for accrual — Phase 2 already scopes it out.

### Track 4 — QBO stays in sync (decision required, see §5)

9. **PR(s): push completion** per Phase 2 §5 — `push_payment` with dependency-ordered
   outbox, `push_invoice` sparse updates, three ItemRefs, outbox health surface.
   QBO **writes are free**; this does not touch the metered-reads concern.
10. **Monthly verification diff** per Phase 2 §6.2 — TrialBalance vs GL with
    expected-diff classes (~hundreds of metered reads/month vs the 500K cap; cheap).

## 5. ~~Open decision~~ DECIDED (Doug, 2026-08-14): QBO is the tax book this year

The accountant files tax year 2026 from QuickBooks. Consequences:

- **Track 4 is in scope in full** (push completion + verification diff) and is
  **elevated**: until `push_payment` exists and the §5.5 backfill runs, every GDX-side
  payment and post-issue invoice change is silently missing from the tax book. This is
  a present, growing gap — Track 4 runs in parallel with Track 1, not after Track 3.
- QBO stays a maintained second book until Phase 3's trust switch; the phase-out
  continues but its endpoint moves past this tax year.
- **Nightly scoped pull is ON** (Doug, 2026-08-14: "if we don't do at least a nightly
  QB pull how will we know everything matches?"). Audit correction: the schedule infra
  already exists end-to-end (beat dispatcher + `FREQ_DAILY` + update endpoint) — the
  real build is (a) date-windowing the *scheduled* path, which today queues an
  UNSCOPED full-history pull, (b) a read counter (none exists; chokepoint
  `QBClient.query`), and (c) loud-stale health — dead OAuth tokens currently no-op
  at INFO level, so the mirror goes stale silently. Verified no conflict with the QB
  money-pull pause (it gates invoice/payment pulls into GDX rows; banking pulls write
  only `qb_*` mirror tables). Rollout precondition: verify the prod OAuth connection
  is alive before flipping frequency to daily.
- CPA process note from Phase 2 §5.1 applies at go-live: their QBO bank-feed workflow
  must *match* deposits to pushed ReceivePayments, not add new ones — one-line brief,
  else card deposits double-book in QBO.

## 6. Sequencing & dependencies

```text
Track 1 (1→2→3)  ──────────────►  no blockers, start now, stacked PRs
Track 4 (9→10)   ──────────────►  ELEVATED (§5 decided): parallel with Track 1 —
                                  tax book is currently missing GDX-side payments;
                                  §5.5 backfill runbook is part of item 9's rollout
Track 2 (4, 5)   ──────────────►  4 independent; 5 after 1–2 (shares confirm surface)
Track 3 research/CPA (parallel, Doug-side + research runs)
Track 3 build (6→7→8)  ────────►  after CPA + §9.1; 6 reuses Track 1's confirm plumbing
```

Track 1 and Track 4 touch disjoint code (bank_feeds/vendor_invoices vs
modules/quickbooks) — safe to run as parallel PR streams.

Every implementation PR: adversarial audit before merge (established practice), synthetic
fixtures only, headed walk on real data before its feature is called done.

## 7. Definition of "everything matches up" (acceptance)

For a chosen recent month, on real data:

1. Every statement line for the period is matched or accepted-outstanding; the
   feed↔statement tie-out for that month is clean.
2. Every confirmed debit shows a labeled book record (expense category or paid bill);
   the Reconcile tab shows zero unexplained items.
3. (Post flag-flip) the tie-out worksheet closes to the statement ending balance.
4. (Per §5 decision) the QBO diff for the month has zero unexplained buckets.
