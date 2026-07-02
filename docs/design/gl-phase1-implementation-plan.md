# GL Phase 1 — Implementation Plan (slice breakdown)

**Purpose:** turn `gl-phase1-core-ledger.md` (v3 FINAL-DRAFT spec) into a sequence of
small, independently-testable slices. The spec says *what* and *why*; this doc says
*in what order* so we never have a huge un-mergeable branch and every step has a green
checkpoint.

**Date:** 2026-07-02 · **Branch:** `feat/gl-phase1` (own branch, off `main`; current
`feat/measurement-diagram` has unrelated uncommitted work).

## Guiding rules

1. **Foundation merges before any writer is touched.** Tables, triggers, engine, and
   enforcement scaffolding are all *additive* and dark behind `ledger_posting_enabled`
   (default **off**). They can land on `main` with zero behavioral change.
2. **Nothing posts until the chokepoint exists.** `transition_invoice_status` +
   `post_for_event` are the only legal money writers; retrofits come after the engine
   is proven in isolation.
3. **CPA answers are *settings*, not code.** The CPA-dependent choices — CoA taxonomy
   (§4900/§4910 split, payment-method→account map), reporting/tax basis, and the
   *inventory capitalize-vs-expense* decision (Phase 1.5, §7) — live on an **Accounting
   Settings page** (S2 backend + S4.5 UI), not as code constants. This dissolves the
   "blocked" framing: we ship with sane defaults and the CPA reviews/edits them in the
   browser, no deploy. Accounts are deactivate-not-delete, so a later taxonomy change is
   cheap while the flag is off and nothing has posted.
4. **The engine posts to account *roles*, never to numbers.** `gl_accounts` carries a
   stable `role` (`AR`, `UNDEPOSITED`, `SALES_TAX_PAYABLE`, `CUSTOMER_CREDITS`,
   `SALES_FALLBACK`, `DISCOUNTS`, `REFUNDS`, `OPENING_EQUITY`, `ROUNDING`, …). Posting
   rules bind to roles; the settings page lets the operator rename/renumber the display
   code+name and add non-system accounts without ever breaking the machinery. This is
   what makes a user-editable CoA and deterministic posting coexist.
5. **Each slice: its own migration is idempotent** (`to_regclass` guards, ORM tables via
   `create_orm_tables()` first per issue #41 ordering), ships with tests, and is green
   before the next starts.
6. **Fold the pre-existing bug fixes into the slice that already touches that code** so
   we don't refactor the same call site twice. (All four §12 defects are Stripe-dormant
   or low-blast-radius; none is live leakage — Doug does not use Stripe.)

## Module boundary

GL is **its own code package `modules/ledger/`** (`models.py`, `engine.py`, `keys.py`,
`money.py`, `coa.py`, `router.py`, `service.py`) — same convention as `modules/forecasting`
/ `modules/quickbooks`. It is **not** a licensable tier-module in `core/modules.py`
`MODULES`: the GL is core financial infrastructure, gated by the operational flag
`ledger_posting_enabled` + the existing `accounting.read`/`accounting.write` caps, not a
sales tier (and the per-tenant grant machinery is vestigial under single-tenant anyway).

- The engine is **called from outside** the package (`routers/invoices.py`,
  `core/payments.py`, `routers/mobile_invoicing.py` import `post_for_event` /
  `transition_invoice_status`). The S4 CI lint bans raw Core writes to money tables
  *outside* `modules/ledger/`, not calls *into* it.
- `transition_invoice_status` (the sole legal `Invoice.status` writer) lives **in**
  `modules/ledger/` and is imported by the retrofit call sites.
