# GL Phase 1 — Core Ledger Design

**Status:** v3 FINAL-DRAFT — survived 3 adversarial audit rounds (R1: not ready → rewritten; R2: not ready → machinery fixed; R3 delta check: READY-WITH-CONDITIONS, all three conditions applied in §5.2/§5.3/§5.6/§5.7). Blocked only on CPA sign-off (§12) before implementation.
**Date:** 2026-07-01
**Depends on:** nothing (additive). **Followed by:** Phase 2 (bank feeds + QBO verification loop), Phase 3 (parallel run + trust switch).
**Provenance:** load-bearing choices are backed by (a) adversarially-verified research (two deep-research passes, 2026-07-01, ~35 claims confirmed 3-0 against primary sources), (b) direct code reading, or (c) audit findings (**[AUDIT-R1]** / **[AUDIT-R2]**). Judgment calls: **[JUDGMENT]**. CPA items: **[CPA]**.

## 0. Audit history

**Round 1** falsified v1's three central codebase claims (QB sync is not one-way; `/send` doesn't lock; Stripe webhook records nothing) and found five design holes (INSERT tampering, non-replayable keys, unruled credit-memo/refund endpoints, void/overpay cash-basis gaps, §446 miss). All addressed in v2.

**Round 2** confirmed the v1 fixes were real, then broke the v2 machinery. v3 addresses:

1. **Writer inventory was still incomplete** — `core/payments.py` `_mark_invoice_paid` (:127, live via `app.py:741-742`; Stripe.js confirm :212, ACH :295, webhook handler :416) flips invoices to `paid` with **no Payment row** and commits mid-flow; `mobile_invoicing.py:504/:624` are two more `sent` writers; three webhook surfaces exist, not one (§5, §5.1, §12).
2. **Content-key A→B→A collision silently erases revenue** — repost at original content collides with the reversed original entry and no-ops; ledger nets to zero while the invoice is live (§5.6).
3. **Sealing trigger breaks data-only restores** (`pg_restore --data-only` replays under a new txid) — restore procedure documented; engine must own its transaction (no mid-flow commits) (§3.5).
4. Flush guard was oversold — reframed as dev/test tripwire; Core-level writes bypass it; enforcement is the writer-inventory test + CI lint (§2).
5. "QBO write-only" was absolutist — read-only `qb_*` mirror pulls (banking sync) continue; Phase 1 has no QBO-divergence detection and says so (§5.4).
6. 2300 Customer Credits had no relief path; credit-memo "adjustment record" was unspecified (§5.2, §5.3).
7. Opening balances had no proration anchor for pre-cutover invoices; opening-AR formula ignored historical credit memos (§5.7).
8. 4900 lumped discounts/warranties/refunds; refund's cash-basis treatment was unspecified (§5.2, §6).

## 1. Goal and non-goals

**Goal:** GDX keeps real double-entry books — chart of accounts, append-only journal, a posting engine covering **every writer of financial state**, and trial balance / P&L / balance sheet with an accrual/cash toggle. QBO remains the tax book. Pulls that mutate Invoice/Payment/Expense are disabled once the ledger is on; read-only `qb_*` mirror pulls continue (§5.4).

**Non-goals:** bank feeds/reconciliation (Phase 2); payroll/tax filing/1099s (QBO + CPA, permanently); inventory asset on the GL (§7, tax-driven); multi-currency; OCR; AP subledger; cached balances (reports `SUM()` the journal — milliseconds at this volume **[JUDGMENT]**).

## 2. Architecture: ledger beside the operational models, with a chokepoint

Research (Modern Treasury, Square; 3-0): business objects are mutable presentations; the ledger is the immutable money truth. The load-bearing corollary (**[AUDIT-R1]**): every mutation of financial state flows through `ledger.post_for_event(session, event)` in the same DB transaction — routers, mobile, portal, front-office Stripe, QB sync, Celery, future code.

Coverage is enforced by **three layers, honestly labeled** (**[AUDIT-R2]** — no single one suffices):

1. **Writer-inventory test (the real enforcement):** a test that greps/imports every code path writing money columns (the §5 table, kept current) and asserts each either posts through the engine or is disabled. New writers fail CI until added.
2. **CI lint:** forbid raw Core writes (`.__table__.delete()/update()`, `session.execute(update(...))`) against money tables outside `modules/ledger/` — `sync.py:372` does exactly this today and bypasses ORM events entirely.
3. **Flush guard (dev/test tripwire only):** a `before_flush` listener raising when ORM-level money mutations lack a registered posting intent. It cannot see Core-level writes and runs log-only in prod — it is a tripwire, not the fence.

