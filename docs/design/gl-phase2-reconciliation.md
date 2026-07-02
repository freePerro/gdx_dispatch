# GL Phase 2 — Bank Reconciliation, Stripe Reconciliation & the QBO Verification Loop

**Status:** v4.1 FINAL-DRAFT (v4.1: added §5.5 payment-backfill runbook step, required by Phase 3's audit) — survived 3 adversarial audit rounds (R1 NOT READY → architecture rewrite; R2 delta → push/hash/seam fixes; R3 mini-delta READY-WITH-CONDITIONS → per-payout catch-up + repair era-restriction applied). Blocked on: §9.1 re-verification of [SOURCED] claims + CPA items before implementation. Non-blocking R3 note adopted: reopening month M flags later tied-out months for re-assertion.
**Date:** 2026-07-02
**Depends on:** Phase 1 (`gl-phase1-core-ledger.md`, v3). **Followed by:** Phase 3 (parallel run + trust switch).
**Provenance labels:** **[VERIFIED]** adversarially verified 3-0 (research runs 2026-07-01/02) · **[SOURCED]** primary-source quotes, verification pass killed by spend limit — re-verify before implementation · **[CODE]** direct code reading, auditor-checked · **[AUDIT-R1]** audit finding · **[JUDGMENT]** / **[CPA]** as in Phase 1.

## 0. Audit round 1 — what changed in v2

R1 falsified v1's architecture, not just details:

1. **The QBO "bank" mirrors are QBO's *booked* transactions, not bank-feed lines** (`qb_bank_transactions` mirrors the Purchase entity, banking.py:148; the API exposes no "For Review" feed lines; no cleared status, no statement boundaries). v1's bank rec and QBO verification were the same books-vs-books comparison twice, anchored circularly (`qb_accounts.current_balance` = QBO **book** balance, sync.py:1686) — and silently required Doug/CPA to keep full double bookkeeping in QBO forever, which Phase 1's cutover ends. **v2: true bank evidence = imported bank statements (§2); QBO mirrors demoted to transition-era verification (§6).**
2. **The push story was fiction:** there is **no `push_payment`** (sync.py has only push_customer/:1174, push_invoice/:1232, push_expense/:1415) and `push_invoice` is create-only (short-circuits on `already_mapped`, :1242-1251). Every GDX payment and post-issue change would be a permanent GDX-vs-QBO diff with no way to detect push failure. **v2 adds QBO push completion (§5).**
3. **P3′'s method-string routing was broken by existing convention** (`Payment.method` free-text and already overloaded — `"quickbooks"` = provenance, core/payments.py:54-77). **v2 routes on PaymentIntent id (§4).**
4. Matching-engine specifics: cross-source duplicate candidates (SalesReceipt pulled unfiltered, banking.py:533-540, also appears inside its sweeping Deposit); `linked_qb_ids`→GDX mapping dies post-cutover (fed only by disabled pulls); the "one non-rejected match" partial index isn't expressible as stated; `qb_bank_transactions` has only `account_name`, no account id (:183). All addressed (§3, §6).
5. `QbReconciliationPanel.vue` is **not** a placeholder — it's a live 258-line delete-sync management panel. v1 would have demolished a shipped feature; the [CODE] label was wrong. **v2 builds a new view (§7).**
6. [SOURCED] honesty: MT match-cardinalities and Stripe balance-transaction semantics ARE load-bearing for §3.2/§4.2 schemas — re-verification is now a **blocking** pre-implementation item (§9.1), not a nice-to-have.

**Round 2 (delta)** confirmed the architecture fix and found three residuals, fixed in v3: **[AUDIT-R2]**

7. **The P3→P3′ seam:** Phase-1-era card payments posted gross to 1000 with fees never expensed — every card-active month before Phase 2 go-live was structurally un-tie-out-able, and §2.2's 3950 fallback would have buried real fee expense in opening equity. v3 adds the Stripe catch-up procedure (§4.4).
8. **push_payment's hidden dependencies and ordering** (ReceivePayment needs CustomerRef + invoice qb_id LinkedTxn; payment-behind-failed-invoice-push) were unspecified (§5.1).
9. `line_hash` "ordinal" as a file-row number would break dedupe for every line under overlapping exports — redefined as an occurrence counter (§2.1); tie-out/lock state machine for voided batches specified (§2.2); P3′ mis-record repair path + orphan-bt monitor added (§4.1).

## 1. Scope

1. **Statement-based bank reconciliation** — imported bank statements ↔ GL, suggest-and-confirm matching + monthly tie-out (the only true external anchor).
2. **Payment-processor fee reconciliation** — provider-agnostic (§4.0); clearing-account pattern for net-deposit processors, statement-rule fees for gross-deposit ones; fees stop being invisible either way. (Stripe is the worked adapter example but is **not currently in use** — Doug, 2026-07-02.)
3. **QBO push completion** — payments, invoice updates, adjustments; push-outbox with failure detection. (Absorbed from what the verification loop turned out to require.)
4. **QBO verification loop** — transition-era books-vs-books diff (honestly labeled as such) + divergence detection, feeding Phase 3's trust-switch criteria.

Non-goals: AP/bill terms; payroll posting (QBO-only; expected-diff class §6.2); real-time cash position (statement cadence is monthly; Plaid upgrade path §2.3).

## 2. Bank evidence: imported statements first, Plaid as the upgrade

### 2.1 Primary: manual statement import **[AUDIT-R1 architecture fix]**

The bank statement is the only evidence that is *of the bank* rather than *of somebody's books*, and for a one-owner shop the monthly import burden is minutes:

```
bank_accounts          -- GDX's own registry (NOT qb_accounts)
  id · name · gl_account_id (1000/1050/…) · institution · last4
  statement_import_format Enum: csv_generic | ofx | qfx
bank_statement_lines   -- append-only external evidence
  id · bank_account_id · txn_date · amount_cents (signed) · description
  balance_after_cents nullable · import_batch_id · line_hash (sha256 of
  account+date+amount+description+occurrence_n, where occurrence_n is the
  1-based count of identical (date,amount,description) lines seen so far
  WITHIN that key — stable under file reordering, so two identical bank
  transactions dedupe correctly across overlapping exports [AUDIT-R2])
  · created_at   (no updates; bad imports are voided by batch)
bank_statement_imports -- batch: file name/hash, period, row counts, imported_by
```

CSV mapping is configured once per account (column positions, date format, sign convention); OFX/QFX parse directly. Re-importing an overlapping file is safe (line_hash dedupe). A voided batch cascades: its lines drop from candidacy; confirmed matches referencing them go to the exception queue — never silently unlink.

### 2.2 Tie-out anchor: the statement, nothing else

`bank_statement_periods.statement_ending_balance_cents` comes from the imported statement (or Doug typing it from the paper statement). **The v1 `qb_account_snapshot` source is deleted** — QBO `Account.CurrentBalance` is a book balance; anchoring tie-out to it re-creates the circularity this phase exists to break. **[AUDIT-R1]**

Tie-out assertion per account-month: `statement ending balance = GL account balance at period_end − uncleared GL lines + deposits-in-transit`, where **cleared = the GL line belongs to a confirmed match against a statement line in the period**. First tie-out retroactively verifies the Phase 1 owner-attested opening bank balance; a gap posts a correction against 3950 with a memo — **after the §4.4 Stripe catch-up has run**, so accumulated card fees are never mistaken for an opening-balance error (**[AUDIT-R2]**). Tied-out months are the natural `gl_period_locks` point.

**Tie-out/lock state machine (**[AUDIT-R2]**):** `tied_out` requires all period statement lines matched-or-accepted-outstanding. Voiding an import batch that underlies a tied-out period reverts the period to `open`, releases its `gl_period_lock`, requires `accounting.close`, and is audit-logged (`tieout_reopened`). Replacement matches whose confirmation posts entries (P10/P12) follow the Phase 1 lock rules: post to the first open day with a memo naming the true date, or into the reopened period while it stays open.

### 2.3 Upgrade path: Plaid (documented, optional)

Pay-as-you-go pricing, per-connected-account subscription for Transactions (**[VERIFIED]**); `/transactions/sync` cursor deltas map directly onto `bank_statement_lines` ingestion (**[VERIFIED]**); **pending transactions are mutable — only posted transactions become statement lines** (**[VERIFIED]**). Plaid raises cadence from monthly to daily; the matching engine and tie-out are unchanged because the evidence table is source-agnostic. Build only if the monthly import ritual proves annoying. **[JUDGMENT]**

## 3. The matching engine

### 3.1 Model

Two-sided, metadata-only, suggest-and-confirm (Xero shape **[SOURCED]**; GDX's recategorize.py Yellow-tier precedent **[CODE]**). Left: `bank_statement_lines`. Right: GL lines on bank-mapped accounts (1000/1050/1060-via-payout…). Invariants unchanged from v1: matches never mutate the GL; evidence is never edited; every confirm is reversible (unconfirm reverses any entries it posted).

### 3.2 Schema

As v1 (`bank_matches` + `bank_match_externals` + `bank_match_gl_lines`, cardinalities 1:1 / 1:N / N:1 — **[SOURCED]**, re-verify §9.1) with two **[AUDIT-R1]** corrections:

- **Exclusivity enforcement:** child tables carry a denormalized `match_status`, kept consistent by trigger; partial unique indexes on `(source_table, source_id) WHERE match_status <> 'rejected'` and `(gl_line_id) WHERE match_status <> 'rejected'`. (A parent-table predicate isn't expressible in a Postgres partial index.)
- `bank_match_externals.source_table` is `bank_statement_lines` in steady state; `qb_*` sources are legal **only in transition mode** (§6) and are dedup-canonicalized first (§6.1).

### 3.3 Rule pipeline (ordered; first hit wins)

| # | Rule | Logic | Disposition |
|---|---|---|---|
| R1 | Reference | Statement description carries a hard id: Stripe payout id (§4.3), check number ↔ `Payment.reference`, invoice number | 0.99, auto-confirm eligible |
| R2 | Exact 1:1 | amount equal + same `bank_account_id` + date within ±3 business days + no competing candidate **across the whole candidate set** | 0.95, auto-confirm eligible |
| R3 | Deposit sweep (N:1/1:N) | statement deposit = sum of undeposited 1050 lines (cash/check payments) within 7 days; bounded subset-sum (n≤12, k≤6, else manual). *Transition mode only:* `linked_qb_ids` shortcut where QBO ids still map (**[AUDIT-R1]**: that mapping is fed by disabled pulls — post-cutover R3 is subset-sum only, stated plainly) | 0.9 / 0.7 |
| R4 | Tolerance | R2 ± ≤100¢ (config); difference → `residual_cents` → P12 to 6990 on confirm | 0.6, suggest-only |
| R5 | User rules → new entry | payee/description patterns for bank-only reality (fees, interest, loan autopay → P10; principal/interest split **[CPA]**) | suggest-only until ≥3 confirmed uses **[JUDGMENT]** |
| R6 | Unmatched aging | unmatched >14 days → exception queue; in transition mode, QBO-mirror rows not traceable to a GDX push → divergence candidates (§6.3) | triage |

P10/P12 posting rules and reversal-on-unconfirm as v1.

## 4. Payment-processor reconciliation (provider-agnostic; Stripe adapter dormant)

### 4.0 Reality check & generalization (Doug, 2026-07-02)

**GDX does not currently process payments through Stripe.** The Stripe code surface (`stripe_connect.py`, portal charge endpoints, webhook stubs) exists but is not live — which reclassifies Phase 1 §12's "orphaned charges" defect from *live money leakage* to *dormant defect: must be fixed before any Stripe activation* (and the prod-history sweep should trivially return empty — run it once to confirm).

The design below therefore generalizes. Everything reduces to one question per processor: **do deposits arrive gross (fees billed separately) or net (fees withheld)?**

- **Gross-deposit processors** (typical bank merchant services: daily gross batches, fees debited monthly): **no clearing account needed.** P3 debits 1000 directly; the deposit statement line matches the day's card payments via R3 (sum match); the monthly fee debit is an R5 user rule → 6950 Payment Processing Fees. This is the simplest case and may be all Phase 2 ever needs.
- **Net-deposit processors** (Stripe, Square, most PSPs): the clearing-account pattern below (1060, P3′/P13/P14/P15) applies, with a **provider adapter** supplying the balance-transaction data — via API where one exists, or derived from monthly processor statements where it doesn't (fees post from the statement, coarser but sufficient at this volume).
- **Routing generalizes:** P3′ applies iff the Payment row carries a `processor_payment_ref` (provider-namespaced, e.g. `stripe:pi_...`, `square:...`) — the Stripe-specific `stripe_payment_intent_id` becomes this general column.
- **Switching providers later** = adding an adapter + an R5/R1 rule set; the match engine, clearing pattern, and posting rules don't change. The §4.4 catch-up procedure applies only to periods where a net-deposit processor was actually live (if Stripe never processed real money, it is a no-op).

**Doug's answer (2026-07-02): "everything is line items on accounts"** — deposits and processing fees each appear as their own line items on the bank account. **Branch picked: statement-driven, no clearing account, no processor API, no mirror tables.** Phase 2 builds for card money exactly two things:

1. Card payments post plain P3 (debit 1000); deposit line items match the underlying payments via R2/R3.
2. Fee line items get a standing R5 rule → P10 entry to 6950 Payment Processing Fees.

Self-diagnosing safety net: if some fees turn out to be netted out of deposits rather than listed separately, R3's sum-match won't close and the deposit lands in the exception queue — the first month's tie-out reveals the true shape with no design change needed (worst case, the R4 tolerance/residual path or the clearing pattern below activates for that provider).

The remainder of §4 (clearing account, 1060/P3′/P13-P15, mirror tables, catch-up) is retained as the **dormant adapter spec** — built only if a net-deposit processor (Stripe or otherwise) ever activates. §4.4's catch-up is a no-op for GDX's actual history.

### 4.1 Clearing pattern

Accounts **1060 Stripe Clearing** (asset, cash-equivalent class) and **6950 Payment Processing Fees**. Rules P3′/P13/P14/P15 as v1 (gross → 1060; fee → 6950; payout net → 1000; refunds/disputes → 4910/6950 against 1060). Invariant: 1060 trends to zero per payout; persistent balance = unreconciled, dashboard-surfaced.

**[AUDIT-R1] fixes:**

- **Routing:** P3′ applies **iff the Payment row carries a `stripe_payment_intent_id`** (new nullable column, populated by the §5.3-consolidated recording path from Phase 1). Never route on `method` — it's free-text and already overloaded as provenance (`"quickbooks"`, core/payments.py:54-77). A hand-typed `method="card"` with no PaymentIntent id is treated as a terminal/other-processor payment: plain P3 to 1000, matched against the statement like any deposit.
- **Mis-record repair (**[AUDIT-R2]**):** a Stripe charge hand-recorded without its intent id misroutes to 1000 and shows up as an **orphan balance transaction** — a bt whose intent id matches no Payment. The nightly pull maintains an orphan-bt exception queue (this is also the permanent home of the Phase 1 orphaned-charges sweep). Repair action from the queue: link the bt to the existing Payment (sets `stripe_payment_intent_id`), which posts the reclass through the Phase 1 engine — reversal of the P3 (1000) entry, repost as P3′ (1060) — restoring the 1060 invariant. If no Payment exists at all, the queue item routes to "record payment" instead. **Era restriction (**[AUDIT-R3]**):** the reclass repair applies only to bts dated after Phase 2 go-live — Phase-1-era bts are already corrected by the §4.4 per-payout catch-up (their fees hit 1000, not 1060), and reclassing one would strand gross in 1060 with no P14 ever coming, permanently breaking the trend-to-zero invariant. Phase-1-era orphans surface in the queue as report-only items for the orphaned-charges investigation.
- **Cash-basis timing policy (stated):** 1060 is classed as a cash account for cash-basis derivation — recognition happens at payment recording (constructive receipt at charge), not at payout. Fees expense on the balance-transaction date and may straddle a month boundary relative to their charge; accepted, memo-linked to the charge for drill-down. **[CPA]** confirm both.
- Hard dependency, inherited from Phase 1 §12: the front-office/portal Stripe paths must create Payment rows (with PaymentIntent ids) before P3′ means anything. Day-one sweep: unmatched historical balance transactions = the orphaned-charges search.

### 4.2 Data

`stripe_balance_transactions` + `stripe_payouts` mirrors as v1 (append-only, cursor-paginated nightly pull; gross/fee/net per bt; `payout` filter ties bts to payouts — **[SOURCED]**, re-verify §9.1; idempotent P13–P15 keyed on bt/payout ids).

### 4.3 End to end

Charge (P3′) → fee (P13) → payout (P14) → **statement line** (R1 on payout id if the bank memo carries it — §9.5 checks one real statement first — else R2 on net+arrival ±2 days) → tie-out. Every card dollar traceable invoice→bank-statement.

### 4.4 The P3→P3′ transition: Stripe catch-up **[AUDIT-R2 — new]**

Between Phase 1 cutover and Phase 2 go-live, card payments posted **gross to 1000** and fees were never expensed — so GL 1000 is overstated by exactly the accumulated fees, and those months cannot tie out. One-time catch-up at Phase 2 go-live, before any historical tie-out runs:

1. Pull balance transactions back to the Phase 1 cutover date.
2. Post **one catch-up entry per payout** (**[AUDIT-R3]** — per-month granularity left Phase-1-era statement lines unmatchable): debit 6950 (that payout's fees), credit 1000 — key `stripe_catchup:{payout_id}`, memo linking the bt ids. Each Phase-1-era payout statement line (net) then line-matches N:1 against {its charges' gross P3 debits} + {its catch-up credit}, which sum exactly to net — expressible in the standard match schema.
3. Prospectively, P3′ applies from go-live (dated posting-rule version, as Phase 1 anticipated). Historical P3 entries are NOT restated — the per-payout catch-up entries are the correction, and 3950 is never involved (fees are expense, not opening equity).
4. Only after catch-up: run historical tie-outs oldest-first. Fallback for any Phase-1-era month where charge→payout attribution is incomplete: an explicit **balance-level tie-out mode** (month-aggregate assertion, flagged `aggregate_only` on the period) rather than pretending line-match confidence that isn't there.

## 5. QBO push completion **[AUDIT-R1 — new section]**

The tax book is only as good as what reaches it. All writes are Intuit-free Core calls (**[VERIFIED]**).

1. **`push_payment`** (new): GDX Payment → QBO ReceivePayment against the mapped invoice; void → QBO void/delete. Adds `qb_dirty`/`qb_synced_at`/`qb_id` to Payment (the Invoice pattern, models:357-370 **[CODE]**). **Dependencies & ordering (**[AUDIT-R2]**):** ReceivePayment requires the customer's qb_id (CustomerRef) and the invoice's qb_id (LinkedTxn). Pre-cutover imported invoices are already mapped (`qb_entity_maps`, written by the historical pulls — sync.py:781/:879); post-cutover invoices are mapped when `push_invoice` succeeds. The outbox is therefore **dependency-ordered**: a payment push is not attempted until its invoice's qb_id exists (customer likewise); it waits in the outbox as `blocked_on:invoice:{id}`, visible in the §5.4 health surface — never pushed as an unlinked/unapplied credit, never retry-spun. **CPA process note** (**[AUDIT-R2]**) **[CPA]**: once payments push, the CPA's bank-feed workflow in QBO must *match* deposits to the pushed ReceivePayments, not add new ones — else every card deposit double-books in QBO. One-line brief to the CPA at go-live.
2. **`push_invoice` sparse updates** (fix): currently create-only (:1242-1251). Gains fetch-SyncToken + sparse update on `qb_dirty` for post-issue changes; credit memos / adjustments push as QBO CreditMemo. (Sparse-update + SyncToken mechanics already exist in recategorize.py:244-313 **[CODE]** — reuse.)
3. **Revenue mapping:** push_invoice lines currently carry no ItemRef (:1276-1289) — all QBO revenue lands in one income account, making per-account revenue diffs structurally meaningless. Minimal fix: three QBO Items (Service/Install/Parts) mapped from `InvoiceLine.category`, pushed as ItemRefs. **[JUDGMENT]** — alternative (group-level diff only) rejected because three Items is an hour of setup and makes the CPA's QBO view match Doug's mental model.
4. **Push-outbox monitoring:** `qb_dirty=true AND age > threshold` (per entity type) is a first-class health signal on `/qb` and the reconciliation dashboard — push failure is *detectable*, distinguishing "we never told QBO" from genuine divergence. **[AUDIT-R1]** — without this, the verification loop can't attribute diffs.
5. **Deployment runbook — historical payment backfill/dedup** (added v4.1; Phase 3's audit caught this referenced-but-unspecified): at push_payment go-live, for each open invoice with a qb_id, list its QBO-side linked payments. GDX payments with no QBO counterpart → push. QBO payments with no GDX counterpart → divergence work items (§6.3), resolved before the Phase 3 gate count starts. Matched pairs (amount+date) → write the qb_id mapping and skip. Invoices without qb_id wait for their invoice push first (dependency ordering, item 1). The procedure is idempotent and re-runnable; its completion is a Phase 3 gate-start precondition.

## 6. QBO verification loop — honestly scoped

**What it is:** a books-vs-books diff between GDX's GL and QBO — meaningful **only while QBO is a maintained second book** (from Phase 2 go-live until Phase 3's trust switch, and afterward only for the accounts QBO still owns: payroll, tax filings). It is NOT bank reconciliation; §2-3 is. **[AUDIT-R1]**

### 6.1 Transition-mode matching inputs

While QBO books remain maintained, `qb_*` mirror rows may serve as *secondary* match candidates (e.g., CPA-booked items GDX lacks). Candidates pass through a canonicalizer first: a Deposit swallows the Payments/SalesReceipts it swept via `linked_qb_ids` (one bank event = one candidate — kills the R1-found duplicate, banking.py:74/:533-540); a Deposit with **empty** `linked_qb_ids` (created directly in QBO, e.g. by the CPA) stands as its own sole candidate, and any UF-parked payment rows it plausibly covers (amount-window overlap) are suppressed from candidacy while it is unresolved rather than offered alongside it (**[AUDIT-R2]**); `qb_bank_transactions` maps to accounts by name (`account_name`-only schema, :183) with unmapped names surfaced, not guessed.

### 6.2 Monthly diff

QBO TrialBalance (+P&L corroboration) vs GL, per `gl_qb_account_map` with `expected_diff_class` (none/timing/basis/qbo_only/gdx_only) **plus `comparison_group_id`** for legitimate N:1 mappings (**[AUDIT-R1]** — e.g., if the three-Item fix (§5.3) is deferred, 4000+4100+4200 compare as a group against QBO's one income account). Buckets: explained / tolerated (0¢ balance-sheet, 100¢ P&L classes **[JUDGMENT]**) / unexplained → drill-down. Constraints, both **[VERIFIED]**: build TrialBalance parsing v2-native (metadata-driven, no positional ColData — pnl.py:340's index assumptions are a flagged liability and its v2 migration ships in this phase, due 2026-08-31 regardless); CorePlus read-budget counter on `/qb` (monthly loop ≈ hundreds of reads vs the 500K cap).

### 6.3 Divergence detection

- **Transition mode:** mirror rows not traceable to a GDX push (via the §5 outbox/qb_id trail — now possible because payments push) → divergence queue. CPA bank-feed bookings that *should* exist in GDX (a payment GDX missed) become work items resolved by deliberate GDX actions that push forward.
- **CDC probe:** QBO Change Data Capture polled for Invoice/Payment changes GDX didn't originate — **[SOURCED]**, confirm endpoint status under the 2025-26 platform changes before building (webhook CloudEvents migration was refuted 0-3; treat polling as the dependable path).
- **Post-trust-switch:** the mirrors stop being load-bearing; the diff narrows to QBO-owned accounts; divergence detection retires with QBO's demotion (Phase 3's call).

## 7. UI

**New** `BankReconciliationView.vue` (nav "Accounting", `accounting.read`/`write`; tie-out + auto-confirm settings under `accounting.close`): tabs for Statement import, Match queue, Tie-out worksheet, QBO verification (diff + divergence queue + push-outbox health). `QbBankingPanel.vue` untouched; **`QbReconciliationPanel.vue` is a live delete-sync management panel and is left alone** (**[AUDIT-R1]** — v1 mislabeled it a placeholder).

## 8. Testing & gates

- Property: confirm/unconfirm round-trips; subset-sum non-overlap; per-payout 1060 zero-sum; statement-line hash dedupe under overlapping imports.
- Replay: bt pull re-run → zero new entries; voided import batch → matches to exception queue, never silent unlink.
- Push: outbox sweep flags stale `qb_dirty`; push_payment idempotent under Celery retry (requestid, client.py:96-109 **[CODE]**).
- Reports v2: pnl.py + TrialBalance parsers CI-tested against `testing_migration=true` before 2026-08-31.
- Fixture month: statement CSV + Stripe payouts + QBO trial balance ties out to zero end-to-end.
- Process: audit round 2 (delta) on this doc; `/audit` on implementation; headed-browser verification on the real account.

## 9. Open questions

1. **[BLOCKING implementation]** Re-verify the **[SOURCED]** load-bearing claims when research budget resets: MT match cardinalities (§3.2 schema), Stripe balance-transaction gross/fee/net + payout filter (§4.2 schema/keys), QBO CDC status (§6.3), Xero suggest-and-confirm (§3.1 posture — lowest risk).
2. **[CPA]** Loan-payment principal/interest split cadence (R5); payroll stays `qbo_only` through Phase 3; §4.1 cash-basis timing policy (1060-as-cash; fee month-straddle).
3. Which statement formats Doug's bank(s) export (CSV columns / OFX availability) — determines the §2.1 parser order.
4. Auto-confirm ships off; revisit after 3 clean tie-outs. **[JUDGMENT]**
5. Does the bank statement memo carry Stripe payout ids? Check one real statement before building R1's parser (falls back to R2 cleanly if not).
6. QBO Items for revenue mapping (§5.3): confirm the CPA wants Service/Install/Parts split in QBO or is happy with one income line (drives whether `comparison_group_id` is temporary or permanent).

## 10. Sources

**[VERIFIED]**: CorePlus metering + 500K cap, Reports v2 cutover 2026-08-31, CloudEvents-webhook refutation (run 1, 2026-07-01); Plaid pricing/pay-as-you-go, `/transactions/sync` cursors, pending mutability (run 2, 2026-07-02). **[SOURCED]** (re-verify, §9.1): MT reconciliation rule-ordering + cardinalities; Xero two-sided suggest-confirm; Stripe balance-transaction semantics + payout filter + Payout Reconciliation report; QBO TrialBalance v2 + CDC. **[CODE]** (map 2026-07-01, corrected by audit R1 2026-07-02): banking.py mirrors/tombstones/linked_qb_ids; pnl.py ColData liability; recategorize.py sparse-update mechanics; sync.py push inventory (`push_payment` absent; push_invoice create-only; no ItemRefs); tenant_models Payment.method free-text; core/payments.py method-as-provenance; QbReconciliationPanel.vue delete-sync panel. Audit round 1 critique: `critique_gl_phase2_r1.md` (session scratchpad).
