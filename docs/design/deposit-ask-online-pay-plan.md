# Deposit ask + online-pay-to-count + job "Paid" display fix

Status: **MERGED #346** — Phases 1 and 2 are on main (verified 2026-08-21).
P1: `job_display_state.py:156/162` partitions deposit invoices out of the
money axis, `:89` adds `deposit_paid`, `JobStateChip.vue:48` renders the
badge. P2: `deposit_ask` on the public + portal payloads and the twin
`POST /api/proposals/{token}/deposit/pay` / `/portal/estimates/{id}/deposit/pay`.
**Phase 3 is CLOSED — nothing needed voiding.** Re-examined against the prod
audit trail 2026-08-21: two of the four listed "phantoms" were never phantoms
(INV-000338 was created by the owner four days before the auto-mint bug;
INV-000345 was minted 17.3s after its accept, i.e. by a customer clicking
"Pay deposit"), and the other two are already closed (INV-000340 credit-memo'd,
INV-000341 void). **Voiding to the original list would have destroyed two live
receivables.** Full reasoning in § Phase 3. One unrelated finding surfaced:
prod has zero `invoice_voided` audit rows despite a voided invoice — see there.

## The two reported problems

1. **A deposit makes the job show "Paid."** `derive_job_display_state`
   ([core/job_display_state.py](../../gdx_dispatch/core/job_display_state.py))
   declares a job Paid (terminal, won) when every live invoice is settled —
   but its input assembler `_display_state_for_jobs`
   ([routers/jobs.py](../../gdx_dispatch/routers/jobs.py) ~L569) selects only
   `status/balance_due/amount_paid`, never `billing_type`. Between acceptance
   and final billing the deposit is usually the job's ONLY invoice, so a paid
   deposit = green "Paid" chip while Ready-for-Billing still lists the job.
   `core/billing_predicates.py` already excludes deposits (2026-07-23); the
   display state never got the same lesson.

2. **Phantom deposit invoices.** Public accept
   ([modules/proposals/router.py](../../gdx_dispatch/modules/proposals/router.py) ~L485)
   and portal accept ([routers/portal.py](../../gdx_dispatch/routers/portal.py) ~L1254)
   auto-mint a `billing_type='deposit'` invoice (status `sent`) whenever the
   tenant `deposit_pct` > 0 — before the customer commits any money. Real
   case: EST-000024 accepted via public link 2026-08-17 → INV-000341, $2,291.89,
   $0 paid. Prod 2026-08-18: 4 live deposit invoices with $0 paid carrying
   $5,519.13 of balance_due — inflating A/R, and they will age into "Overdue"
   on the job board (mirror image of bug 1).

## Locked decisions (Doug, 2026-08-18)

- **Keep asking for the deposit** on the public accept page. "Nothing wrong
  if someone wants to give us money."
- **Only an ONLINE payment counts automatically.** No invoice record is
  created just because the customer accepted. The invoice is minted only when
  the customer actually initiates online payment.