No new money/GL columns on operational models (exceptions: `payments.voided_at`, `job_receipts.promoted_expense_id`, plus the `invoice_adjustments` table in §5.2). Financial reports read only the ledger.

## 3. Data model

Three entities (MT, Square, TigerBeetle, Uber; 3-0; two-entity alternative refuted 0-3).

### 3.1 `gl_accounts`

As v2: code/name/type/parent/is_system/active; no `deleted_at` — deactivate, never delete, once posted-to.

### 3.2 `gl_journal_entries`

As v2: `entry_no` sequence; **`effective_at` Date** (economic) + `posted_at` (record) bitemporal pair (MT `effective_at` + Fowler, verified); `status ∈ {posted, reversed}`; `source_type/source_id`; unique `idempotency_key` (§5.6); `reverses_entry_id`/`reversed_by_entry_id`; `created_txid`; `created_by`; `company_id`. No draft entries in Phase 1 — born posted, fixed by reversal. **[JUDGMENT]**

### 3.3 `gl_journal_lines`

As v2: `amount_cents BigInteger` signed (debit +, credit −; Square 3-0), `CHECK (amount_cents <> 0)`, memo, `job_id`/`customer_id` dimensions. Integer cents only; one `Decimal` ROUND_HALF_UP boundary function; floats lint-banned in `modules/ledger/`.

### 3.4 Balance invariant — in the database

Deferred constraint trigger at commit: `SUM(amount_cents) = 0`, `COUNT(*) >= 2`, ≥1 positive and ≥1 negative line. Engine pre-asserts in Python for good errors.

### 3.5 Immutability — all three verbs, with operational honesty

