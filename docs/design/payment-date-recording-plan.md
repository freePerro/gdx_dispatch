# Payment Date Recording — Plan (v3, BUILT)

**Date:** 2026-07-30 (v1 researched 2026-07-29; v2 = adversarial audit
against main @ `335b38d`; v3 = Decision 0 resolved by Doug; built + a
second adversarial audit of the code diff the same day)
**Branch:** `feat/payment-date-recording`
**Status:** **MERGED #249 — and the rollout has RUN on prod.** Verified by
read-only prod query 2026-08-21: `tenant_settings.qb_money_pull_paused = true`
for the GDX tenant, so no QB webhook or manual sync can overwrite or duplicate
GDX payment rows; live payments now carry dates across 2024 (98), 2025 (184)
and 2026 (58), which is the backfill this plan's Decision 0 called for.
Code on main: `invoices.py:827 PaymentCreateIn` with the `_no_future_dates`
validator, date pickers at `PaymentCaptureForm.vue:204` and
`BillingView.vue:474/529`, migration 049, and `sync.py:92
_assert_money_pull_allowed` reading the flag fail-closed.
**Adjacent and still open:** `qb-import-paid-status-repair-plan.md` Phase 2,
the office-side mark-paid backfill.

---

## Decision 0 — RESOLVED (Doug, 2026-07-30)

Direction: **bank feeds are coming in and QuickBooks is being phased out.**
GDX is becoming the book of record. The canonical payment date is the
**bank deposit date**. Corrections include **last year's (2025) records** —
so the picker must accept arbitrary past dates, not just recent ones.

Consequences:

