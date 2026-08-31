# Invoicing Gap Fixes — 2026-08-08

**Status:** **COMPLETE** — #293, #294, #295 and #296 are all MERGED and PR 5's
work is on main (verified 2026-08-21): `core/invoice_delivery.py`
`require_deliverable`, the overpayment banner at `InvoiceDetailView.vue:535`,
typed-$0 lines surviving at `InvoiceCreateView.vue:814`, and
`invoices.py:1223 zero_price_warnings`. The "Still open" list at the end is
other documents' work, not this one's.

Final report for the invoicing-gaps sweep Doug requested ("make a todo list
of all of these gaps, start a clean update new branch and stack the PRs as
you go; fix other gaps as you find them; give a final report in a file").

Everything below started from the 2026-08-08 invoicing-lifecycle audit
(draft → verify → send → pay), run the day after the closeout-autodraft
feature (v1.43.0) made machine-priced drafts an everyday object.

## The stack

Five PRs, stacked in merge order (each based on the previous):

| # | PR | Branch | What |
|---|-----|--------|------|
| 1 | #293 | `fix/invoicing-rail` | The §11 delivery rail |
| 2 | #294 | `fix/invoice-numbering` | One invoice-number generator |
| 3 | #295 | `fix/invoice-provenance-tax` | Autodraft tax + provenance + accepted-only prefill |
| 4 | #296 | `fix/invoice-dead-endpoints` | Delete the two dead creation endpoints |
| 5 | (this PR) | `fix/invoice-visibility` | Overpayment banner + typed-$0-line fix + this report |

Every PR ran the **full 7-shard backend matrix locally before push**
(≈5,600 tests, zero failures each time) plus the full vitest suite
(1,433) and a production build. Merge order matters: #293 first.

## 1. The §11 delivery rail (#293) — the systemic finding

**Gap:** `verified_at` — the office-review stamp the autodraft design
depends on — was enforced on exactly two endpoints, both mobile. Every
desktop path delivered unverified drafts: one-click Send on Draft rows,
bulk Send-Selected over the Draft filter, email-compose → Outlook →
mark-sent, Mark-as-Mailed (which also fed drafts into the auto-dunning
population), pay-link, send-receipt, and manual send-reminder (which had
no status filter at all — it could dun a draft or a $0 invoice).
email-compose had **no guard of any kind** and composed voided invoices,
minting pay tokens and embedding live pay URLs. Worst: the public
**/pay/{token} page rendered the full Stripe form for DRAFTS and charged
them** — and a paid draft auto-flipped straight to `paid`.

**Fix:** one shared gate, `core/invoice_delivery.require_deliverable`:
a DRAFT may not be delivered or paid until a human verified it. Invoices
already past draft cleared the gate at issue time — deliberately, so the
historical book (verified_at NULL, status sent/paid) keeps re-sending
with **no backfill migration**; a test pins the grandfather rule.
/pay answers **404** for drafts (a leaked pre-issue token reveals
nothing). `POST /verify` moved from `invoices.read_all` to
`invoices.write` — verification approves money.

UI: Send and Mark-as-Mailed offer **verify-and-continue** in one confirm
(the office click is the review moment, recorded as the reviewer); bulk
send partitions out unverified drafts with a toast before confirming —
single-invoice review stays one click, bulk verify is deliberately
impossible; the Copy-pay-link button hides on unverified drafts.

## 2. One invoice-number generator (#294)

**Gap:** four generators coexisted. The count-based one
(`count(*) + 1`, no deleted_at filter, no fallback) re-issued
already-taken numbers whenever count and max diverged — soft-deleted
rows, hex-format or imported numbers. Concurrent creation raised an
uncaught IntegrityError → raw 500.

**Fix:** `core/closeout_billing.next_invoice_number` is the one
generator: high-water mark over the fixed-width `LIKE 'INV-______'` set,
bump past any takers, hex fallback last. The count-based generator
delegates (deposits, office create, mobile, autodraft share one
sequence). `create_invoice` retries once on an invoice_number collision.
Pinned: a soft-deleted number is never re-issued; hex/imported neighbors
don't derail the sequence.

## 3. True numbers, known provenance (#295)

- **Autodraft tax:** the autodraft was the only creation path producing
  `tax_rate NULL` (the legacy flat-tax branch) — its parts were
  structurally untaxable through every later office edit. It now
  resolves the same customer-aware rate the office path uses. The shared
  closeout line-builder stamps `taxable` + `category` on every line
  (labor per the tenant tax-labor flag, parts taxable) — which also
  fixed a **latent mobile bug**: default-True labor lines would have
  been suddenly taxed by the first office line-edit recalc. For GDX's MN
  service work the outcome stays $0 tax on labor — by rule now, not by
  accident.
- **Provenance:** `origin` was stored but never serialized — the office
  reviewed machine-priced invoices with no indication they were
  machine-priced. Serialized now; the invoice page shows an
  **auto-drafted from closeout** tag with a review tooltip.
- **§15.1:** the create screen's estimate prefill took the **latest**
  estimate regardless of status — a draft or declined estimate's prices
  could prefill an invoice the customer never agreed to (and block the
  closeout labor prefill). Accepted estimates only now.

## 4. Dead endpoints deleted (#296)

`POST /api/jobs/{id}/create-invoice` (~400 lines: own numbering scheme,
own tax resolution, CO auto-pull) and `POST /api/invoices/batch`
(lineless $0 shells, a third numbering scheme, a pay token minted per
shell): **zero frontend callers** — every Create Invoice affordance
routes to /billing/new → `POST /api/invoices`. Their tests pinned
unreachable behavior; each retired test carries a tombstone pointing at
the live-path test that pins the same business rule (estimate-precedence
→ autodraft + mobile builders; CO claiming → `from_change_order_ids`
tests).

## 5. Visibility (this PR)

- **Overpayment banner:** the M11 money-audit detector
  (`amount_overpaid`) had been computed and serialized since v1.41.1 but
  rendered **nowhere** — `balance_due` floors at 0, so money collected
  above the total (usually a duplicate payment) was invisible on every
  screen. The invoice page now shows a warn banner with the amount.
- **Typed $0 lines:** the create screen silently **dropped**
  operator-typed $0 lines (warranty/no-charge items) at submit, while
  machine-generated $0 lines sent fine elsewhere. Described lines now
  survive; the tenant's zero-price catalog policy (block/warn) is the
  server-side arbiter, and a block is a visible 422 instead of a silent
  vanish. Create still requires at least one priced line.

## Gaps found along the way (beyond the audit list)

- The mobile latent labor-tax-on-edit bug (fixed in #295, see above).
- The estimate-prefill status hole was confirmed live during the audit
  and folded into #295.
- test fixtures across four suites sent unverified drafts — updated to
  verify-first, which is itself the new contract, pinned.

## Deliberately NOT changed (documented decisions)

- **Office record-payment on a draft** still auto-flips it to paid: the
  money physically arrived and an office human recorded it — blocking
  that would be wrong. The public path can no longer do this (drafts
  404 on /pay).
- **Payment-plan endpoint** (`POST /api/invoices/{id}/payment-plan`)
  remains: backend-only, permission-gated, harmless — likely a future
  feature. Flagged, not deleted.
- **apply-credit** stays wired-but-409ing while GL posting is off
  (existing intended state pending CPA sign-off).
- **api.d.ts** stale entries for deleted routes: generated file, left *[2026-08-31: `api.d.ts` and `openapi-typescript` are gone — nothing imported them; the route table is pinned in `gdx_dispatch/openapi_routes.txt`.]*
  for the next regeneration.
- **sequence_number semantics** (hardcoded 1) — cosmetic; the one path
  that computed it differently was deleted.

## Still open (pre-existing, tracked elsewhere)

- Sales-tax report does not net credit memos / §12 supersessions
  (memory: sales-tax-report-and-adjustment-netting-gap).
- QB paid-status Phase 2 backfill: 26 invoices / $39.8K with the office.
- Dashboard Mark-paid → reviewed_at follow-up.
- Browser walk of the rail UI (verify-and-continue, bulk partition,
  overpaid banner) on prod after deploy.

## Deploy notes

No migrations in this stack. The rail changes **behavior**: unverified
drafts can no longer be sent/mailed/pay-linked — the office will meet
the one-click "Verify and continue" confirm the first time they send a
draft. Verify now requires `invoices.write`.
