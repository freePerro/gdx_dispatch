# Accepting an estimate: no way to take a check or cash for the downpayment

Status: **MERGED #309** — P1 through P5 are all on main (verified 2026-08-21).
`components/PaymentCaptureForm.vue` is the shared form, used by
`MobileEstimatesView.vue:260` (P1), `MobileCustomerQuoteDialog.vue:303` (P2),
`EstimateView.vue:75` + `:174` (P3, both the accept and Request-Deposit
dialogs), and `MobileBillingView.vue:120` (P4). P3b prefills `balance_due` at
`InvoiceDetailView.vue:992` and `BillingView.vue:1459`; P4 shipped as option
(a), the tech-scoped list at `mobile_invoicing.py:1105`, not the blanket
`invoices.read_all` grant. P5 is `CustomerPortalView.vue:264`.
_This header read "PLAN — nothing built yet" for shipped work until 2026-08-21._

Doug: *"when accepting an estimate there is no way to accept a check or cash for
a downpayment"*

The downpayment lives on a **deposit invoice**, minted at estimate acceptance.
Every accept surface offers a Stripe card link. The customer writing a check at
the kitchen table has nowhere to go — but *how* badly that hurts depends
entirely on who is standing there, and the first draft of this plan got that
backwards.

All line references below are verified against **this branch**
(`feat/deposit-cash-check-capture`, off `origin/main`). The first draft was
written against a different branch and six of its citations had drifted.

---

## 0.1 What the adversarial audit overturned

Recorded because the corrections are the most useful part of this document.