- **Check/cash deposits are manual**: the office records them when the money
  physically arrives (existing retroactive endpoint
  `POST /estimates/{id}/deposit-invoice` + payment record; mobile capture
  PR #309 path unchanged).
- **Job card shows a "Deposit paid" badge**, not a "Paid"/money stage. The
  work-axis state (Scheduled / In Progress / Ready to Bill) stays the stage.

## Phase 1 — display-state fix + deposit_paid badge

- Add `Invoice.billing_type` to the `_display_state_for_jobs` select and the
  per-invoice dict (routers/jobs.py).
- In `derive_job_display_state`: partition out deposit invoices before the
  money-axis logic (mirror `invoice_bills_job`). Deposit invoices no longer
  drive Paid/Invoiced/Partially Paid/Overdue. A job reaches "Paid" only when
  its billing-real invoices are settled.
- Add `deposit_paid: bool` to `DisplayState.as_dict()` — true when a live
  deposit invoice has `amount_paid > 0` (or balance settled). Callers that
  don't pass billing_type keep today's behavior (default None → treated as
  non-deposit) so the pure function stays backward compatible for tests.
- Frontend: render a small "Deposit paid" badge on JobStateChip surfaces
  (job board card, JobDetailView). `jobDisplayState.js` is a severity/icon
  map, not a re-derivation — only the badge is new.
- `core/billing_predicates.py` untouched (already correct).

## Phase 2 — no invoice until online pay intent

- Public + portal accept: STOP calling `create_deposit_invoice`. Compute the
  same ask (tenant pct; tier accepts keep the `tier_contract_subtotal` cap
  logic) and return it as `body["deposit_ask"] = {pct, amount}` — no invoice
  row, no pay_url yet.
- New endpoint (public + portal twins), e.g.
  `POST /api/public/proposals/{token}/deposit/pay`:
  idempotent via `find_deposit_invoice_for_estimate` → mints the deposit
  invoice (existing `create_deposit_invoice`, unchanged) → returns
  `deposit_summary` incl. `pay_url`. Called when the customer clicks
  "Pay deposit". Payments-hardening contract holds: the invoice exists
  before any charge, amount stays server-derived.
- Proposal/portal GET: if a live deposit invoice exists → show it (current
  re-surface behavior, unchanged); else if the ask applies and estimate is
  accepted → show the ask + Pay button.
- Office accept dialog: already opt-in (`deposit_amount: None` → nothing);
  unchanged. Manual check/cash path unchanged.
- `apply_deposits_to_final` netting: unchanged; with lazy mint there are
  simply fewer unpaid deposit rows to net around.

## Phase 3 — prod cleanup — **RESOLVED 2026-08-21: nothing to void**

Original instruction, kept for the record: *"Void the phantom unpaid deposit
invoices: INV-000340 ($0.54, looks like a test), INV-000341 ($2,291.89,
EST-000024), INV-000338 ($3,226.70 — may still be genuinely chased), + the 4th
$0-paid row."*

Re-examined against the prod audit trail 2026-08-21 before voiding anything.
**Two of the four were never phantoms, and the other two are already closed.**
Voiding to the original list would have destroyed two live receivables.

| Invoice | Amount | Verdict |
|---|---|---|
| INV-000340 | $0.54 | **Closed** — credit memo issued 2026-08-18 (audited). Exactly the "penny residue → credit adjustment, not a payment" treatment. |
| INV-000341 | $2,291.89 | **Already void.** ⚠ See the audit gap below. |
| INV-000338 | $3,226.70 | **NOT a phantom — do not void.** `deposit_invoice_created` by the owner account on 2026-08-13, four days before the auto-mint bug produced its first row. A deliberate office deposit request. Chase it. |
| INV-000339 | $6,793.04 | The "4th $0-paid row" — also office-created, now **fully paid**. |
| INV-000345 | $2,232.03 | **NOT a phantom — do not void.** See below. |

**Why INV-000345 is not a phantom, and why that also proves Phase 2 works.**
The auto-mint bug created the deposit invoice *inside the accept transaction*:
on 2026-08-17 the public accepts at 13:00:03.719 and 16:34:19.533 minted
INV-000340 and INV-000341 at 13:00:04.138 and 16:34:19.676 — **0.42s and 0.14s
later, same request**. After Phase 2 shipped (#346, v1.67.0, 2026-08-18), the
2026-08-19 accept at 13:41:17.603 minted nothing; INV-000345 appeared at
13:41:34.869 — **17.3 seconds later, a separate request** — and was viewed by
the customer 0.13s after that. That is the customer tapping "Pay deposit" and
landing on the pay page, then not finishing. It is a live intent-to-pay, not
an artefact. The timestamp gap is the cleanest available proof that
mint-at-pay-click is working in production.

**Audit gap found while doing this (unrelated to this plan, worth its own fix):**
`void_invoice` (`routers/invoices.py`) does write `action="invoice_voided"` —
but prod has **zero** `invoice_voided` rows in `audit_logs`, while INV-000341
sits at `status = 'void'`. Something voided a $2,291.89 invoice outside the
endpoint, so the record cannot answer who did it or why. The code honours
invariant #1; this particular mutation bypassed the code.

## Traps

- E2E/spec tests pin the accept response's `body["deposit"]` payload
  (public accept + portal). They move to `deposit_ask` + the new pay
  endpoint.
- Accept is idempotent today via find-existing; the pay endpoint must be too
  (double-click, refresh).
- `deposit_summary.pay_url` is None when Stripe/base-URL unconfigured —
  frontends already degrade; keep that on the new endpoint.
- Do NOT stamp `sent_at` on deposit invoices (PR #192 semantics: sent_at =
  email delivery fact) — `create_deposit_invoice` already respects this.
- The display-state pure function is pinned by unit tests over prod
  permutations — extend fixtures with billing_type rather than rewriting.
