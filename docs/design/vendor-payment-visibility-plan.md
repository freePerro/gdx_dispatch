# Vendor payment visibility — closing the loop after we pay a supplier

Status: DRAFT (design only, nothing built)
Date: 2026-08-04

> Repo hygiene: this doc deliberately names no suppliers, processors, account
> numbers, invoice numbers, or dollar amounts. Sender domains and account
> identifiers live in tenant DB settings (`outlook_settings`), never in code
> or docs. Keep it that way in any implementation PR.

## The moment that prompted this

We paid a supplier through their card/ACH processor's portal. The processor
emailed a payment confirmation within the hour — amount, timestamp,
transaction ID, approval code — and the app recorded **nothing**, for four
stacked reasons, each working as designed:

1. The confirmation comes from the *processor's* domain, not the supplier's,
   and the vendor-bill ingest allowlist (`outlook_settings.
   vendor_bill_sender_allowlist`) gates on sender.
2. It is a body-only email; the sweep only ingests PDF attachments
   (`modules/outlook/vendor_bill_ingest.py`).
3. Even a receipt PDF is rejected by classification — receipts are
   deliberately not invoices (`modules/vendor_invoices/llm_extract.py`).
4. Vendor bills have no payment concept at all: `open/paid/void`, and the only
   transition is a manual PATCH from the bill detail page — one bill at a
   time.

So today, "what things look like after a payment" is: **nothing changes for
10–29 days**, then the next supplier statement arrives and the statement-diff
engine tells the story.

## What already works (and works well)

`modules/vendor_statements/account.py` is the heart of it:

- The latest statement is the current position; older ones are history.
- Per invoice: `amount − balance = paid so far` — partial payments are visible
  with no payment records at all.
- Diffing consecutive statements yields `cleared_total + paid_down_total =
  implied_payment_total`, surfaced on the Vendor Statements account card as
  "Since the &lt;date&gt; statement: N new · M paid off, $X applied".
- Verified against prod history: a real lump-sum payment made between two
  statements was recovered by the diff **to the exact cent**, including which
  invoices it cleared and which one it paid down partially. The inference
  machinery is not approximate; it reproduces the supplier's application of
  the money precisely (statement granularity aside).

Also verified on prod: the bills book (`vendor_invoices`) and the statement
position reconcile exactly once you account for coverage windows (bills
ingest started later than statement history, and the newest bill postdates
the newest statement). Join key `vendor_statement_lines.vendor_invoice_no =
vendor_invoices.invoice_number` matched every overlapping row.

## The gaps

1. **The bills book never hears about payments.** The statement view *knows*
   an invoice settled; the bill for that same invoice number stays `open`
   until someone opens its detail page and clicks Mark paid. No suggestion,
   no bulk action, no cross-link anywhere in code (verified: the two modules
   never reference each other).
2. **Latency.** Post-payment visibility is one statement cycle behind
   (10–29 days, irregular). The processor's confirmation email carries
   amount/date/txn-id within minutes, and nothing reads it.
3. **Silence when a statement doesn't come.** The diff merges periods
   invisibly if a statement is missed (acknowledged in `account.py`'s
   docstring). Nothing nags.

## Design

### A. Statement→bills reconciliation suggestions (build first)

After statement ingest, join the latest statement's lines to open bills by
invoice number, per vendor account:

- Line vanished since previous statement, or `balance == 0` → suggest
  **"statement shows settled — mark paid"**.
- Line balance fell but > 0 → surface **"paid down on statement"** on the
  bill (informational; the bill model has no partial state).

Surface as a suggestion strip on the Vendor Bills view with per-row accept
and accept-all. Accepting = the existing `PATCH /api/vendor-invoices/{id}`
status change, attributed to the acting user, with the statement id recorded
in the audit `details`.

**Never auto-post.** A credit memo or return reads exactly like a payment in
the diff; the human click is the control. The diff is labelled inference
everywhere today — suggestions must keep that labelling.

Traps:
- Match within the vendor account (name + code), not globally — invoice
  numbering across suppliers is not unique in principle.
- A bill can exist with no statement history (newer than the latest
  statement): no suggestion, not "unknown" noise.
- Twin statements for one period are already collapsed by the account
  builder; reuse `build_vendor_accounts` rather than re-querying raw rows, so
  suggestion logic inherits that defense.

### B. Vendor payment records from processor confirmation emails

A small `vendor_payments` table: amount, paid_at, method, processor
transaction id (unique — the natural idempotency/dedup key), source message
id, optional vendor account link, free notes. State: `pending` (awaiting
statement corroboration) → `corroborated` (a later statement's implied
payment matches) or `manual`.

Ingest: a new allowlist in `outlook_settings` for *payment-confirmation
senders* (processor domains — DB data, never code). Matching messages are
body-parsed deterministically (the confirmation body is a fixed key/value
layout: amount, date, transaction type, masked account, transaction id,
approval code). No LLM needed; unparseable → skip and count, same
fail-visibly pattern as the statement rung.

Surface: on the vendor account card, "unapplied payment: $X on <date>,
awaiting next statement" — so the position reads `open_balance − pending
payments` *as a labelled projection*, never as the recorded position. When
the next statement's `implied_payment_total` matches a pending payment (exact
first, then tolerance), tie them off and show the corroboration on both
sides.

Notes:
- Which vendor? The processor email names the supplier's corporate entity,
  which may differ from the storefront name on statements. Map via a
  DB-settings alias table (again: names in data, not code).
- This table is evidence + visibility, not GL. If/when the GL program needs
  A/P postings, `vendor_payments` is the source document, but that wiring is
  out of scope here.

### C. Statement-cadence watchdog (cheap)

Per vendor account with ≥2 statements: if `today − latest.statement_date`
exceeds ~1.5× the account's median gap (floor 35 days), surface a nag on the
existing sync-health/dashboard channel: "no statement in N days — the account
position is stale". This also backstops B (a pending payment that never gets
corroborated because the statement never came).

## Sequencing

A is standalone and immediately useful (it retroactively covers every past
payment already visible in statement history). C is an afternoon. B is the
real feature; it depends on nothing but benefits from A's suggestion UI
patterns. Suggested order: A → C → B.