- `BEFORE UPDATE OR DELETE` raise-triggers on both tables; two validated column exemptions on entries (`status: posted→reversed`, `reversed_by_entry_id: NULL→value`).
- **Sealing:** lines may only INSERT in the transaction that created their entry (`created_txid = txid_current()` check). Verified sound for SAVEPOINTs/`begin_nested`, pgbouncer transaction mode, logical replication (apply skips ordinary triggers), and full `pg_dump` restores (triggers emit post-data). **[AUDIT-R2] operational caveat, documented here and to be added to the `/backup` runbook:** `pg_restore --data-only` / per-table COPY repairs replay under a new txid and **must run with `--disable-triggers`** (superuser) or they will reject every historical line. A restore drill without this note would falsely "prove" backups unrestorable.
- **The engine owns its transaction** (**[AUDIT-R2]**): `post_for_event` requires an open transaction it does not commit; helpers that commit mid-flow (e.g. `core/payments.py:_mark_invoice_paid` commits internally today) must be refactored to the request-scoped transaction before they can host posting calls. A mid-flow commit would split entry/lines across txids and trip the sealing trigger — by design.
- Corrections are reversal entries (all lines negated, `reverses_entry_id`, key per §5.6). No soft-delete on journal tables. Triggers ship in the Alembic migration (tables come from `create_orm_tables()` first, per #41 ordering — verified `bootstrap_app.py:53-68`).

### 3.6 `gl_period_locks`

As v2: append-only lock history; hard block on `effective_at <= lock_date` except `accounting.close` holders, every override audit-logged (`gl_posted_into_locked_period`) — Xero's block + QBO's exception trail (both verified). Late facts post to the first open day with a memo naming the true date.

### 3.7 `expense_receipts` — source documents

As v2 (schema, soft-delete-only, SHA-256 content-hash). IRS grounding verified: $75 rule is §274(d)-only — parts receipts always kept (Pub 463 ch.5); digital images replace paper per Rev. Proc. 97-22 (current; OMB renewal 2026-03-04) — compliance mapping: content-hash (integrity), vendor/date/amount filtering (indexing), original-resolution download (reproduction), no hard delete + ≥7-year retention (Pub 583 Table 3). Flows: direct attach (multiple per expense) and **promote-from-field** (JobReceipt → prefilled Expense → `promoted_expense_id`), the Dext/QBO-snap shape minus OCR.

## 4. Chart of accounts

Starter CoA as v2, with **[AUDIT-R2]** refinements:

- 1000 Operating Bank · 1050 Undeposited Funds · 1200 AR (system, engine-only)
- 2100 Sales Tax Payable · **2300 Customer Credits** (relief path in §5.3/P9)
- 3000/3100 equity · 3900 Retained Earnings · 3950 Opening Balance Equity
- 4000 Service & Repair · 4100 Installation · 4200 Parts revenue — **4000 is the explicit fallback for NULL/unmapped line categories** (memo-flagged)
- **4900 Discounts Given** and **4910 Refunds & Allowances** (contra-revenue) — split because lumping discounts with warranty/error credits misstates the discounts line; credit-memo/refund carry a `reason` that maps to one of the two. **[CPA]** review the split.
- 5000 Parts & Materials · 5100 Subcontractors (COGS) · **6050 Wages & Payroll · 6060 Payroll Taxes & Fees** (added 2026-07-02 — payroll runs through an external payroll company, not QBO Payroll; its bank debits post here via Phase 2's R5 statement-matching rules, so the biggest expense line of the business has a home from day one) · 6xxx opex mapped 1:1 from the eight hardcoded categories (`expenses.py:28-37`; API validates category from Phase 1 on; unknown → 6900 + memo flag) · 6990 Rounding Differences
- `ExpenseLine.account` free-text is removed from the UI; the posted CoA account is displayed (one truth in the drill chain).

CoA composition is convention — Doug + CPA review before seeding. **[JUDGMENT][CPA]**

## 5. Posting rules — from the (round-2-completed) inventory of actual writers

| Writer | Paths | Disposition |
|---|---|---|
| Invoice routers | create, PATCH, `/send` :1023, `/mark-sent` :989, `/finalize` :1375, `/credit-memo` :1473, `/refund` :1506, line CRUD, soft-delete | transition helper + P1/P2/adjustment rules |
| `_recalculate_invoice` | auto-flip →`paid` from any status incl. draft (:201-204) | transition helper |
| **`core/payments.py`** **[AUDIT-R2]** | `_mark_invoice_paid` :127 via Stripe.js confirm :212, ACH :295, webhook handler :416 (live, `app.py:741-742`) — **no Payment row today, commits mid-flow** | refactor: create Payment row, drop internal commit, route through transition helper (§5.3) |
| **`routers/mobile_invoicing.py`** **[AUDIT-R2]** | `status="sent"` writers :504, :624 | transition helper |
| Portal/webhook surfaces | `routers/payments.py:235` charge (no Payment row), `routers/stripe_webhook.py:15` (logs only), `core/payments.py:416` (dead handler never wired) | one consolidated Stripe recording path (§5.3); dead handler deleted |
| Expense routers | create, PATCH, line add :213, soft-delete | P5/P6 |
| QB sync pulls | `pull_invoices` :641/:701, `_resync_invoice_lines` :354, `pull_payments` :1046/:1088, `_apply_qbo_deletes` :270 | **disabled when ledger on** (§5.4) |
| `core/quickbooks.py:433` | status writer, apparently dead (no callers) | delete or mark deprecated so nobody resurrects it |

### 5.1 Invoice issuance — hook the state transition

**P1 fires on the transition out of `draft` into `sent`/`paid`, wherever it happens.** `transition_invoice_status(session, invoice, new_status)` becomes the only legal writer of `Invoice.status`. Grep-verified retrofit set (**[AUDIT-R2]**): `invoices.py:202/:1004/:1030/:1486/:1524`, `mobile_invoicing.py:504/:624`, `core/payments.py:129` (3 call sites); `sync.py:701/:751` are mooted by §5.4. ~7 live sites in 3 files — bounded scope.

- P1: debit 1200 (total); credit 4xxx per line category (NULL→4000); credit 2100 (tax_amount, mirrored never recomputed).
- Paying a draft posts P1 (auto-flip transition) before P3 in the same transaction — negative-AR is structurally impossible.
- Post-issuance mutation of totals/tax/lines: reversal of live P1 + repost at current content (§5.6 keys).
- **Void:** new `POST /invoices/{id}/void`; all payments must be voided first (enforced); `/send`-resurrection of void invoices (possible today, :1030) is blocked — voided stays void. **[JUDGMENT]**

### 5.2 Credit memos and refunds

- Both recorded in a new small **`invoice_adjustments`** table (id, invoice_id, kind ∈ {credit_memo, refund}, amount, reason, refund_method nullable, created_by/at) — replacing the `/credit-memo` habit of mutating the deprecated `amount_paid` column (**[AUDIT-R2]**: that column is ignored by `_recalculate_invoice`, so today's endpoint can flip status with no payment — fixed).
- **Credit memo:** debit 4900 or 4910 per `reason`; credit 1200 AR. Key: `invoice:{id}:credit:{adjustment_id}`. **Cap (**[AUDIT-R3]**):** a credit memo may not exceed the invoice's remaining balance (validated at the API).
- **Operational integration (**[AUDIT-R3]**):** `_recalculate_invoice` learns the adjustments table — `balance_due = total − SUM(non-voided payments) − SUM(adjustments)` — so a fully-credited invoice actually closes operationally instead of the GL and the invoice status silently disagreeing.
- **Refund:** debit 4910; credit 1000. Requires `refund_method`. **Bug fix in scope:** `/refund` writes enum-invalid `status="refunded"` (`models:344-348`).
- **Refunds and credit memos are negative payment-events for cash-basis derivation** (**[AUDIT-R2]** — otherwise cash-basis revenue never decreases on refund): they prorate negatively against the invoice's anchor at their `effective_at`.

### 5.3 Payments

- **P3:** debit 1000 (card/ACH) or 1050 (cash/check) **[JUDGMENT]**; credit 1200. Key per §5.6.
- **Overpayment:** rejected unless caller opts into excess→2300 (debit cash, credit 2300; proration ratio capped at 1.0).
- **P9 — apply customer credit (new, [AUDIT-R2] relief path):** `POST /invoices/{id}/apply-credit` consumes 2300 balance against an open invoice — debit 2300, credit 1200. Without this, credits accumulate forever. **Caps (**[AUDIT-R3]**):** the applied amount may exceed neither the customer's 2300 balance (checked under `SELECT FOR UPDATE` on the customer's credit rows — the one place Phase 1 does need a balance precondition) nor the invoice's remaining balance.
- **P4 — payment void:** `POST /payments/{id}/void` sets `voided_at`, posts reversal; `_recalculate_invoice` skips voided payments; the void pushes to QBO so pull can never resurrect it.
- **Stripe consolidation (pre-existing defects, fixed in scope):** ALL Stripe money paths — portal charge (`payments.py:235`), front-office confirm/ACH (`core/payments.py:212/:295`), and the one real webhook (`stripe_webhook.py:15`) — converge on one recording function: create Payment row idempotent on PaymentIntent id → transition helper → P3. The webhook is the reconciliation backstop; the dead `core/payments.py:416` handler is deleted. Today front-office and portal card money moves with **no Payment row** (§12).

### 5.4 QB sync — pulls under control, honestly framed

When `ledger_posting_enabled`:

- **Pulls that mutate Invoice/Payment/Expense are disabled** (fail loudly + `/qb` health surfacing): `pull_invoices`, `_resync_invoice_lines`, `pull_payments`, `_apply_qbo_deletes` (the last also stays behind `delete_sync_enabled`, default OFF). Initial QB import remains available only pre-cutover.
- **Read-only `qb_*` mirror pulls continue** (**[AUDIT-R2]** correction): the beat-scheduled banking sync (`core/scheduler.py:128` → `tasks.py:418`) writes only mirror tables (`banking.py:36`) and is untouched — it becomes Phase 2 reconciliation input.
- **Known workflow loss, accepted:** CPA edits made in QBO no longer flow back. Corrections happen in GDX (credit memo, void, adjustment) and push forward. **[JUDGMENT]**
- **Known gap, accepted for Phase 1** (**[AUDIT-R2]**): there is **no automated detection of QBO-side divergence** (e.g., a CPA-recorded ReceivePayment leaves the GDX invoice open). Interim mitigation: the §11 monthly hand-check against QBO's AR/trial balance. Permanent fix: Phase 2's verification loop — this gap is a design input to that document.

### 5.5 Expenses

As v2: P5 (validated category; lines must sum to header; `gt=0` closes the $0/CHECK conflict), P6 on every mutation path incl. the separate line-add endpoint (:213), paid-on-`date` simplification **[JUDGMENT][CPA]**, manual JE for sales-tax remittance.

### 5.6 Engine mechanics

- **Synchronous, in-transaction**; engine never commits (§3.5).
- **Idempotency keys with liveness** (**[AUDIT-R2]** fix for the A→B→A eraser; formula corrected per **[AUDIT-R3]**): key = `{source_type}:{source_id}:{event}:{sha256(canonical money fields)[:16]}:{seq}` where **`seq` = count of `reversed` entries** for the same `(source, event, hash)` prefix — NOT count of all entries (that formula would mint a fresh key on a plain retry and double-post; a retry must land on the same key as the live entry). Collision handling: the insert runs inside a SAVEPOINT; on unique-violation, roll back to the savepoint (never aborting the enclosing operational transaction), SELECT the colliding entry — **`posted` → return it (idempotent success); `reversed` → recompute `seq` and retry**. Reversal entries use their own key form `reversal:{original_entry_id}` (§3.5), which is unique by construction. `seq` derives from ledger state, so backfill replay reconstructs identical keys in order. Property tests: plain retry → one live entry; A→B→A → exactly one live entry at content A.
- **Concurrency:** no balance preconditions in Phase 1; unique key is the guard; `SELECT FOR UPDATE` if preconditions arrive (MT pattern). **[JUDGMENT]**
- **Lock check** per §3.6. **Backfill** `tools/gl_backfill.py`, re-runnable by construction.

### 5.7 Cutover & opening balances ([AUDIT-R2] rewritten)

- **P8 opening balances are posted per open invoice** (one AR debit line per invoice, `customer_id` dimension, credit 3950) — not one AR lump. This gives every pre-cutover invoice an anchor entry.
- **Cash-basis proration for post-cutover payments on pre-cutover invoices** reads the invoice's operational `InvoiceLine` rows (they exist in the DB regardless of cutover) for the category split. **This is a report-time derivation only** (**[AUDIT-R3]** clarification) — no 4xxx journal entries are posted for pre-cutover revenue; the cash-basis P&L computes these rows on the fly, memo-tagged `pre-cutover`. The "live P1" anchor rule applies only to post-cutover invoices.
- **P8 anchors are immutable in a stronger sense** (**[AUDIT-R3]**): opening-balance entries are never reversed-and-reposted. Post-cutover changes to a pre-cutover invoice happen exclusively through the ruled endpoints — payment (P3), credit memo, refund, void — each of which posts its own entry against the anchor. Direct edits to pre-cutover invoice totals/lines are blocked (they are all issued invoices; issued-invoice edit paths already route through reversal+repost, which for a P8-anchored invoice is instead rejected with "use a credit memo").
- **Opening AR per invoice = `total − SUM(non-voided payments) − SUM(historical credit memos)`** — the credit-memo term matters because today's endpoint mutated `amount_paid`, which the payments-sum ignores (**[AUDIT-R2]**). Cutover procedure includes a per-invoice reconciliation against QBO's AR aging; discrepancies are resolved by hand before the flag flips (this hand-check is the seed of Phase 2's automated loop).
- Opening bank balance is owner-attested until Phase 2 feeds verify it.

## 6. Accrual GL, cash-basis reports

Architecture verified against Intuit's own implementation (one accrual store, report-time cash derivation with proportional allocation; their worked example — $1,060, $424+$636 → $24+$36 tax — is a unit test). Hardening: proration anchors to the live P1 (or opening/operational-line anchor per §5.7); void ordering enforced; overpayment excess in 2300, never prorated (cap 1.0); **refunds/credit memos prorate negatively at their `effective_at`** (§5.2); manual JEs touching both a BS and P&L account appear under both bases (QBO edge case, verified); expenses identical under both bases as a documented Phase 1 simplification. §448 (verified 3-0) poses no conflict between an accrual GL and a cash-basis return for this entity profile. **[CPA]** confirm basis + entity type; §446 question in §12.

## 7. Inventory: deliberately NOT on the GL in Phase 1

Unchanged (no audit findings). 26 CFR §1.471-1(b)(6) Examples 6/7 (verified 3-0): books can bind the tax position — a perpetual inventory asset could force ending-inventory capitalization on a return that expenses parts. Parts purchases post as expense (P5→5000); `InventoryItem`/`StockAdjustment` stay operational-only; the NIMS upgrade path (FIFO or weighted-average; LIFO barred; COGS at later-of-payment-or-providing) is documented, **not built until the CPA chooses**. **[CPA — the most important pre-build question in this document.]**

## 8. Rounding discipline

Unchanged (verified: dinero.js/RubyMoney allocate, Fowler Money, Stripe/Avalara/Xero, Odoo/Xero/SAP rounding accounts): one sum-preserving largest-remainder `allocate()`; invariant `sum == total`; residuals → 6990; floats lint-banned on money paths.

## 9. Reports

As v2: trial balance (rendered zero-proof; Phase 2 reconciliation anchor), P&L with basis toggle, balance sheet (computed retained-earnings rollup), journal browser with drill report→entry→line→source document (expense→receipt photo). Expense detail shows the posted CoA account. "Accounting" nav group on existing `accounting.read`/`accounting.write`.

## 10. Testing & verification gates

- **Writer-inventory test** (the enforcement): every §5-table path posts or is disabled; new writers fail CI. Plus the **CI lint** on raw Core writes to money tables (§2).
- **Property tests:** sum-to-zero; allocate exactness; key liveness (A→B→A → one live entry); same-state replay → identical ledger.
- **Trigger tests:** UPDATE/DELETE raise; exempt transitions; unbalanced rejected; cross-transaction line INSERT rejected; **documented `--disable-triggers` restore path exercised in the next `/backup` restore drill** (**[AUDIT-R2]**).
- **Proration tests:** Intuit example exact; non-even splits; overpayment→2300; refund negative-proration; pre-cutover invoice proration from operational lines.
- **Lifecycle tests:** draft-paid-in-full; issue→partial-pay→void ordering; credit memo (both reasons); refund; apply-credit (P9); expense chains; front-office Stripe confirm/ACH create Payment rows; QB pull attempt while ledger on → loud failure.
- **Process gates:** `/audit` round 3 (delta check) on this doc; `/audit` on implementation before merge; headed-browser verification on the real account.

## 11. Rollout

1. Migration 012: GL tables + triggers + CoA seed + `payments.voided_at` + `job_receipts.promoted_expense_id` + `invoice_adjustments`; router fixes ship with it (Stripe recording consolidation, refund enum, overpayment validation, expense `gt=0`, `_mark_invoice_paid` refactor, dead-code deletion).
2. Engine dark behind `ledger_posting_enabled` (default off); **the flag and the pull-path disable are the same switch**. Dark-launch validation is defined, not vibes: daily trial-balance zero-check + §5 writer-coverage counters on the demo stack.
3. Enable on prod at a month boundary: final QB import → per-invoice opening balances + AR reconciliation vs QBO aging (§5.7) → flag on → replay from cutover.
4. Reports + receipts UI exposed once the trial balance passes the monthly hand-check against QBO (repeated monthly until Phase 2 automates it — this is also the §5.4 divergence mitigation).

## 12. Open questions & flagged defects

**Pre-existing production defects (fix regardless of GL timeline):**

- **Front-office AND portal Stripe paths would move real money with no Payment row** (`core/payments.py:127/:212/:295`; `routers/payments.py:235`; real webhook `stripe_webhook.py:15` logs only; `core/payments.py:416` handler is dead code). **Urgency downgraded 2026-07-02: Doug confirms Stripe is not currently in use — this is a dormant defect, not live leakage.** Must be fixed before any Stripe activation; run the prod Stripe-history sweep once to confirm it's empty. **[AUDIT-R1+R2]**
- `/refund` writes enum-invalid `status="refunded"` (`models:344-348`).
- `/send` resurrects voided invoices (:1030).
- `/credit-memo` mutates deprecated `amount_paid`, which balance recomputation ignores (:1473 vs :192-196).

**CPA questions:**

1. **[BLOCKING §7]** Expense parts at purchase or capitalize inventory on the return? Decides Phase 1.5.
2. Filing basis + entity type (§448 verified inapplicable either way; confirm no syndicate wrinkle).
3. **[Phase 3 — §446(a) books-conformity]** Once GDX is the book of record, does an accrual internal book pressure the cash-basis return under §446(a)? Asked now so the answer exists before cutover. **[AUDIT-R1]**
4. MN sales-tax filing basis (what 2100 must tie to).
5. 4900/4910 contra-revenue split and credit-memo reason taxonomy. **[AUDIT-R2]**
6. Payment-method→account mapping (1000/1050); Stripe fees stay gross-until-Phase-2 (~2.9% of card volume unexpensed interim — confirm acceptable).

## 13. Sources

Primary sources as v2 (Modern Treasury ledger series + floats; Square Books; TigerBeetle; Uber; Intuit cash/accrual + proration example; IRC §448, 26 CFR §1.471-1, TD 9942, Rev. Proc. 2022-9; IRS Pub 463/583, Rev. Proc. 97-22; Intuit lock-books, Xero lock dates, MT effective_at, Fowler timeNarrative; dinero.js, RubyMoney, Fowler Money, Stripe, Avalara, Xero, Odoo rounding). Audit rounds 1–2 (2026-07-01): codebase citations verified by auditors against `invoices.py`, `payments.py`, `core/payments.py`, `mobile_invoicing.py`, `expenses.py`, `sync.py`, `banking.py`, `scheduler.py`, `tenant_models.py`, `app.py`.
