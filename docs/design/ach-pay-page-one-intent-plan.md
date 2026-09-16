# ACH on the customer pay page: one intent, completed in the browser

**Date:** 2026-09-16 (investigated, planned, built, adversarially audited and browser-verified the same day)
**Status:** **PLAN** — built and verified on the branch, not merged. Flip to `MERGED #N` in the PR that lands it, and to `RELEASED vX.Y.Z` after the prod walk.
**Branch:** `fix/ach-pay-page-one-intent`

## What already exists (do not rebuild)

Everything around the bank rail was already built and is reused unchanged:

- `POST /api/payments/create-intent` mints a PaymentIntent bound to the invoice by `metadata.invoice_id`, sized from `balance_due`, through `_create_usable_intent` (idempotency replay liveness check) — `gdx_dispatch/core/payments.py`.
- `_idempotency_key(invoice, amount_cents, method)` already varies by method, so card and ACH attempts on the same balance never collide.
- M16 (#428): `_ach_in_flight` / `_refuse_if_ach_processing` refuse a second mint while a `processing` debit is moving, and the pay page renders "Bank transfer processing" instead of a form.
- M12: `cancel_open_intents_for_invoice` cancels stale open intents when money lands another way.
- The signed webhook (`routers/stripe_webhook.py` → `handle_payment_webhook`) notes `payment_intent.processing` on the audit trail for bank intents, records `payment_intent.succeeded` as `ach` or `card`, and reverses late ACH returns. (It used to read the rail off the intent's allowed `payment_method_types`; item 5 below replaces that with the PaymentMethod that paid.)
- The office bell, the ledger repost and the audit rows all hang off `_mark_invoice_paid`, which the webhook calls.

None of that needed to change. Only the mint and the page did.

## The defect

The "Bank Transfer (ACH)" tab on `/pay/{token}` could never complete a payment. It minted a **customer-less SetupIntent**, opened Stripe's picker with `collectBankAccountForSetup`, **never called `confirmUsBankAccountSetup`** (the mandate-acceptance step Stripe's guide requires), then posted the bank account id to `POST /api/payments/ach/charge`, which created a **second** PaymentIntent on that PaymentMethod with `confirm=True`, no Customer, no mandate.

Reproduced 2026-09-16 in test mode against the business's own Stripe account, exact parameters:

```
[C] page's charge call as written ........ REFUSED: invalid_request_error
    "The provided PaymentMethod cannot be attached. To reuse a
     PaymentMethod, you must attach it to a Customer first."
[E] same, after confirming the SetupIntent ... same refusal
[D] one PaymentIntent with mandate_data ...... status=processing
[F] Customer + confirmed SetupIntent ......... status=processing
```

Live evidence (prod, queried 2026-09-16): one ACH SetupIntent ever created (2026-08-19, `requires_payment_method`, no bank account attached), zero ACH PaymentIntents ever, zero `payments` rows with `method='ach'`. The one attempt was a real customer's deposit invoice; it was still unpaid a month later.

## Decision

Use Stripe's **accept-a-payment** flow for ACH Direct Debit, Elements + Payment Intents (<https://docs.stripe.com/payments/ach-direct-debit/accept-a-payment?payment-ui=direct-api>, read 2026-09-16):

1. `create-intent` takes `method: "card" | "ach"`; for `ach` it passes `payment_method_types=["us_bank_account"]` and keys idempotency on `ach`.
2. The page calls `stripe.collectBankAccountForPayment` (Stripe's hosted picker: Financial Connections, with manual entry + micro-deposits as the fallback), which attaches the account to **that** intent → `requires_confirmation`.
3. The page shows the linked account and Stripe's recommended one-time mandate text (business name from `AppSettings.company_name`); "Confirm and pay" calls `stripe.confirmUsBankAccountPayment`, which records the acceptance (IP, user agent, timestamp) and moves the intent to `processing`, or `requires_action` (`verify_with_microdeposits`).
4. Settlement is recorded by the webhook exactly as before. `/confirm` stays the best-effort fast path for the rare instant `succeeded`, and now labels the rail from the intent's `payment_method_types` instead of hard-coding `card` (audit finding 4).
5. **The rail label reads the PaymentMethod that paid, never the intent's allowed list alone** (audit round 2, finding 2). Prod card intents are automatic-payment-method intents carrying six allowed types; the day the Dashboard's ACH toggle goes on, `us_bank_account` joins that list on every card intent, and the old `"ach" if "us_bank_account" in payment_method_types` would have booked every card payment as a bank transfer in both the webhook and `/confirm`. `_intent_method` trusts the list only when it names one rail, otherwise reads the PaymentMethod (expanded on `/confirm`'s retrieve; one retrieve in the webhook).
6. **A refusing in-flight state is office-readable** (audit round 2, finding 3). Both mint sites (`create-intent` and the portal's pay) hand the gate their session, and it writes `ach_in_flight_blocked_new_payment` on the invoice's trail whenever it refuses (op, stage, intent, hosted link); the webhook notes `payment_intent.requires_action` as `ach_payment_awaiting_verification`. **Prod's webhook endpoint is not subscribed to `payment_intent.requires_action`** — Doug's Dashboard call; until then the gate's own row is the trace.
7. **The webhook never guesses a rail on a money event** (audit round 3, findings 2–3). `handle_payment_webhook` now sets the Stripe key itself — after a deploy, a cold worker's first request can be the `succeeded` for a debit started four days earlier, and nothing upstream had set it — and `_intent_method(strict=True)` raises when the allowed list is ambiguous and the PaymentMethod is unreadable, so the router 500s and Stripe retries instead of a payment landing on the wrong rail. Trail-only events still fall back with a warning.
8. **An abandoned intent carrying a bank account is never replayed** (audit round 3, finding 5). A customer who links an account and leaves at the mandate step leaves an intent in `requires_confirmation` with their PaymentMethod attached; idempotency replayed it — secret included — to anyone else holding the token, and `confirmUsBankAccountPayment(secret)` would have debited the first person's account. `_create_usable_intent` now cancels such an intent and mints fresh; "Use a different bank account" rides the same path.
9. **A failed ACH is on the trail too** (audit round 4, finding 3). `payment_intent.payment_failed` for a bank intent — an R01 bounce, a micro-deposit that timed out — writes `ach_payment_failed` with the code, message and reversal outcome. Card declines do not (the customer sees those on screen; they would flood the trail). Until this, a hand-typed account could live its whole life on prod with the trail ending on "processing".
10. **Micro-deposits count as in flight** (audit round 1, finding 3). Manual bank entry parks the intent in `requires_action` / `verify_with_microdeposits` for up to 10 days; M16 only knew `processing`, so the page would have shown a live form and minted a second intent. `_ach_in_flight` now returns `stage: "verifying"` for that state, the mint gates refuse with copy that tells the customer to confirm the deposit, and the page shows "Bank transfer awaiting verification" with Stripe's hosted verification link. A card intent's `requires_action` (3DS) is not matched. This is the one judgement call in the change: the alternative was a soft note that still allowed a second collection, and M16's decision was already that an in-flight debit blocks re-collection.

`ach/setup` and `ach/charge` are **deleted**, not patched: nothing could reach a working state through them, and a dead endpoint is the orphan pattern `CLAUDE.md` rules against.

## Rejected alternatives

- **Keep the SetupIntent design, add the missing confirm.** Proven insufficient ([E] above): reuse of a PaymentMethod across intents needs a Customer. The pay page is anonymous by design; minting a Stripe Customer per emailed token adds an object with no owner in this system.
- **Customer + confirmed SetupIntent, then charge ([F]).** Works, but it is the "save bank details for future payments" flow, carries the periodic-debit mandate language, and keeps a server-side charge step whose only job was to be refused. More surface, no benefit for a one-off invoice.
- **Replace both tabs with the Payment Element.** Cleanest long-term, but it rewrites the working card path too, and with `automatic_payment_methods` it depends on the Dashboard's ACH display preference, which is currently off. Out of scope for a defect fix in HARDENING.

## Money and rollback

No schema change, no migration, no existing rows touched. Existing `processing`/`succeeded` intents are unaffected (there are none for ACH). Rollback is a revert of the PR; the removed routes had no working caller.

## Verification

- Unit: `create-intent` names `us_bank_account` for `method="ach"`, keys on `ach`, passes no Customer and no `mandate_data`; card default unchanged; unknown method 422; old routes 404; the M16 gate fires for the ACH mint; the rendered page no longer carries the dead flow; a micro-deposit wait is in flight and a card 3DS wait is not; the page renders the awaiting-verification banner with the link; `/confirm` labels a bank intent `ach`.
- Adversarial audit, four rounds (2026-09-16, `critique_latest.md` <!-- lives in the session memory dir outside this repo; link-ok -->). Round 1: foundational assumption held against live test-mode probes; findings 3 (micro-deposits invisible to M16), 4 (`/confirm` hard-coded card) and 5 (prod-walk blind spot) addressed above; "Use a different bank account" replay and M16-on-the-new-mint killed on evidence. Round 2: assumption holds in live mode (prod webhook endpoint enabled, subscribed to `payment_intent.succeeded/processing/payment_failed` and `charge.*`, `pending_webhooks=0`); finding 2 (label from the allowed list) and 3 (no office trace for the verifying lock) addressed above; finding 5 (no real-phone walk) is open — the desktop walk was repeated at 390 px, but the Pixel AVD walk was not done and the prod walk should be on a phone. Round 3: findings 2–3 (webhook key, guessing the rail) and 5 (replayed secret carries a bank account) addressed above. Round 4: finding 3 (failed ACH silent on the trail) addressed as item 9; the portal's refusal row now names the portal user. **Open, Doug's call (round 4, finding 2 / blind spot):** the micro-deposit lock holds both rails for up to 10 days with no in-app release — the office's only lever is cancelling the intent in the Stripe Dashboard. The alternative is to cancel a verifying intent when the customer picks the other rail; that is a product-shape choice this PR does not make. Cost noted by the audit: prod card intents carry six allowed types, so every card `succeeded` webhook now does one PaymentMethod read.
- Browser (throwaway container on this branch, Stripe **test** keys, Playwright): Bank Transfer tab → Stripe picker (test institution) → linked account + mandate shown → Confirm and pay → `processing` banner; reload shows M16's "Bank transfer processing". Recorded in the PR.
- Prod walk after release: **on an invoice Doug owns, not a customer's, and on a phone.** Stopping at the mandate step leaves a live `requires_confirmation` intent carrying the walker's bank account on that invoice. Item 8 means the next `create-intent` cancels it rather than replaying it, so nobody else can confirm it — but until that next mint or the settle-time sweep it exists, attached to that invoice, in the Dashboard. Walk on a test invoice, or cancel the intent in the Dashboard immediately after.

## Sibling sweep

Class: an ACH surface that cannot complete a payment, or a dead ACH endpoint.
Searched: `gdx_dispatch/templates/*.html`, `core/payments.py`, `core/stripe_payments.py`, `routers/payments.py`, `routers/portal.py`, `frontend/src/**` (grep `us_bank_account|ach/|ACH`), `tests/`.
Instances: 3 found / 2 fixed (the pay page tab; `ach/setup`+`ach/charge`) / 1 deferred — the portal router's `POST /payments/ach/setup` (`routers/payments.py` → `stripe_payments.create_ach_verification`, legacy Token + `Customer.create_source`, no UI caller, no verify step). Deferred because the portal has no ACH UI at all and its card path is a separate design; recorded in `FOUND_NOT_FILED.md` 2026-09-16. <!-- untracked by design, see CLAUDE.md "One issue per session"; link-ok -->