- `modules/ledger/models.py` must be imported at bootstrap **before**
  `create_orm_tables()` (#41 ordering) so the `gl_*` tables register on `TenantBase`
  metadata. GL-native tables (`gl_*`, `expense_receipts`) live here; `invoice_adjustments`
  (operational-adjacent) can live here or beside `Invoice` — decided in S7.

## Slice map

| # | Slice | Mergeable | Flag-gated | CPA-gated | Folds bug fix |
|---|-------|-----------|-----------|-----------|---------------|
| S1 | Ledger data model + triggers (mig 012) | ✅ now | — | no | — |
| S2 | Chart of accounts (+ roles) + config store | ✅ now | — | defaults only | — |
| S3 | Posting engine core (isolated) | ✅ now | — | no | — |
| S4 | Chokepoint + enforcement scaffolding | ✅ now | flag plumbing | no | — |
| S4.5 | Accounting Settings page (UI) | ✅ now | edits flag | reviews defaults | — |
| S5 | Invoice issuance posting (P1) | after S4 | yes | no | #3 /send void-resurrection |
| S6 | Payments posting (P3/P4) + `voided_at` | after S5 | yes | method→acct map | #1 Stripe no-Payment-row, #2 /refund enum |
| S7 | Credit memos / refunds / apply-credit (P9) | after S6 | yes | 4900/4910 split | #4 /credit-memo amount_paid |
| S8 | Expenses posting (P5/P6) + receipts | after S4 | yes | expense simplification | — |
| S9 | QB pull disable under flag (§5.4) | after S5 | yes | no | dead-code deletion |
| S10 | Cutover + opening balances (P8) + backfill | after S5–S9 | yes | switch-month | — |
| S11 | Reports + receipts UI + accounting nav | after S5+ | reads flag | no | — |

S1–S4.5 form the **foundation PR set** — all can go to `main` immediately, in order, with
no posting behavior (S4.5 adds a settings screen but nothing posts while the flag is off).
S5+ live behind the flag until the cutover (§11 of the spec).

---

## S1 — Ledger data model + triggers  *(spec §3)*

**Goal:** the immutable journal exists in the DB with all integrity rules, but nothing
writes to it yet.

- `modules/ledger/models.py` — `GlAccount`, `GlJournalEntry`, `GlJournalLine`,
  `GlPeriodLock` (ORM; created by `create_orm_tables()` first per #41).
- Migration `012_gl_core.py` — ships the **triggers** (tables already exist by create_all):
  - balance invariant: deferred constraint trigger at commit — `SUM(amount_cents)=0`,
    `COUNT(*)>=2`, ≥1 positive & ≥1 negative (§3.4).
  - immutability: `BEFORE UPDATE OR DELETE` raise on both tables, with the two validated
    column exemptions on entries (`status: posted→reversed`,
    `reversed_by_entry_id: NULL→value`) (§3.5).
  - sealing: lines INSERT only when `created_txid = txid_current()` (§3.5).
  - `amount_cents <> 0` CHECK; bigint signed cents.
- **`--disable-triggers` restore note** added to the `/backup` runbook (§3.5 caveat).
- **Tests:** UPDATE/DELETE raise; exempt transitions pass; unbalanced rejected;
  cross-transaction line INSERT rejected. (Property tests for balance come in S3.)

**Done when:** trigger tests green; `alembic upgrade head` idempotent on fresh + existing DB.

## S2 — Chart of accounts (+ roles) + config store  *(spec §4)*

The engine binds to **account roles**, never numbers (guiding rule 4), and reads its
mappings from a config store the settings page (S4.5) edits — so S3 has no hardcoded
account constants.

- `gl_accounts.role` — stable enum the engine posts to (`AR`, `UNDEPOSITED`,
  `SALES_TAX_PAYABLE`, `CUSTOMER_CREDITS`, `SALES_FALLBACK`, `DISCOUNTS`, `REFUNDS`,
  `OPENING_EQUITY`, `ROUNDING`, `WAGES`, `PAYROLL_TAX`, …). `is_system` rows own a role
  and can be renamed/renumbered but not deleted or role-reassigned.
- `modules/ledger/coa.py` — starter CoA as a data table (code/name/type/parent/role/
  is_system). Idempotent seed with sane defaults (1200 AR engine-only, 1000/1050, 2100,
  2300, 3950, 4000 fallback, 4900/4910, 5000/5100, 6050/6060, 6900/6990 …).
- **Config store** — a `gl_settings` singleton (dedicated table, mirroring the
  `AppSettings` precedent that already carries `qb_accounting_method`): payment-method→role
  map, credit/refund-reason→role map (4900 vs 4910), reporting/tax basis, inventory
  treatment (expense vs capitalize), cutover month, entity/filing basis, opening-bank
  attestation, and `ledger_posting_enabled`. Seeded with defaults; per-key `[CPA] reviewed`
  stamp. Engine reads role→account resolution + these maps from here.
- Remove `ExpenseLine.account` free-text from the **UI** — deferred to S8 (expense posting);
  here just the model + seed.
- **Tests:** seed idempotency; every role has exactly one active system account; fallback
  `SALES_FALLBACK`/4000 exists; config-store defaults load; engine role-resolution helper
  returns the mapped account.

## S3 — Posting engine core (isolated)  *(spec §5.6, §8)*

- `modules/ledger/money.py` — one `allocate()` largest-remainder (sum-preserving),
  ROUND_HALF_UP boundary fn, residual→6990; floats lint-banned in `modules/ledger/`.
- `modules/ledger/keys.py` — idempotency key
  `{source}:{id}:{event}:{sha256(canonical fields)[:16]}:{seq}`, **`seq` = count of
  `reversed` entries** for the prefix; SAVEPOINT collision handling
  (posted→return, reversed→recompute seq & retry); reversal key `reversal:{entry_id}`.
- `modules/ledger/engine.py` — `post_for_event(session, event)`: in-transaction,
  **never commits**; pre-asserts balance in Python; period-lock check (§3.6); reversal
  helper (negate all lines).
- **Tests (property):** sum-to-zero; allocate exactness incl. non-even; key liveness
  (A→B→A → exactly one live entry at content A); same-state replay → identical ledger.
  Driven by *synthetic events* — no operational writers wired yet.

**Done when:** engine posts/reverses/idempotency all green against hand-built events.

## S4 — Chokepoint + enforcement scaffolding  *(spec §2, §11)*

- `transition_invoice_status(session, invoice, new_status)` — becomes the *only* legal
  writer of `Invoice.status`; initially a pass-through that calls `post_for_event` **only
  when the flag is on** (still off → identical behavior).
- `ledger_posting_enabled` feature flag (default off) — same switch also gates the S9
  pull-disable.
- **Writer-inventory test** — greps/imports every §5-table path; asserts each posts or is
  disabled; new writer → CI red. This is the real enforcement.
- **CI lint** — forbid raw Core writes (`.__table__.delete()/update()`,
  `session.execute(update(...))`) to money tables outside `modules/ledger/`
  (`sync.py:372` is the known offender).
- **Flush guard** — `before_flush` tripwire (dev/test raise, prod log-only).

**Done when:** flag off = no behavior change; writer-inventory + lint green; both wired to CI.

## S4.5 — Accounting Settings page (UI)  *(new — surfaces S2 config)*

The browser home for every CPA-dependent choice, so the CPA reviews the seeded CoA and
mappings *before anything posts* and the answers arrive as data entry, not a deploy.
Follows the existing settings convention (`AdminSettingsView.vue` + a `admin_settings`-style
router; `qb_accounting_method` is the precedent for a tenant accounting toggle).

- **CoA editor** — list/rename/renumber/add/deactivate accounts; role bindings shown;
  system-role rows protected (can't delete or reassign role).
- **Mappings** — payment-method→account-role (1000 vs 1050, Stripe fee handling);
  credit/refund reason→4900/4910.
- **Policy toggles** — inventory treatment (expense vs capitalize — the Phase 1.5 gate);
  reporting/tax basis (cash/accrual); entity type/filing basis; cutover month; opening-bank
  attestation; per-setting `[CPA] reviewed` stamp.
- **Master switch** — `ledger_posting_enabled` with a one-way-door confirm.
- **Guardrails (guiding rule 3 / §7):** inventory method, cutover month, and the
  method→role map render **read-only-with-reason** once the flag is on or any entry has
  posted (mirrors QBO "can't change accounting method after transactions").
- **Perms:** `accounting.write` to edit, `accounting.read` to view.
- **Tests:** guardrail lock after first posted entry; role-protected accounts reject
  delete/reassign; config round-trips to the S2 store.

**Done when:** page edits the S2 config store; guardrails enforced; system accounts safe.

---

## S5 — Invoice issuance posting (P1)  *(spec §5.1)*  · behind flag

Retrofit the ~7 live status writers through `transition_invoice_status`
(`invoices.py:202/1004/1030/1486/1524`, `mobile_invoicing.py:504/624`). P1 = debit 1200 /
credit 4xxx per line (NULL→4000) / credit 2100 (mirrored tax). Auto-flip draft→paid posts
P1 before P3 (negative-AR structurally impossible). Post-issuance total/line edits →
reverse live P1 + repost at current content (§5.6 keys). **New `POST /invoices/{id}/void`;
block /send resurrection of voided invoices (bug #3).** Lifecycle + reversal tests.

## S6 — Payments posting (P3/P4)  *(spec §5.3)*  · behind flag

`payments.voided_at` column. P3 (debit 1000/1050, credit 1200); P4 void reversal +
`POST /payments/{id}/void`; overpayment→2300 opt-in (cap 1.0). **Refactor
`core/payments.py:_mark_invoice_paid`** — drop mid-flow commit, create idempotent Payment
row (bug #1, Stripe consolidation), route through the transition helper. **`/refund` enum
fix (bug #2).** Partial-pay/void-ordering + overpayment tests.

## S7 — Credit memos / refunds / apply-credit  *(spec §5.2, P9)*  · behind flag

`invoice_adjustments` table. Credit memo (debit 4900/4910 per reason, credit 1200, cap =
remaining balance); refund (debit 4910/credit 1000, `refund_method` required); apply-credit
`POST /invoices/{id}/apply-credit` (P9: debit 2300/credit 1200, `SELECT FOR UPDATE` on
credit rows, dual cap). `_recalculate_invoice` learns the adjustments table
(`balance_due = total − Σpayments − Σadjustments`) — **fixes bug #4** (stop mutating
`amount_paid`). Both-reasons + negative-proration + cap tests.

## S8 — Expenses posting (P5/P6) + receipts  *(spec §5.5, §3.7)*  · behind flag

`expense_receipts` table (SHA-256 content-hash, soft-delete-only, ≥7yr retention);
`job_receipts.promoted_expense_id`; promote-from-field flow. P5 (validated category, lines
sum to header, `gt=0`) / P6 on every mutation incl. line-add `:213`. `ExpenseLine.account`
UI → display posted CoA account. Expense-chain tests.

## S9 — QB pull disable under flag  *(spec §5.4)*  · behind flag

When flag on: `pull_invoices`, `_resync_invoice_lines`, `pull_payments`,
`_apply_qbo_deletes` fail loudly + surface on `/qb` health. Read-only `qb_*` mirror pulls
(banking sync) untouched. Delete/deprecate dead `core/quickbooks.py:433` +
`core/payments.py:416`. Pull-attempt-while-on → loud-failure test.

## S10 — Cutover + opening balances (P8) + backfill  *(spec §5.7)*  · behind flag

P8 per-invoice opening balances (AR debit per invoice + `customer_id` dim, credit 3950,
never reversed). Opening AR = `total − Σnon-voided-payments − Σhistorical-credit-memos`.
Cash-basis proration for post-cutover payments on pre-cutover invoices = **report-time
derivation only** from operational `InvoiceLine` rows (no 4xxx JEs posted).
`tools/gl_backfill.py` re-runnable. Per-invoice AR reconciliation vs QBO aging. Proration
tests incl. the Intuit worked example.

## S11 — Reports + receipts UI + accounting nav  *(spec §6, §9)*  · reads flag

Trial balance (zero-proof), P&L (accrual/cash toggle, report-time cash derivation w/
proportional allocation), balance sheet (computed RE rollup), journal browser drill
(report→entry→line→source doc, expense→receipt photo). "Accounting" nav group on existing
`accounting.read`/`accounting.write`. Intuit `$1,060 → $24+$36` example as a unit test.

---

## Rollout (after slices land — spec §11)

1. Foundation (S1–S4.5) → `main`, no posting behavior; CPA reviews the seeded CoA +
   mappings on the S4.5 settings page.
2. S5–S11 behind `ledger_posting_enabled=off`; dark-launch validation on the **demo stack**:
   daily trial-balance zero-check + §5 writer-coverage counters.
3. Enable on prod at a **month boundary**: final QB import → P8 opening balances + AR
   reconciliation vs QBO aging → flag on → replay from cutover.
4. Reports exposed once TB passes the monthly hand-check vs QBO (repeated monthly until
   Phase 2 automates the divergence loop).

## Still-open gates (from spec §12 — do not need answers to start S1–S4)

- **[CPA blocking Phase 1.5]** parts expensed vs inventory-capitalized (§7).
- **[CPA]** filing basis + entity type; MN sales-tax basis (what 2100 ties to);
  4900/4910 taxonomy; switch-month; method→account map.
- **[Phase 2 re-verify]** MT cardinalities, Stripe balance-transaction semantics, QBO CDC
  (spend-limit killed that pass — re-run before Phase 2).
- **pnl.py** ColData index parsing breaks at Reports v2 cutover **2026-08-31** — Phase 2
  work item, due regardless.