1. **Corrections and the backfill happen in GDX** (v2's option B). The
   picker is the tool, including for 2025 dates. No lower bound on the
   date; `max` = today stands (future dates are still nonsense).
2. **The QB money back-flow must be paused before corrections begin.**
   Two verified hazards while it runs:
   - QB webhooks fire `sync_payment_task` on any QB-side payment change
     (`modules/quickbooks/webhook_router.py:200`), and the pull's update
     branch **overwrites `payment_date` from QB** on mapped payments
     (`modules/quickbooks/sync.py:1145-1146`) — a future sync would revert
     GDX-side date corrections on the ~263 QB-imported payments.
   - The pull dedupes by QB entity id only — anything entered in both
     systems mints duplicate Payment rows.
   The designed kill switch exists — `_assert_money_pull_allowed`
   (`sync.py:75-94`) blocks invoice/payment pulls — but it only trips on
   the **GL ledger flag**, which drags in period locks and overpayment
   gates and is pending CPA review. This plan adds a narrow standalone
   pause (see decision 8) so the phase-out doesn't wait on GL.
3. **Bank-deposit-date semantics**: the field records the deposit date by
   office convention. When the bank-feeds program lands, matching will
   reconcile `Payment.payment_date` against bank-transaction posting
   dates — recording deposit dates now means that reconciliation starts
   clean. No schema or label gymnastics needed today.
4. Last-year corrections to *wrong-dated existing payments* use
   **void + re-record with the right date** (void endpoint exists; with
   decision 5 the re-record produces the correct `paid_at` too). A
   dedicated "edit payment date" feature stays out of scope until the
   void+re-record path proves too slow in practice.

---

## Current state (verified 2026-07-30 on `335b38d`)

Backend already accepts a payment date — **zero schema change**:

- `Payment.payment_date` — `Date NOT NULL default today`
  (`gdx_dispatch/models/tenant_models.py:548`); live in prod.
- `PaymentCreateIn.date`, defaults to today (`routers/invoices.py:432-440`).
- `POST /api/invoices/{id}/payments` persists it; GL `post_payment_received`
  409s on locked periods; history sorts by it. Compat `POST /api/payments`
  passes it through.

Frontend gap:

| Surface | Date field? | Sends |
| --- | --- | --- |
| `PaymentsView.vue` /payments dialog | ✅ picker | `form.date`, default **UTC** today (line 239) |
| `InvoiceDetailView.vue` Record Payment | ❌ | hardcoded UTC today (line 1333) |
| `BillingView.vue` Record Payment | ❌ | hardcoded UTC today (line 1281) |
| `BillingView.vue` bulk Mark Paid | ❌ | hardcoded UTC today (line 731) |
| `MobileInvoiceDialog.vue` | ❌ (deliberate) | hardcoded UTC today (line 105) |

**UTC skew bug:** `toISOString().slice(0,10)` is the UTC day — after
~6-7 PM Central every payment is dated *tomorrow*. Fix with
`dateKeyInZone(new Date(), tz)` (`composables/useTenantTimezone.js`;
`/api/me/timezone` at `me_settings.py:43`; null-tz falls back to
browser-local, correct for the office).

## Audit findings carried into the design (all verified in code)

1. **QB pull is a second writer** (id-only dedupe + date overwrite on
   mapped rows) → pause it, decision 8.
2. **The mobile offline queue treats HTTP 409 as SYNCED**
   (`composables/useOfflineSync.js:187-196`), but the payments endpoint
   uses 409 for business refusals (void invoice, closed-out deposit, GL
   period lock) — the phone shows "Payment recorded" for refused money.
   Fixed by decision 7. Pre-existing follow-up to file (not this PR):
   `record_payment` has no idempotency key, so a replayed
   timed-out-but-landed POST double-records.
3. **`paid_at` via MAX(payment_date) was wrong** (v1 design): invoices
   settle to paid with zero payments when fully credited
   (`invoices.py:2357`). Replaced by decision 5's explicit threading.
4. **Held up under attack:** sales-tax report buckets by `invoice_date`
   (`paid_at` only as a boolean) — backdating moves nothing between tax
   periods. Dunning doesn't read `paid_at`. Closeout lanes create no
   payments. Zero-schema-change is real.

## Design decisions

1. **Date field on both desktop Record Payment dialogs**
   (`InvoiceDetailView`, `BillingView`) — `<InputText type="date">`
   (matches the invoice-edit convention), required, default = tenant-zone
   today, `max` = today, **no minimum** (2025 corrections are the point),
   reset on every dialog open. Value stays a `YYYY-MM-DD` string
   end-to-end.
2. **Same single date field on bulk Mark Paid** — one date for the batch
   ("stack of checks deposited on day X"). Defaults to today.
3. **Fix the UTC default in `PaymentsView.vue`** (one line).
4. **Mobile keeps no picker** but stamps tenant-zone today at capture
   time (queue replays keep the capture date).
5. **`paid_at` from the zeroing payment, threaded explicitly** — in
   `record_payment` only: snapshot `was_unpaid = invoice.paid_at is None`
   before `_recalculate_invoice`; after it, if the flip happened in this
   request and `payload.date < today`, set
   `paid_at = datetime.combine(payload.date, min.time(), UTC)` — the
   existing "day known, minute not" convention (QB sync writes it;
   `isDateOnlyStamp`/`formatStampDate` in `useFormatters.js:71-83` render
   it). Same-day payments keep the real `now()`. Credit-memo settlements,
   QB sync, and void-resets-`paid_at` (`invoices.py:2032`) untouched.
   Works identically for a 2025 date.
6. **Future dates:** UI `max` = today; backend rejects `date > today + 1`
   (+1 absorbs tenant-zone vs UTC midnight slack and clock skew; mobile
   capture-time dates are past, so replays are safe). No past-date bound.
7. **Surface payment 409s on mobile** — for `invoice.payment` writes, a
   business-refusal 409 is a failure, not "synced": show the server's
   detail, mark the entry failed. Desktop dialogs already surface the
   detail via `err.message` — verify, don't build.
8. **QB money-pull pause switch** — a narrow tenant setting (e.g.
   `qb_money_pull_paused`) checked inside `_assert_money_pull_allowed`
   alongside the ledger flag, raising the same `QBPullDisabledError` with
   a phase-out message. Blocks webhook-triggered and manual invoice/
   payment pulls without touching GL, customer/item sync, or OAuth.
   Flip it ON before the first correction is entered.

## Out of scope

- QB-pull match-based dedupe (moot — single system of entry + pause).
- Payment idempotency keys for the offline queue (follow-up issue).
- Bank-feed → payment auto-matching (bank feeds program, later phase;
  this plan just records the dates that matching will rely on).
- The other ~40 UTC-slice call sites outside payments; Stripe webhook
  payments (real-time; ±1 day acceptable for now).
- Refund/credit-memo dates (server-stamped).
- A dedicated edit-payment-date feature (void + re-record covers
  corrections; revisit if the volume makes it painful).
- Editing last year's *invoice* dates (sales-tax report buckets by
  `invoice_date` — touching those is an accounting decision, not a
  payments feature).