| First draft claimed | Actually true |
| --- | --- |
| Closeout **autodrafts** a final invoice that voids the unpaid deposit — "fires on the tech's closeout, not the office's schedule" | **False.** `autodraft_invoice_for_closeout` ([closeout_billing.py:311](gdx_dispatch/core/closeout_billing.py#L311)) returns `None` when an accepted estimate exists on the job ([:349](gdx_dispatch/core/closeout_billing.py#L349)) — which is *always* the case for a deposit-taking job — and calls `apply_deposits_to_final` **zero times**. The urgency sentence was invented. |
| Office desktop has no cash/check door at accept — P1 was "highest value" | **False.** The deposit dialog already reads *"record a check/cash payment on the invoice"* ([EstimateView.vue:162](gdx_dispatch/frontend/src/views/EstimateView.vue#L162), and again at [:138](gdx_dispatch/frontend/src/views/EstimateView.vue#L138)) and ships an **Open Invoice** button to the full Record Payment dialog. P1 is a convenience, not a capability. |
| D5: two method spellings in the raw column; P6 to fix it | **False.** [invoices.py:2298](gdx_dispatch/routers/invoices.py#L2298) already does `payload.method.strip().lower()`. Every UI writer funnels through it. **D5 and P6 deleted.** |
| Trap 6.1: `postQueued`'s `Idempotency-Key` is consumed server-side, so mobile replays are deduped | **False.** `IdempotencyMiddleware` bails unless `request.state.principal` is set ([idempotency.py:70](gdx_dispatch/core/middleware/idempotency.py#L70)), and **nothing in production sets it** — the only assignment in the repo is in a test. SS-14 is dead on every route. The plan documented a safety property that does not exist. |
| "No backend change is needed" | **True for P2/P3, false for P4.** See D3. |

The over-billing bug is **still real** (§1) — the audit moved its trigger, not
its existence.

---

## 0.2 What is verified, and what is not

Everything is **code-level, read on this branch**. No prod queries were run —
the harness has no prod DB. Two are worth running before building:

```sql
-- 1. Deposits that went to void unpaid: customers we may have over-billed
SELECT count(*), coalesce(sum(total),0) FROM invoices
WHERE billing_type='deposit' AND status='void' AND deleted_at IS NULL;

-- 2. Deposits ever created, and how many ever got a payment
SELECT count(*) AS deposits, count(p.id) AS with_payment
FROM invoices i LEFT JOIN payments p
  ON p.invoice_id=i.id AND p.voided_at IS NULL
WHERE i.billing_type='deposit' AND i.deleted_at IS NULL;
```

---

## 1. The money bug — corrected

An unrecorded deposit still gets the customer over-billed, and the correction is
still blocked afterward. The trigger is an explicit **"create the final
invoice"** click, not the closeout:

1. Estimate accepted → `create_deposit_invoice` mints a `billing_type='deposit'`
   invoice at status `sent`, balance $X
   ([deposits/service.py:118-246](gdx_dispatch/modules/deposits/service.py#L118-L246)).
2. Customer hands over a **check for $X**. Nothing on the accept result can
   record it. Balance stays $X.
3. Someone builds the final invoice — tech taps *Build invoice*
   ([mobile_invoicing.py:371](gdx_dispatch/routers/mobile_invoicing.py#L371)) or
   the office creates one. **Both** call `apply_deposits_to_final`
   ([mobile_invoicing.py:701](gdx_dispatch/routers/mobile_invoicing.py#L701),
   [invoices.py:1328](gdx_dispatch/routers/invoices.py#L1328)).
4. It sees `paid <= 0.009` and **voids the deposit**
   ([deposits/service.py:360-375](gdx_dispatch/modules/deposits/service.py#L360-L375)) —
   correct for an abandoned deposit, wrong here. No netting line is added.
5. The final invoice bills the **full job total** to someone who already paid $X.
6. The office finds the check later: *"invoice is void — un-void it before
   recording a payment"*
   ([invoices.py:2213-2214](gdx_dispatch/routers/invoices.py#L2213-L2214)).

**Severity, honestly stated:** step 3 requires a human click, so this is not a
silent background timer. But building the final invoice is what you do at the
end of every job — it is the happy path, not an edge case. The window between
accept and that click is the whole exposure, and nothing warns anyone.

---

## 1.1 The reported scenario, exactly

Doug, 2026-08-13:

> *"A customer hands a form of payment for the downpayment, so you go back to
> the office and hit Accept on the estimate and it asks for a downpayment — but
> it only creates the invoice and a link to pay. No way to record a payment that
> has already been received."*

This is the **office desktop accept dialog**, and it is the primary case. Two
things follow that reshape the build:

**The fix belongs in the accept dialog, not the result dialog.** The dialog
already asks *"Collect a deposit at acceptance"* + amount
([EstimateView.vue:53-65](gdx_dispatch/frontend/src/views/EstimateView.vue#L53-L65)).
The operator is holding the money at that exact moment. Asking for the amount
and then handing back a pay link is the wrong ending — the question "was it
already paid?" belongs beside the amount, in one motion.

**The payment date must be backdatable, and this is why.** The money changed
hands *in the field*, possibly days before anyone got to a desktop. If the date
silently stamps "today at the office," every field-collected deposit lands on
the wrong day — and at month-end, in the wrong month. `PaymentCreateIn` already
allows backdating and rejects forward-dating
([invoices.py:616-626](gdx_dispatch/routers/invoices.py#L616-L626)); the form
must expose it, defaulted to today.

---

## 2. Who can accept, who can take money

### Four surfaces accept an estimate and mint a deposit

| # | Surface | File | Result step offers |
| --- | --- | --- | --- |
| A1 | Office estimate page | [EstimateView.vue:154-171](gdx_dispatch/frontend/src/views/EstimateView.vue#L154-L171) | Copy Pay Link · **Open Invoice** · Done — *and copy that names check/cash* |
| A2 | Mobile tech accept | [MobileEstimatesView.vue:226-250](gdx_dispatch/frontend/src/views/MobileEstimatesView.vue#L226-L250) | Copy link · Done |
| A3 | Customer signs on tech's tablet | [MobileCustomerQuoteDialog.vue:313-321](gdx_dispatch/frontend/src/components/MobileCustomerQuoteDialog.vue#L313-L321) | Pay by card · Pay later |
| A4 | Customer portal, own device | [portal.py:1230-1262](gdx_dispatch/routers/portal.py#L1230-L1262) | pay_url only |

`deposit_summary()`
([deposits/service.py:88-102](gdx_dispatch/modules/deposits/service.py#L88-L102))
returns `invoice_id, invoice_number, amount, balance_due, status, pay_url` —
`pay_url` is the only payment affordance in the contract, which is why A2/A3/A4
render card-or-nothing. A1 escapes it only because someone hand-wrote the
Open Invoice button.

### So where is the real dead end?

Not the office. **The field, in one specific shape:**

| Who | Deposit is job-linked | Deposit has `job_id IS NULL` |
| --- | --- | --- |
| Office | Open Invoice → full dialog. **Works.** | Billing list → full dialog. **Works.** |
| Technician | Job invoice dialog ([MobileInvoiceDialog.vue:331-368](gdx_dispatch/frontend/src/components/MobileInvoiceDialog.vue#L331-L368)) lists it and records cash/check. **Works.** | **Nothing.** Not in the job feed (queried by `job_id`, [mobile_invoicing.py:319-322](gdx_dispatch/routers/mobile_invoicing.py#L319-L322)); MobileBillingView 403s (D3). |

A mobile estimate accept mints `job_id NULL` until
`adopt_orphan_deposit_invoices` runs, and that runs **only** inside
`_create_job_from_estimate` ([estimates.py:1740](gdx_dispatch/routers/estimates.py#L1740)).
So the tech who accepts a quote for a not-yet-scheduled job and is handed a
check has no door at all.

---

## 3. Defects

**D1 — no cash/check control on the accept result** *(the ask)*. A2/A3/A4 have
none; A1 has a signpost to another page, not a control. Fix = P1–P3.

**D2 — `deposit_summary()` advertises only `pay_url`.** Fix = treat `invoice_id`
as the payable thing.

**D3 — a technician cannot list invoices at all.** `GET /api/invoices` is gated
on `invoices.read_all` ([invoices.py:755](gdx_dispatch/routers/invoices.py#L755)),
and the `technician` role has **no invoices permission of any kind**
([permissions.py:205-216](gdx_dispatch/core/permissions.py#L205-L216)). So
MobileBillingView's unfiltered `api.get('/api/invoices')` 403s for the field
tier. The **route itself** carries the same guard —
`/mobile/billing` is declared `requiresPermission: 'invoices.read_all'`
([router/index.js:383](gdx_dispatch/frontend/src/router/index.js#L383)) — so a
technician cannot even navigate to the page. **P4 as first drafted was designed
against a screen its only user cannot open.** Fixing it is a backend change
(see P4).

**D4 — MobileBillingView records the wrong date after ~7 PM Central.**
[:340](gdx_dispatch/frontend/src/views/MobileBillingView.vue#L340) sends the UTC
day via `toISOString().slice(0,10)`. Not a 422 (the validator also compares
against UTC, [invoices.py:616-626](gdx_dispatch/routers/invoices.py#L616-L626)) —
just money booked on the wrong day, wrong month at month-end.
MobileInvoiceDialog solved this with `zonedDateKey` and left a comment saying
why; this file never got it. Pre-existing.

**D5 — cash double-tap has no defense, client or server.** See trap 6.1.

**D6 — orphan deposits can be recorded but never netted.**
`apply_deposits_to_final` matches on `or_(job_id, estimate_id)`
([service.py:295-311](gdx_dispatch/modules/deposits/service.py#L295-L311)). The
mobile build path does set `estimate_id`
([mobile_invoicing.py:506](gdx_dispatch/routers/mobile_invoicing.py#L506)) **when
an estimate is found**, so that case nets. The gap is a `job_id IS NULL` deposit
whose job is later created by a non-estimate path, then final-invoiced with
`job_id` only — no match, money recorded, never netted. Narrower than the audit
first framed it, still real.

**D8 — every desktop payment dialog makes the operator re-key the amount.**
Both open at `amount: 0`
([InvoiceDetailView.vue:896](gdx_dispatch/frontend/src/views/InvoiceDetailView.vue#L896),
[BillingView.vue:1455](gdx_dispatch/frontend/src/views/BillingView.vue#L1455)) —
and BillingView's renders **"Balance Due: $2,000.00"** immediately above the
empty field. The amount is known; a human retypes it.

Why this is a money bug and not friction: a mistyped **low** amount ($200 for
$2,000) leaves the deposit *partially* paid. Partially-paid deposits are not
voided at final-invoice time — they are credit-memo'd as "superseded"
([service.py:376-394](gdx_dispatch/modules/deposits/service.py#L376-L394)) — so
the final nets only $200, the missing $1,800 is written off as settled, and the
customer is over-billed by $1,800 while every screen reads "paid". Same damage
as §1, reached by a typo, and quieter. Fix = P3b.

**D7 — `POST /api/invoices/{id}/payments` has no permission gate.** Module gate
only ([invoices.py:54](gdx_dispatch/routers/invoices.py#L54)). This asymmetry is
*why* techs can record money at all, and this plan depends on it. Note the
contrast: a tech can **write** a payment but cannot **read** the invoice list
(D3). Flagged as a decision, not fixed here.

---

## 4. Design decision: client-side second call, not an atomic accept

**Rejected:** folding a payment into `AcceptEstimateIn`
([estimates.py:1772](gdx_dispatch/routers/estimates.py#L1772)).

**Why:** the codebase is emphatic that a deposit failure must never fail an
acceptance — `mobile_quoting.py` catches everything and returns
`deposit_skipped`; `portal.py` does the same. Putting an overpayment 422 on the
critical path of "the customer said yes" is worse than the current bug.

**Chosen:** the accept response already returns `deposit.invoice_id`. Post a
second, independent call to the existing `POST /api/invoices/{id}/payments`
([invoices.py:2201](gdx_dispatch/routers/invoices.py#L2201)), which accepts the
exact payload proposed ([schema at :598-626](gdx_dispatch/routers/invoices.py#L598-L626))
and inherits its void/superseded/overpayment guards. **No backend change for
P1–P3.** P4 is the exception.

---

## 5. The build

**Order: P3b → P3 → P1 → P2 → P4.** P3b is two lines and helps every payment in
the app. P3 is the reported case (§1.1). P1 is the only *total* dead end (a tech
with no job-linked deposit). P2 is the tablet handoff. P4 is parked behind a
permission decision.

### P1 — Mobile tech accept *(now first: the only total dead end)*

[MobileEstimatesView.vue:226-250](gdx_dispatch/frontend/src/views/MobileEstimatesView.vue#L226-L250)

Add the pay form to the deposit-result dialog: `SelectButton` Cash/Check,
amount (prefilled `balance_due`, capped — §6.2), check #, `zonedDateKey` date.
`postQueued` with `actionType: 'invoice.payment'`, `resourceId: invoice_id`,
`conflictIsError: true`, plus an in-flight disable (§6.1).

Extract a shared `<PaymentCaptureForm>` — P2 and P4 both want it, and three
divergent copies is how D4 happened.

### P2 — Customer-signs-on-tablet: a **tech** step after the customer step

[MobileCustomerQuoteDialog.vue:313-321](gdx_dispatch/frontend/src/components/MobileCustomerQuoteDialog.vue#L313-L321)

Tech's device, tech's auth, handed across the counter. Two labelled halves:

- **Customer:** Pay by card · Pay later (both unchanged)
- **Tech — hand the device back:** Paid by cash / check → the P1 form

A customer must never attest their own cash payment.

### P3 — Office desktop accept dialog: "already paid" beside the amount ⭐ THE REPORTED CASE

[EstimateView.vue:45-71](gdx_dispatch/frontend/src/views/EstimateView.vue#L45-L71) ·
handler at [:2502-2524](gdx_dispatch/frontend/src/views/EstimateView.vue#L2502-L2524)

**Re-promoted and re-placed (Doug, 2026-08-13).** The first revision called this
a convenience because Open Invoice reaches a working dialog. Wrong twice: the
desktop path makes the operator re-key the amount (D8), and the fix belongs in
the **accept** dialog, not the result dialog (§1.1).

**Dialog.** Under the existing `collectDeposit` toggle and amount input, add a
second toggle — *"Already paid — record it now"* — revealing the shared
`<PaymentCaptureForm>`: method (full office list), amount defaulted to the
deposit amount above, **date defaulted to today and backdatable** (§1.1),
reference (`Check #` for Check). Cash triggers the confirm per 6.1.

**Handler.** `doAcceptEstimate` currently posts the accept and stores
`result.deposit`. Add: if the payment section is filled and
`result.deposit?.invoice_id` came back, POST the payment as a **second,
independent call** (§4), then re-read so the result dialog reads *paid*.

**On payment failure the acceptance still stands** — never unwind it (§4). Show
the result dialog with the server's `detail` and a retry, so the operator lands
somewhere they can finish rather than re-accepting.

**Result dialog must stop offering a pay link for money already in hand.**
[:154-171](gdx_dispatch/frontend/src/views/EstimateView.vue#L154-L171) shows
*"Send the customer the payment link…"* and a Copy Pay Link button whenever
`pay_url` exists. Gate both on `balance_due > 0`. Handing someone a payment link
for a check you are holding is the wrong-state UI that made this report.

**Same treatment for the retroactive path.** The Request Deposit dialog
([:135-152](gdx_dispatch/frontend/src/views/EstimateView.vue#L135-L152)) serves
estimates accepted before the deposit step existed — which is *also* squarely
the money-already-received case. It gets the same section.

### P3b — Prefill `balance_due` in the two existing desktop dialogs *(do this first — it is two lines)*

[InvoiceDetailView.vue:896](gdx_dispatch/frontend/src/views/InvoiceDetailView.vue#L896) ·
[BillingView.vue:1455](gdx_dispatch/frontend/src/views/BillingView.vue#L1455)

Both open at `amount: 0`. Change to the invoice's `balance_due`. This is not
deposit-specific — it removes the re-key from **every** payment the office
records, which is a much larger surface than this plan's. Cheapest correctness
win in the document.

### P4 — The orphan-deposit safety net *(needs a backend change)*

Two ways, pick one:

- **(a) Tech-scoped list.** Add `GET /api/mobile/invoices?unpaid=1` on the
  mobile router (already gated on `require_module("mobile")`,
  [mobile_invoicing.py:59-62](gdx_dispatch/routers/mobile_invoicing.py#L59-L62)),
  scoped to invoices for the tech's own customers/jobs plus deposits from
  estimates they accepted. Point MobileBillingView at it. Keeps the field tier
  out of the whole AR book.
- **(b) Grant `invoices.read_all` to `technician`.** One line, and it hands
  every tech the entire receivables list. **Not recommended.**

Either way, also fix D4 (`zonedDateKey`) and replace full-balance-only
`markPaid` with the shared form.

### P5 — Portal: an honest check answer *(optional)*

[CustomerPortalView.vue](gdx_dispatch/frontend/src/views/CustomerPortalView.vue) · [portal.py:1230-1262](gdx_dispatch/routers/portal.py#L1230-L1262)

A "Paying by check?" panel: remit-to address, invoice number to write on it, and
*"we'll mark it paid when it arrives."* **Records no money.**

---

## 6. Traps

**6.1 — Cash double-tap has NO defense. Corrected from the first draft.**
The reference-based idempotency ([invoices.py:2266-2291](gdx_dispatch/routers/invoices.py#L2266-L2291))
and migration 056's unique index are both `WHERE reference IS NOT NULL`; cash
carries no reference. The first draft claimed `postQueued`'s `Idempotency-Key`
covered the rest — **it does not.** `IdempotencyMiddleware` returns early unless
`request.state.principal` is set ([idempotency.py:70](gdx_dispatch/core/middleware/idempotency.py#L70)),
and nothing outside tests sets it; it is also only registered when Redis is up.
And each `queueAction` mints a fresh uuid ([useOfflineSync.js:60](gdx_dispatch/frontend/src/composables/useOfflineSync.js#L60)),
so even a live middleware would dedupe a *replay*, never two taps.

**DECIDED (Doug, 2026-08-13): cash gets a confirmation step.** Submitting a
**cash** payment opens a confirm — *"Record $2,000.00 in cash?"* — before the
POST fires. Check does **not** get one: the check # lands in `reference`, which
migration 056's partial unique index already dedupes server-side. Cash is the
only method with no key, so it is the only one that needs the human step.

The confirm does double duty: it debounces the fat-finger double-tap **and**
catches a mis-keyed amount, which is the likelier error given the field is
prefilled from `balance_due`.

Viability verified, because this mechanism has a history in this repo:

- Issue #215 (*"confirm dialogs never render — the fallback silently
  auto-accepts"*) is **closed**, fixed 2026-08-05. `useConfirm()` now resolves
  during `setup()` instead of inside the click handler
  ([useDestructiveConfirm.js:44-61](gdx_dispatch/frontend/src/composables/useDestructiveConfirm.js#L44-L61)).
- `<ConfirmDialog />` is mounted **only** in
  [AppLayout.vue:63](gdx_dispatch/frontend/src/components/AppLayout.vue#L63).
  Mobile routes are `noSidebar`, **not** `noShell`
  ([router/index.js:372-386](gdx_dispatch/frontend/src/router/index.js#L372-L386)),
  so they render inside the shell and get the dialog. **Had mobile been
  `noShell`, `confirmAsync` would never resolve** — the promise hangs, the
  payment silently never records, and it looks like a dead button. Any future
  confirm added to the customer portal hits exactly that, since the portal
  **is** `noShell` ([:169](gdx_dispatch/frontend/src/router/index.js#L169)).
- Place the confirm's accept button **away from** the submit button's position
  so a fast second tap cannot land on it.

**Residual → BUILT (2026-08-13).** The confirm cannot cover the weak-signal
case — tap, confirm, no visible feedback, back out, record again — and the
adversarial audit showed that moving the field surfaces onto the offline queue
made it worse: a request that errors *after* the server commits gets replayed
unattended, with no confirmation in the loop. So the server-side check shipped
in this change: same invoice, same amount, same method, no reference, within
**120s** → 409, in `record_payment`, which already owns dedupe.

Two things that 409 must get right, both done:

- It carries `code: "duplicate_payment"`, because it is the only 409 here that
  means *the money IS recorded*. Every other one (void, superseded, locked
  period) means nothing was written. All four field surfaces branch on it and
  report "Already recorded" — reporting it as a failure is precisely what would
  make an operator re-enter money the server had just protected.
- The confirmation now fires on **any reference-less payment**, not only cash.
  A Check with the number left blank has no dedupe key either.

**Still open, deliberately:** the window is measured from server commit, and the
offline queue has no drain timer and no fetch timeout — so a genuinely lost
response usually replays *after* 120s. Closing that means persisting the
`Idempotency-Key` the queue already sends on every replay (new column + partial
unique index). Left to its own change; do not read the window as covering it.

**6.2 — Overpayment.** Exceeding the remaining balance 422s when ledger posting
is on unless `allow_overpayment`
([invoices.py:2240-2264](gdx_dispatch/routers/invoices.py#L2240-L2264)). **Cap
the input at `balance_due`** and say "record the rest on the final invoice."
Never pass `allow_overpayment` from a tailgate.

**6.3 — Surface the 409s verbatim.** Void and superseded-deposit refusals carry
operator-useful text ([:2216-2235](gdx_dispatch/routers/invoices.py#L2216-L2235)).
Business refusals, not failures — show the server's `detail`.

**6.4 — Tenant-zone date, always.** `zonedDateKey`, never
`toISOString().slice(0,10)` (D4).

**6.5 — Don't touch the netting logic.** Once the payment exists,
`apply_deposits_to_final` does the right thing
([service.py:342-453](gdx_dispatch/modules/deposits/service.py#L342-L453)).
Voiding a genuinely unpaid deposit is correct — the bug is upstream.

**6.6 — A paid deposit must not mark the job billed.** `billing_predicates`
excludes `billing_type='deposit'`, and the mobile feed filters deposits out of
`payment_status` ([mobile_invoicing.py:330-332](gdx_dispatch/routers/mobile_invoicing.py#L330-L332)).
Regression-test it — this is the failure that hides the final invoice.

**6.7 — `sent_at`/`sent_via` untouched.** A payment is not a delivery. A receipt
button would go through [`/send-receipt`](gdx_dispatch/routers/mobile_invoicing.py#L1001).

---

## 7. Tests

Backend — new `test_deposit_payment_capture.py`:

1. Cash on a fresh deposit → balance 0, status paid, job **not** billed (6.6).
2. Deposit paid → build the final → `Less deposit paid` line for exactly the
   paid amount, deposit **not** voided. *Fails on main today.*
3. Deposit partially paid → netting line for the paid part, credit memo for the
   remainder.
4. Overpayment with ledger posting on → 422 (6.2).
5. Duplicate check # → 409, one payment row.
6. Payment on a superseded deposit → 409 with the "final invoice" text.
7. **Double-tap cash** — with the confirm mocked to accept, two identical
   no-reference payments in quick succession currently produce **two rows**.
   Assert that as the known-and-accepted state (6.1 residual), so the day
   someone adds the server-side window check this test tells them it worked.
8. **Orphan netting (D6)** — deposit with `job_id IS NULL`, job created by a
   non-estimate path, final invoiced with `job_id` only → assert netting, or
   assert-and-document the gap.

Frontend (vitest):

1. `MobileEstimatesView.spec.js` — Check submit goes through `postQueued` with
   `conflictIsError: true`, check # in `reference`, tenant-zone date; button
   disabled in flight.
1b. **Cash submit opens the confirm and posts nothing until it resolves true;
   rejecting it posts nothing at all.** The `confirmAsync`-returns-false path is
   the one that regresses silently — issue #215 was exactly this.
2. `MobileCustomerQuoteDialog.spec.js` — cash/check absent until the tech step.
3. `MobileBillingView.spec.js` — tenant-zone day, not the UTC slice (D4).
4. `EstimateView.spec.js` (P3, the reported case) — accept with "already paid"
   filled fires **two** calls in order: accept, then the payment against the
   returned `deposit.invoice_id`. A **backdated** date survives to the payload.
   The result dialog reads *paid* and shows **no** Copy Pay Link. And: when the
   payment POST rejects, the estimate is still accepted and the pay link
   reappears — the acceptance is never unwound.
5. `InvoiceDetailView.spec.js` / `BillingView.spec.js` — the Record Payment
   dialog opens prefilled with the invoice's `balance_due` (D8 regression).
   These two assertions guard the cheapest fix in the plan.

---

## 8. Rollout

P1–P3 are frontend-only. **P4 and the 6.1 fix are backend changes** — the first
draft's "no backend change is needed" was wrong.

1. `pytest` + `vitest` full local matrix, no shard skipped.
2. `/verifyplaywright` on a throwaway: accept an estimate with a deposit, record
   a check, then build the final and confirm the netting line. Both themes.
3. Android emulator or Doug's phone for P1/P2 — airplane mode is what proves
   `postQueued`, and **a technician login**, not an admin, is what proves D3.
4. Prod walk after deploy on Doug's own account. Mutates money — Doug's click.

---

## 9. Open questions for Doug

1. **Scope.** P1+P2 (field) is the real fix. P3 is polish. P4 needs a backend
   endpoint. Where do you want to stop?
2. **P4 shape:** tech-scoped mobile list (a), or grant techs `invoices.read_all`
   (b)? I recommend (a) — (b) hands the field the whole AR book.
3. ~~**6.1 double-tap.**~~ **ANSWERED (Doug, 2026-08-13):** cash gets a
   "is this the right amount?" confirm; check relies on the existing reference
   index. Residual weak-signal gap accepted and logged in 6.1.
4. **Over-payment at the door:** cap at the amount due, or record the whole
   check and mint a credit? Capping is my recommendation.
5. **§0.2 query 1.** If it returns rows, real customers may already have been
   over-billed. Do we go look?

---

## Appendix — adjacent finding, not in scope

The invoice **create** form has no payment field
([InvoiceCreateView.vue:771-778](gdx_dispatch/frontend/src/views/InvoiceCreateView.vue#L771-L778)) —
create, toast, redirect, then find Record Payment and re-key the amount. Every
other invoice surface already has cash/check. Recorded so it isn't lost; not
part of the estimate-acceptance work.