## Work plan (one PR, ~1 day)

1. Frontend dialogs + defaults + mobile stamp (decisions 1-4).
2. Backend: `paid_at` threading + future-date guard (decisions 5-6).
3. Mobile 409 surfacing for payment writes (decision 7).
4. QB money-pull pause setting + wiring into `_assert_money_pull_allowed`
   (decision 8).
5. Tests
   - Vitest: each dialog sends the picked date; tenant-zone default
     (mock timezone); a 2025 date is accepted and sent; bulk applies its
     date to every POST; mobile payment 409 shows an error, not success.
   - Pytest: backdated zeroing payment ⇒ date-only UTC-midnight
     `paid_at` (including a year-old date); same-day ⇒ real timestamp;
     fully-credited settlement unchanged; partial backdated payment on a
     still-open invoice ⇒ `paid_at` untouched; `date > today+1` ⇒ 422;
     period-lock 409s stay green; pause flag ON ⇒ payment/invoice pulls
     raise `QBPullDisabledError` while customer/item pulls still run.
   - File the idempotency follow-up issue.
6. Verify — headed Playwright on a throwaway container: record a payment
   dated last year on a real invoice from both desktop dialogs; history
   and status reflect it; light + dark. Run the invoices/mobile/QB
   test-matrix shards before release.

## Rollout order (matters)

1. Deploy the release.
2. Flip `qb_money_pull_paused` ON.
3. Only then start entering corrections/backfill in GDX.

## Build outcomes (2026-07-30, post-code-audit deltas vs. the plan above)

The implementation audit (critique_latest.md, diff-hashed) changed four
things from the plan text:

1. **No +1-day slack on the future-date guard.** Single-company system in
   America/Chicago — BEHIND UTC — so a legitimate client stamp can never
   exceed the UTC day; the slack would only have legalized post-dating.
   `PaymentCreateIn` rejects `date > now(UTC).date()`.
2. **The pause reader fails CLOSED** (unexpected read error ⇒ paused),
   with two deliberate open paths: no settings row, and missing
   table/column (a pre-049 schema can't have the flag ON). It reads
   through the caller's session — one DB, no second engine connection.
3. **Control-plane ORM drift fixed**: `TenantSettings` in
   `control/models.py` had been left behind by migration 047 AND lacked
   the prod `DEFAULT now()` on created_at/updated_at — which is why no
   test could ever touch `/api/workflow/flags`. Model now matches prod;
   a real GET/PATCH round-trip test exists and proves the sync gate
   respects what the endpoint writes.
4. **The idempotency follow-up dissolved**: the SS-14 Idempotency-Key
   middleware already covers queued mobile replays (the queue always
   sends the header; true replays get the cached 2xx back). Residual
   exposure only while Redis is down (middleware fails open); desktop
   dialogs are guarded by their button loading state. No issue filed.

Deliberately unchanged after the audit flagged them: the pause stays
default-OFF and procedural (Doug runs deploy → flip ON → backfill
himself — a webhook pull in that window is the residual risk); the
regex source-contract spec stays alongside the real mount tests.

## Acceptance

- A payment deposited May 10 — or any date in 2025 — can be recorded with
  that date from the invoice page, /billing, or bulk, and the invoice's
  paid timing reflects it.
- An evening payment is dated today, not tomorrow — desktop and mobile.
- A refused payment (void / deposit-closed / locked-period) shows the
  server's reason on mobile instead of a false success.
- With the pause ON, no QB webhook or manual sync can overwrite or
  duplicate GDX payment rows; customer/item sync keeps working.
