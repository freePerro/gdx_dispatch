"""Stripe Elements embedded payment collection + ACH bank transfer support.

These endpoints back the anonymous ``/pay/{token}`` page, so they have no user
to authenticate. Authorization is structural instead — see the "Public-payment
authorization" section below: the caller proves which invoice they may touch by
presenting its token, and the amount, the intent↔invoice binding and the ACH
method↔invoice binding are all decided server-side.

Endpoints (all take ``invoice_token``)
--------------------------------------
POST /api/payments/create-intent   — create PaymentIntent for an invoice
POST /api/payments/confirm         — confirm payment after Stripe.js completes
POST /api/payments/ach/setup       — create SetupIntent for ACH bank account
POST /api/payments/ach/charge      — charge the bank account collected for it

Saved-payment-method management lives on the AUTHENTICATED portal router
(``gdx_dispatch/routers/payments.py``, ``/payments/methods``). This module's
unauthenticated duplicates were removed 2026-08-04.

Public (no auth):
GET  /pay/{invoice_token}          — serve the Stripe Elements payment form

Webhook entry point (dispatched from routers/stripe_webhook.py):
    handle_payment_webhook(event, db)
"""
from __future__ import annotations

import contextlib
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone, time as dt_time, timedelta
from typing import Any
from uuid import UUID

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.customer_views import record_customer_view
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import Invoice, Payment

logger = logging.getLogger(__name__)

# NOTE: this module used to declare `oauth2_scheme = OAuth2PasswordBearer(
# auto_error=False)` and give every handler `token: str | None =
# Depends(oauth2_scheme)` — a parameter that was never read. With
# auto_error=False the dependency never rejects anything, so the signatures
# LOOKED authenticated in review while enforcing nothing. It survived multiple
# audits that way. Removed 2026-08-04; if you need auth here, use a dependency
# whose result you actually consume.

router = APIRouter(prefix="/api/payments", tags=["payments"])
public_router = APIRouter(tags=["payments-public"])


# M17 (money audit 2026-08-04). An UNAUTHENTICATED, tenant-wide
# `GET /api/payments` used to live here — no auth dependency, `Depends(get_db)`
# only, returning the whole AR book. It was never reachable because
# `ui_compat`'s AUTHENTICATED handler for the same path registers first
# (app.py:1670 vs :1756) and FastAPI is first-match-wins.
#
# That safety was accidental. `app.py` wraps the ui_compat import in a
# try/except that substitutes an EMPTY router on failure — and with that
# branch taken, this handler became the live one. Demonstrated 2026-08-23
# against a real container: simulating the import failure turned an
# unauthenticated `GET /api/payments` into **HTTP 200 with 200 payment rows**,
# invoice numbers and amounts, no credentials sent.
#
# `authz_sweep.py` knew about the duplicate and deliberately ignored it as
# "unreachable behind the shadow" — reasoning that was true only while the
# import it depended on kept succeeding.
#
# Deleted rather than gated: it had no callers, and the authenticated version
# in `ui_compat` is what `PaymentsView.vue` actually reaches. A route that
# exists only to be shadowed is a hole waiting for an import to fail.
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

# NOTE ON SHAPE (2026-08-04 payment-authorization hardening):
# ``invoice_token`` is the credential — it is the same unguessable token that
# addresses the /pay/{token} page, so possessing it is what authorizes the
# request. ``invoice_id`` and ``amount`` are DEPRECATED client inputs kept
# nullable for ONE release so a /pay tab opened before the deploy does not
# 422 in the middle of a payment. They are never trusted for authorization:
# the amount is always recomputed from the invoice, the PaymentIntent is
# bound to the invoice through Stripe metadata, and the ACH method is bound
# through its SetupIntent. Remove both fields (and _LEGACY_SHAPE_DEADLINE
# below) in the release after this one.
_LEGACY_SHAPE_DEADLINE = "2026-09-01"


class CreateIntentRequest(BaseModel):
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE
    amount: int | None = None  # DEPRECATED + IGNORED — server derives from balance
    # DEPRECATED + IGNORED (M4, money audit 2026-08-04). The server derived the
    # AMOUNT but passed the client's CURRENCY straight to Stripe, and the
    # webhook records `amount_received / 100` as dollars without checking it —
    # so `currency: "idr"` settled a $500 invoice for about $3. Kept on the
    # model (ignored) for one release so an open /pay tab does not 422.
    currency: str = "usd"


class ConfirmPaymentRequest(BaseModel):
    payment_intent_id: str
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE


class ACHSetupRequest(BaseModel):
    customer_email: str
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE


class ACHChargeRequest(BaseModel):
    payment_method_id: str
    # The SetupIntent that collected ``payment_method_id``. REQUIRED: it is
    # what proves the bank account was collected for THIS invoice. Without it
    # any caller could charge any saved payment method they knew the id of.
    setup_intent_id: str | None = None
    invoice_token: str | None = None
    invoice_id: str | None = None  # DEPRECATED — see NOTE ON SHAPE
    amount: int | None = None  # DEPRECATED + IGNORED — server derives from balance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The only currency this application prices, charges and records in. The
# recording paths treat Stripe minor units as cents-of-a-dollar, which is only
# true for USD (JPY has no minor unit at all), so this is an invariant of the
# money model, not a default. See M4 in the 2026-08-04 money audit.
CURRENCY = "usd"


def _init_stripe() -> None:
    """Set Stripe API key from environment."""
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")


def stripe_configured() -> bool:
    """Whether online payments can actually charge (a Stripe key is set)."""
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def public_pay_url(public_token: str | None) -> str | None:
    """Absolute customer-facing /pay/{token} URL, or None when the link
    would be dead: no token, no public base URL, or Stripe unconfigured
    (the pay page renders but Stripe.js can't charge without keys)."""
    base = os.getenv("GDX_PUBLIC_BASE_URL", "").rstrip("/")
    if not (public_token and base and stripe_configured()):
        return None
    return f"{base}/pay/{public_token}"


def _stripe_extra(tenant: dict) -> dict[str, Any]:
    """Return Stripe Connect kwargs if tenant has a connected account."""
    acct = tenant.get("stripe_connect_account_id")
    return {"stripe_account": acct} if acct else {}


# ---------------------------------------------------------------------------
# Public-payment authorization (2026-08-04)
# ---------------------------------------------------------------------------
# These endpoints serve the anonymous /pay/{token} page, so there is no user to
# authenticate. Authorization is therefore structural: the caller proves which
# invoice they may touch by presenting its token, and every other money-bearing
# decision (how much, which payment method) is made server-side. Before this,
# the client supplied invoice_id, amount AND payment_method_id, so a caller
# holding any one leaked identifier could credit a payment to an unrelated
# invoice or debit an unrelated bank account.


def _resolve_public_invoice(
    db: Session,
    *,
    invoice_token: str | None,
    invoice_id: str | None,
    op: str,
    require_balance: bool = True,
) -> Invoice:
    """Resolve (and gate) the invoice a public payment request may act on.

    ``invoice_token`` is authoritative. ``invoice_id`` is the deprecated legacy
    shape (see NOTE ON SHAPE) and is accepted only until
    ``_LEGACY_SHAPE_DEADLINE``; it is safe in the interim because callers can no
    longer choose the amount, the PaymentIntent binding, or the ACH method.
    """
    invoice: Invoice | None = None
    if invoice_token:
        invoice = (
            db.query(Invoice)
            .filter(Invoice.public_token == invoice_token, Invoice.deleted_at.is_(None))
            .first()
        )
        if invoice is None:
            # Same response as an unknown invoice — never reveal whether a
            # token merely mismatched an existing invoice.
            raise HTTPException(status_code=404, detail="Invoice not found")
    elif invoice_id:
        logger.warning(
            "public_payment_legacy_shape op=%s — client sent invoice_id without "
            "invoice_token; support ends %s",
            op,
            _LEGACY_SHAPE_DEADLINE,
        )
        try:
            invoice_uuid = UUID(invoice_id)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=422, detail="Invalid invoice_id format") from None
        invoice = db.get(Invoice, invoice_uuid)
        if invoice is None or invoice.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Invoice not found")
    else:
        raise HTTPException(status_code=422, detail="invoice_token is required")

    # An open /pay tab can outlive the invoice — voided, or a deposit settled
    # when the final invoice superseded it. Don't move money nothing is owed on.
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="This invoice has been cancelled.")
    # §11 rail (2026-08-08 audit): a DRAFT was payable here — including a
    # machine-priced closeout autodraft nobody reviewed, whose token is
    # minted at creation. An un-issued invoice must not take money; 404 (not
    # 409) so a leaked pre-issue token reveals nothing.
    if str(invoice.status or "").lower() == "draft":
        raise HTTPException(status_code=404, detail="Invoice not found")
    # `require_balance=False` for RECORDING operations (confirm). The signed
    # webhook usually beats the browser's confirm call, so by the time confirm
    # runs the balance is legitimately zero — rejecting it there would 409 the
    # customer's own success callback on every fast payment.
    if require_balance and float(invoice.balance_due or 0) <= 0:
        raise HTTPException(status_code=409, detail="This invoice has no balance due.")
    return invoice


def _amount_cents(invoice: Invoice) -> int:
    """The only amount this module will ever charge: what is actually owed.

    Never the client's number, and never ``invoice.total`` — a partially paid,
    credit-memo'd or deposit-applied invoice owes less than its total, and
    charging the total overcharges a real customer.
    """
    return int(round(float(invoice.balance_due or 0) * 100))


def _idempotency_key(invoice: Invoice, amount_cents: int, method: str) -> str:
    """Stripe idempotency key for a public payment attempt.

    MUST vary with amount AND method. Stripe rejects a reused key whose
    parameters differ (for 24h), so:
      * ``gdx-pi-{invoice_id}`` alone wedges the customer out of paying as soon
        as the balance changes between attempts (partial payment lands, or an
        earlier attempt sent the old client-supplied amount);
      * omitting the method wedges the customer who tries card, fails, then
        switches to ACH for the same balance — different params, same key.
    """
    return f"gdx-pi-{invoice.id}-{method}-{amount_cents}"


_UNUSABLE_INTENT_STATUSES = frozenset({"canceled", "succeeded"})


def _create_usable_intent(**kwargs) -> Any:
    """`PaymentIntent.create`, then prove the intent handed back is live.

    Stripe's idempotency layer replays **"the resulting status code and body of
    the first request"** — the body as it was at creation, not the object's
    current state (<https://docs.stripe.com/api/idempotent_requests>, read
    2026-08-24; keys are pruned once "at least 24 hours old"). Our keys are
    derived from (invoice, amount, method), so after the sweep cancels an
    intent, re-opening the pay page for the same amount replays the ORIGINAL
    body: status `requires_payment_method`, the old `client_secret`, and an
    intent that is actually **cancelled** at Stripe. The customer gets a pay
    page that cannot charge, for up to 24 hours.

    That sequence is not exotic. The office records a check (sweep cancels the
    open intent), then deletes or voids that payment — the balance returns to
    what it was, the customer reloads, same amount, same key.

    An earlier version of this checked the CREATE response for
    `status == "canceled"`. That is **provably unreachable**: a replay never
    carries the current status, and a freshly created intent is never cancelled.
    The check read as a guard and could not fire — and its test fabricated a
    response the documented idempotency layer cannot return.

    The only way to know is to ask. One `retrieve` per mint, which is a GET
    against an object we just named.
    """
    pi = stripe.PaymentIntent.create(**kwargs)
    pid = str(getattr(pi, "id", "") or "")
    if not pid:
        return pi

    # `retrieve` must look at the same account the object lives on.
    connect = {"stripe_account": kwargs["stripe_account"]} if kwargs.get("stripe_account") else {}
    try:
        live = stripe.PaymentIntent.retrieve(pid, **connect)
    except Exception:
        # A create that worked and a retrieve that did not: hand back what we
        # have rather than failing a checkout over a verification step.
        logger.warning("intent_liveness_unverified intent=%s — returning the create response", pid)
        return pi

    status = str(getattr(live, "status", "") or "")
    if status not in _UNUSABLE_INTENT_STATUSES:
        return live

    key = kwargs.pop("idempotency_key", "") or ""
    logger.warning(
        "intent_idempotency_replayed_dead_intent intent=%s status=%s key=%s — minting a "
        "fresh one so the customer does not get a pay page that cannot charge.",
        pid, status, key,
    )
    return stripe.PaymentIntent.create(**kwargs, idempotency_key=f"{key}-r{_uuid.uuid4().hex[:8]}")


_CANCELLABLE_INTENT_STATUSES = frozenset(
    {
        "requires_payment_method",
        "requires_confirmation",
        "requires_action",
        "requires_capture",
        # `processing` IS cancellable for the bank-debit family: "You can also
        # cancel a PaymentIntent in the `processing` state when the payment
        # method is ACH, ACSS, AU BECS, BACS, NZ BECS, or SEPA. However,
        # cancellation might fail due to a limited and varying cancellation time
        # window." (<https://docs.stripe.com/payments/paymentintents/lifecycle>,
        # read 2026-08-24.)
        #
        # An earlier version skipped it and logged that the money "cannot be
        # cancelled". That was wrong on the documentation: ACH is the one
        # asynchronous case where it often CAN be, and refusing to try
        # guaranteed the overcharge it was reporting. It is attempted now, and a
        # refusal is reported as a refusal rather than dressed up as policy.
        "processing",
    }
)
"""States Stripe will actually cancel from.

<https://docs.stripe.com/api/payment_intents/cancel>, read 2026-08-24. These
are exactly the "customer is sitting on the pay page" states — no money has
been captured in any of them, so cancelling costs the customer a confusing
failure on an invoice that is already settled, which beats charging them twice.

`processing` is deliberately absent. Stripe lists it as cancellable only "in
rare cases", and for ACH it means the money is already moving. Attempting it
would usually be refused, and recording that refusal as "handled" is precisely
the kind of comfortable lie this fix exists to remove. It gets shouted about
instead — see `stale_intent_in_flight` below.
"""

# Stripe's lifecycle documentation describes **no automatic expiry** for a
# PaymentIntent — one left in `requires_payment_method` stays there
# (<https://docs.stripe.com/payments/paymentintents/lifecycle>, read
# 2026-08-24). So this window is a deliberate cap on how far back we look, not a
# fact about when intents die: anything older is simply not scanned. 7 days was
# the first guess; 30 costs the same handful of API calls for one garage-door
# company's pay pages and covers a tab left open over a holiday. The page cap
# below stops a runaway, and it is logged rather than swallowed.
_INTENT_LOOKBACK_DAYS = 30
_INTENT_PAGE_SIZE = 100
_INTENT_MAX_PAGES = 5


def _open_intents_for_invoice(invoice, *, connect: dict) -> list:
    """Ask Stripe which intents are still open against ``invoice``.

    **Stripe is the register; we are not.** Every mint site already stamps
    ``metadata.invoice_id`` — the same binding ``/confirm`` checks before it
    will record a payment — so listing is complete *by construction*. A mint
    site added next year is covered without being wired to anything, which a
    local table could never promise: its completeness would depend on somebody
    remembering.

    `list`, not `search`. The Search API supports `metadata[...]` and would be
    a one-liner, but its data is only "searchable in under 1 minute"
    (<https://docs.stripe.com/search>, read 2026-08-24) and this entire bug
    lives inside that minute — the customer opened the page *just now*. The
    same page directs read-after-write flows to the list APIs, which carry no
    such delay.
    """
    since = int((datetime.now(timezone.utc) - timedelta(days=_INTENT_LOOKBACK_DAYS)).timestamp())
    want = str(invoice.id)
    found: list = []
    starting_after = None

    for _ in range(_INTENT_MAX_PAGES):
        params: dict[str, Any] = {"limit": _INTENT_PAGE_SIZE, "created": {"gte": since}}
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe.PaymentIntent.list(**params, **connect)
        rows = list(getattr(page, "data", None) or [])
        for pi in rows:
            meta = getattr(pi, "metadata", None) or {}
            if str(meta.get("invoice_id") or "") == want:
                found.append(pi)
        if not getattr(page, "has_more", False) or not rows:
            return found
        starting_after = getattr(rows[-1], "id", None)
        if not starting_after:
            return found

    logger.error(
        "stale_intent_scan_truncated invoice=%s pages=%d — more than %d intents in %d days; "
        "an older open intent may not have been checked.",
        want, _INTENT_MAX_PAGES, _INTENT_MAX_PAGES * _INTENT_PAGE_SIZE, _INTENT_LOOKBACK_DAYS,
    )
    return found


def _ach_in_flight(invoice, *, tenant: dict | None = None) -> dict | None:
    """The ACH debit already moving for ``invoice``, or None.

    M16. ACH is a delayed-notification method — "up to 4 business days to
    receive acknowledgement of success or failure"
    (<https://docs.stripe.com/payments/ach-direct-debit>, read 2026-08-24) —
    and nothing was recorded while a debit sat in `processing`: the balance
    stayed full and the pay page stayed live. Stripe's idempotency key expires
    after 24h, so a customer paying Friday and again Monday minted a second
    intent, and both settled.

    Stateless, like the M12 sweep it reuses: Stripe is the register, and a
    `processing` intent bound to this invoice by ``metadata.invoice_id`` IS
    the pending marker — no local row to write, drift, or forget to clear
    when the debit fails.

    Best-effort by design at render time, load-bearing at mint time: callers
    that only DISPLAY treat None as "nothing known", and the mint-site gates
    fail open on a Stripe outage (refusing to take money because we could not
    check would strand a legitimate payer; the M12 sweep and the
    ``payment_exceeds_receivable`` backstop still cover the overlap).
    """
    if invoice is None:
        return None
    try:
        _init_stripe()
        for pi in _open_intents_for_invoice(invoice, connect=_stripe_extra(tenant or {})):
            if str(getattr(pi, "status", "") or "") == "processing":
                return {
                    "intent_id": str(getattr(pi, "id", "") or ""),
                    "amount_cents": int(getattr(pi, "amount", 0) or 0),
                }
    except Exception:
        logger.exception("ach_in_flight_probe_failed invoice=%s", getattr(invoice, "id", "?"))
    return None


def _refuse_if_ach_processing(invoice, *, tenant: dict | None = None, op: str) -> None:
    """409 when an ACH debit for ``invoice`` is already moving (M16).

    The double-payment window in one sentence: the customer pays by bank on
    Friday, nothing records until the debit settles, and on Monday the page
    still shows the full balance — so they pay again, the 24h idempotency key
    has expired, and both debits settle.
    """
    pending = _ach_in_flight(invoice, tenant=tenant)
    if pending:
        logger.warning(
            "ach_processing_blocks_new_payment invoice=%s op=%s intent=%s amount_cents=%s",
            invoice.id, op, pending["intent_id"], pending["amount_cents"],
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "A bank transfer for this invoice is already processing. "
                "Bank transfers take up to 4 business days to clear — "
                "you don't need to pay again."
            ),
        )


def cancel_open_intents_for_invoice(
    invoice,
    *,
    why: str,
    remaining_cents: int,
    connected_account: str = "",
) -> list[dict]:
    """Close any pay page still open on ``invoice``. Never raises.

    M12. A PaymentIntent freezes its amount when it is minted, and `/confirm`
    only *records* what Stripe.js already charged in the browser — so once the
    customer's tab holds a $500 intent, nothing on the server gets a veto at
    charge time. The only place to stop a double collection is before they
    click: money has arrived some other way, so the open intent must go.

    Stateless on purpose. There is no local record of minted intents to drift,
    to miss a mint site, to grow forever, or to mark an intent "handled"
    because one DNS blip made a cancel fail. A failure here leaves nothing
    behind to be wrong later — the next settle event simply asks Stripe again.

    ``remaining_cents`` is what the invoice still owes AFTER the money that
    triggered this. **Intents that cannot overcharge are left alone**, judged
    on their CUMULATIVE total newest-first, not one at a time: killing a
    customer's live checkout that was never going to overcharge them is a
    worse bug than the one being fixed, but two intents that each fit under
    the balance can still exceed it together. Pass 0 when the invoice can never
    owe anything again (a void) — every open intent then exceeds it and goes.

    ``connected_account`` must be the account the intent LIVES on. A scan of
    the platform account for an intent minted on a connected account returns
    nothing, which is indistinguishable from "all clear" — so callers that
    know the account pass it, and Connect tenants are not silently unprotected.

    Runs in a Celery task, never in a request or webhook transaction: this
    makes two to six Stripe calls, stripe-python 11.6.0 retries twice by
    default, and holding invoice/payment/ledger locks across that is the
    silent-write window this repo ranks highest.
    """
    if invoice is None:
        return []
    try:
        _init_stripe()
        connect = {"stripe_account": connected_account} if connected_account else {}
        intents = _open_intents_for_invoice(invoice, connect=connect)
    except Exception:
        # Nothing is now inconsistent — there is no state to corrupt. The
        # `payment_exceeds_receivable` backstop still catches the overcharge
        # if this was the run that mattered.
        logger.exception("stale_intent_scan_failed invoice=%s why=%s", getattr(invoice, "id", "?"), why)
        return []

    results: list[dict] = []
    candidates: list = []
    for pi in intents:
        pid = str(getattr(pi, "id", "") or "")
        status = str(getattr(pi, "status", "") or "")
        if not pid:
            continue
        if status not in _CANCELLABLE_INTENT_STATUSES:
            # succeeded / canceled / already dealt with. Not our business.
            continue
        candidates.append(pi)

    # The decision is CUMULATIVE, not per-intent. Judging each intent on its
    # own leaves a hole a per-intent test cannot see: two open intents that
    # each fit inside what is owed but together exceed it. Every mint here is
    # sized to the full remaining balance (`_amount_cents`), so two open
    # intents only differ when the balance MOVED between them — which happens
    # whenever a payment is voided and the balance goes back up.
    #
    # Newest first: that is the tab the customer is actually looking at. Keep
    # intents while their running total still fits inside what is owed, and
    # cancel everything past that point. In the ordinary case the newest
    # intent equals the balance exactly, so it survives and every stale one
    # goes.
    # `created` is whole seconds, and `list.sort` is stable — it does not
    # reverse ties — so same-second intents would otherwise keep whatever order
    # Stripe happened to return. Tie-break on id so the decision is at least
    # deterministic and reproducible from a log.
    candidates.sort(
        key=lambda pi: (int(getattr(pi, "created", 0) or 0), str(getattr(pi, "id", "") or "")),
        reverse=True,
    )
    running = 0
    for pi in candidates:
        pid = str(getattr(pi, "id", "") or "")
        status = str(getattr(pi, "status", "") or "")
        frozen = int(getattr(pi, "amount", 0) or 0)
        if running + frozen <= remaining_cents:
            # Still inside what is owed even counting everything kept so far,
            # so this cannot overcharge. A customer part way through paying
            # their bill keeps their checkout.
            running += frozen
            logger.info(
                "open_intent_left_alone intent=%s invoice=%s amount_cents=%d kept_total=%d "
                "remaining_cents=%d why=%s",
                pid, invoice.id, frozen, running, remaining_cents, why,
            )
            continue
        try:
            # "duplicate", not "abandoned": the invoice was settled another
            # way, so a second collection would be a duplicate. The customer
            # did not walk away.
            stripe.PaymentIntent.cancel(pid, cancellation_reason="duplicate", **connect)
            logger.warning(
                "stale_intent_canceled intent=%s invoice=%s was=%s amount_cents=%s why=%s",
                pid, invoice.id, status, getattr(pi, "amount", "?"), why,
            )
            results.append({"intent_id": pid, "result": "canceled"})
        except Exception as exc:
            # Two very different failures wore one message before. Stripe
            # refusing because the intent LEFT a cancellable state (the browser
            # confirmed while we were reaching for it) is not "still open and
            # can still collect" — it means the money moved and
            # `payment_exceeds_receivable` is the net. Only a transport failure
            # leaves it genuinely open.
            text = str(exc)
            raced = "unexpected_state" in text or "cannot be canceled" in text
            if status == "processing" and not raced:
                # A bank debit already on its way, outside Stripe's "limited and
                # varying" cancellation window. It will settle, and since the
                # invoice no longer owes it, that settlement WILL overcharge.
                logger.error(
                    "stale_intent_in_flight_uncancellable intent=%s invoice=%s amount_cents=%s "
                    "why=%s — %s — the debit is already moving and will overcharge on "
                    "settlement; payment_exceeds_receivable is the backstop.",
                    pid, invoice.id, frozen, why, exc,
                )
                results.append({"intent_id": pid, "result": "in_flight"})
            elif raced:
                logger.warning(
                    "stale_intent_cancel_raced intent=%s invoice=%s was=%s why=%s — %s — "
                    "it left a cancellable state before we got there; if it succeeded the "
                    "payment_exceeds_receivable backstop covers it.",
                    pid, invoice.id, status, why, exc,
                )
                results.append({"intent_id": pid, "result": "raced"})
            else:
                logger.error(
                    "stale_intent_cancel_failed intent=%s invoice=%s was=%s why=%s — %s — "
                    "the intent is still open and can still collect.",
                    pid, invoice.id, status, why, exc,
                )
                results.append({"intent_id": pid, "result": "failed"})
    return results


def _audit_overcharge(db: Session, *, invoice, payment, source: str, detail: dict) -> None:
    """Record that a charge exceeded what the invoice owed. Never raises.

    Filed against the INVOICE so it lands on the trail an operator actually
    reads when reconstructing a bill, and attributed to the surface that
    recorded it rather than a blanket system identity — `/confirm` and the
    signed webhook are different actors and a money event may not pretend
    otherwise.
    """
    # SAVEPOINT, not a bare try. `log_audit_event_sync` ends in a flush; on
    # Postgres a failed flush poisons the whole session, so swallowing the
    # exception would leave the very next `db.commit()` unable to save the
    # PAYMENT. Losing the alert is survivable; losing the money record is not.
    #
    # ⚠ NOT PROVEN BY A TEST. `test_a_failing_overcharge_audit_does_not_lose_the
    # _payment` passes with this savepoint REVERTED, because SQLite does not
    # poison a session on a failed flush the way Postgres does — so on SQLite
    # that test is a green light that means nothing. A structural test that
    # spied on `begin_nested` was also vacuous (something else on this path
    # calls it). A real Postgres proof needs a full FK-valid object graph and
    # was not built. Treat this guard as correct-by-construction from
    # SQLAlchemy's SAVEPOINT semantics, not as verified.
    try:
        from gdx_dispatch.core.audit import log_audit_event_sync

        with db.begin_nested():
            log_audit_event_sync(
                db=db,
                tenant_id=None,
                user_id=source,
                action="payment_exceeds_receivable",
                entity_type="invoice",
                entity_id=str(invoice.id),
                details={**detail, "payment_id": str(getattr(payment, "id", "") or "")},
            )
    except Exception:
        logger.exception(
            "payment_exceeds_receivable_audit_failed invoice=%s — the overcharge is real and "
            "is NOT in the audit trail; the ERROR log above is the only record.",
            invoice.id,
        )


def _mark_invoice_paid(
    invoice: Invoice,
    db: Session,
    *,
    external_ref: str | None = None,
    method: str = "card",
    amount: float | None = None,
    source: str = "stripe-webhook",
    connected_account: str = "",
) -> None:
    """Record processor money as a REAL Payment row, recalc, post P3.

    GL S6 rewrite (bug #1, GL audit §12): the old version flipped the status
    straight to paid with NO Payment row and a mid-flow commit — money moved
    at the processor with nothing recorded locally. Now:

    - idempotent on ``external_ref`` (the PaymentIntent id): confirm +
      webhook both firing records exactly one payment;
    - the status flip happens inside ``_recalculate_invoice`` via the
      chokepoint (auto-flip), so P1 posts before P3 when the ledger is on;
    - one commit at the end — the payment, the recalc, and the ledger entry
      land or roll back together.
    """
    from sqlalchemy import select as _select

    # Late imports: the single recalc/posting truths live one layer up/over;
    # importing at call time avoids a routers←core import at module load.
    from gdx_dispatch.modules.ledger.rules import post_payment_received
    from gdx_dispatch.routers.invoices import _recalculate_invoice

    if external_ref:
        existing = db.scalars(
            _select(Payment).where(
                Payment.invoice_id == invoice.id,
                Payment.reference == external_ref,
                # M14 (money audit 2026-08-04): must exclude VOIDED rows. A
                # wrongly-reversed payment (late charge.failed on a retried
                # intent) left a voided row here, and this check then blocked
                # the redelivered `succeeded` from re-recording it — the
                # invoice stayed open with the money collected.
                Payment.voided_at.is_(None),
            )
        ).first()
        if existing is not None:
            return  # already recorded (idempotent across confirm + webhook)

    from sqlalchemy import func as _func

    already_paid = db.execute(
        _select(_func.sum(Payment.amount)).where(
            Payment.invoice_id == invoice.id, Payment.voided_at.is_(None)
        )
    ).scalar_one_or_none() or 0
    remaining = float(invoice.total or 0) - float(already_paid)
    # amount = what the processor says MOVED (PaymentIntent amount /
    # amount_received, audit round 2: recording "remaining" instead of the
    # actual charge misstated cash on partial intents). Zero/None → fall
    # back to remaining (legacy events without an amount).
    pay_amount = amount if amount else max(remaining, 0)
    if pay_amount <= 0:
        _recalculate_invoice(invoice, db)  # nothing new to record; true-up status
        db.commit()
        return

    # M12. A PaymentIntent freezes its amount when it is minted, and `confirm`
    # deliberately runs with `require_balance=False` — so an intent minted for
    # $500 can still be confirmed after the office records a $300 check, and
    # $800 lands on a $500 invoice.
    #
    # The money MOVED, so it is recorded: discarding it would be inventing a
    # different lie. What was missing was anyone being told. `amount_overpaid`
    # (M11) already renders a banner on the invoice screen, but nothing wrote
    # a trace an operator could search, alert on, or reconcile against.
    overpay = round(float(pay_amount) - max(remaining, 0.0), 2)
    if overpay > 0.009:
        logger.error(
            "payment_exceeds_receivable invoice=%s reference=%s charged=%.2f "
            "remaining=%.2f excess=%.2f — recorded in full (the money moved); "
            "the excess is a customer credit and needs a refund or an "
            "application decision.",
            invoice.id, external_ref, float(pay_amount), max(remaining, 0.0), overpay,
        )

    payment = Payment(
        company_id=invoice.company_id,
        invoice_id=invoice.id,
        amount=pay_amount,
        method=method,
        payment_date=datetime.now(timezone.utc).date(),
        reference=external_ref,
    )
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        # M2: the existence check above is optimistic — /confirm and the
        # webhook race by design ("the signed webhook usually beats the
        # browser's confirm call"), and both saw no row. Migration 056's
        # partial unique index on (invoice_id, reference) is what actually
        # decides it; losing the race means the payment IS recorded, so this
        # is the idempotent no-op it always claimed to be.
        db.rollback()
        logger.info(
            "payment_already_recorded_by_concurrent_writer invoice=%s ref=%s",
            invoice.id, external_ref,
        )
        return
    _recalculate_invoice(invoice, db)
    post_payment_received(db, payment, invoice)
    if overpay > 0.009:
        # M12. A searchable, alertable record — not just a log line and a
        # banner someone has to be looking at the right invoice to see.
        #
        # On the INVOICE, not the payment: this is a fact about the bill, and
        # an operator reconstructing "what happened to this invoice" reads its
        # trail, not each payment's. `source` names who actually did it — the
        # webhook and /confirm are different actors, and money events may not
        # be filed under one anonymous system identity.
        _audit_overcharge(
            db,
            invoice=invoice,
            payment=payment,
            source=source,
            detail={
                "charged": float(pay_amount),
                "remaining_before": round(max(remaining, 0.0), 2),
                "excess": overpay,
                "reference": external_ref or "",
                "why": (
                    "a PaymentIntent freezes its amount at mint time and confirm "
                    "runs with require_balance=False, so a stale tab can collect "
                    "more than the invoice still owes"
                ),
            },
        )
    # Captured BEFORE the commit: the sweep below is queued after it, and by
    # then `invoice` is expired — see the "only plain locals" note further
    # down. The sweep must never depend on re-reading the invoice.
    invoice_id = str(invoice.id)
    db.commit()

    # M12. AFTER the commit, and on a task — never inside the transaction.
    # Processor money just landed, so any OTHER intent still open on this
    # invoice could collect again. The one that produced this payment has
    # already succeeded, so it is not in a cancellable state and the sweep
    # skips it. Queued rather than called: this makes several Stripe calls and
    # stripe-python retries twice by default, and holding invoice, payment and
    # ledger locks across a Stripe outage is the silent-write window this repo
    # ranks highest — in the webhook it would also risk Stripe's own timeout
    # and a retry storm on top of the outage.
    from gdx_dispatch.tasks.stale_intent_sweep import enqueue_stale_intent_sweep

    enqueue_stale_intent_sweep(
        invoice_id,
        why=f"payment_recorded:{external_ref or ''}"[:64],
        connected_account=connected_account,
    )

    # Tell the office. Every caller of this function is a surface where the
    # CUSTOMER moved the money and no staff member was in the loop — the
    # emailed pay page, the portal, the ACH charge, and the webhook that
    # settles a bank debit days later. Before this, all four wrote a Payment
    # row, a ledger entry and an audit trail, and rang nothing: prod carried
    # five card payments and not one bell notification.
    #
    # Placed here on purpose. Every early return above is a path that recorded
    # NO new money — the idempotent redelivery, the lost race, the zero-amount
    # true-up — so a payment rings exactly once no matter whether /confirm or
    # the webhook won. And it is after the commit: `notify_office` opens its
    # own transaction and swallows its own failures, so the badge can never
    # roll back the money.
    #
    # Only plain locals cross this call. `invoice` is expired by the commit
    # above, so every attribute read on it is a lazy refresh SELECT — those
    # belong inside `notify_payment_received`'s guard, not on this line, where
    # a raise would escape into three callers that do not wrap this function
    # and 500 a request whose money is already committed.
    from gdx_dispatch.core.office_notifications import notify_payment_received

    notify_payment_received(
        db,
        invoice,
        amount=pay_amount,
        method=method,
        overpaid=max(overpay, 0.0),
    )


# ---------------------------------------------------------------------------
# POST /api/payments/create-intent
# ---------------------------------------------------------------------------

@router.post("/create-intent")
def create_intent(
    body: CreateIntentRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a Stripe PaymentIntent for the invoice the caller's token names.

    The amount is the invoice's balance due — the client's ``amount`` is
    ignored. ``metadata.invoice_id`` binds the intent to this invoice so
    ``confirm`` can refuse to credit it anywhere else.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="create-intent"
    )
    tenant: dict = getattr(request.state, "tenant", {}) or {}
    # M16: a card payment while an ACH debit is processing double-pays just as
    # surely as a second ACH — the balance has not moved yet.
    _refuse_if_ach_processing(invoice, tenant=tenant, op="create-intent")
    amount_cents = _amount_cents(invoice)

    try:
        pi = _create_usable_intent(
            amount=amount_cents,
            currency=CURRENCY,  # M4: never body.currency
            metadata={
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            idempotency_key=_idempotency_key(invoice, amount_cents, "card"),
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe create_intent error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {
        "client_secret": pi.client_secret,
        "payment_intent_id": pi.id,
        # What will actually be charged. The pay page renders the balance
        # server-side and does not read this back — it is here so any API
        # consumer (and anyone debugging a disputed charge) can see the
        # server's figure rather than inferring it.
        "amount": amount_cents,
    }


# ---------------------------------------------------------------------------
# POST /api/payments/confirm
# ---------------------------------------------------------------------------

@router.post("/confirm")
def confirm_payment(
    body: ConfirmPaymentRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Confirm payment after Stripe.js reports success.

    The PaymentIntent must carry ``metadata.invoice_id`` matching the invoice
    the caller's token resolves to. Without that check a succeeded intent could
    be replayed against any invoice — idempotency is keyed on
    ``(invoice_id, reference)``, so one real payment could settle a different
    invoice as well as its own.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db,
        invoice_token=body.invoice_token,
        invoice_id=body.invoice_id,
        op="confirm",
        require_balance=False,
    )

    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = stripe.PaymentIntent.retrieve(
            body.payment_intent_id, **_stripe_extra(tenant)
        )
    except stripe.StripeError as exc:
        logger.error("Stripe retrieve error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    intent_invoice_id = str((getattr(pi, "metadata", None) or {}).get("invoice_id") or "")
    if intent_invoice_id != str(invoice.id):
        logger.warning(
            "payment_confirm_invoice_mismatch pi=%s intent_invoice=%s requested_invoice=%s",
            body.payment_intent_id,
            intent_invoice_id or "<none>",
            invoice.id,
        )
        raise HTTPException(
            status_code=409,
            detail="This payment does not belong to this invoice.",
        )

    if pi.status == "succeeded":
        _mark_invoice_paid(
            invoice, db, external_ref=pi.id, method="card",
            # M17.3: `amount_received`, not `amount`. Identical under
            # auto-capture (every intent here — no mint site sets
            # capture_method="manual"), divergent the moment manual capture
            # appears: `amount` is what was ASKED, `amount_received` is what
            # MOVED, and the webhook already records the latter. Recording
            # different figures for the same charge depending on which
            # message arrives first is a books divergence waiting for a
            # capture flow. Fallback to `amount` keeps legacy/test intents
            # without the field recording exactly as before.
            amount=(getattr(pi, "amount_received", None) or pi.amount or 0) / 100.0,
            source="stripe-confirm",
            connected_account=str(_stripe_extra(tenant).get("stripe_account", "") or ""),
        )

    return {"status": pi.status, "invoice_id": str(invoice.id)}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/setup
# ---------------------------------------------------------------------------

@router.post("/ach/setup")
def ach_setup(
    body: ACHSetupRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Create a SetupIntent so Stripe.js can collect ACH bank account details.

    Scoped to an invoice: previously this took only an email, so anyone could
    mint unlimited SetupIntents. The ``metadata.invoice_id`` stamped here is
    what ``ach/charge`` later checks to prove the collected bank account was
    gathered for THIS invoice.
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="ach-setup"
    )
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        si = stripe.SetupIntent.create(
            payment_method_types=["us_bank_account"],
            metadata={
                "email": body.customer_email,
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_setup error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    return {"client_secret": si.client_secret, "setup_intent_id": si.id}


# ---------------------------------------------------------------------------
# POST /api/payments/ach/charge
# ---------------------------------------------------------------------------

@router.post("/ach/charge")
def ach_charge(
    body: ACHChargeRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Charge the bank account collected for this invoice.

    Creates a PaymentIntent with ``confirm=True`` so the charge is initiated
    immediately. ACH payments are typically pending for 1-2 business days.

    ``setup_intent_id`` is required and must be a SetupIntent minted by
    ``ach/setup`` for THIS invoice that collected THIS payment method. That
    chain is the authorization: without it, knowing any ``pm_`` id was enough
    to debit an unrelated person's bank account (an unauthorized ACH debit,
    which is a NACHA violation regardless of where the money lands).
    """
    _init_stripe()
    invoice = _resolve_public_invoice(
        db, invoice_token=body.invoice_token, invoice_id=body.invoice_id, op="ach-charge"
    )
    # M16: THE double-payment window. Friday's debit is still processing on
    # Monday, the 24h idempotency key has expired, and a second charge here
    # would mint a fresh intent — both settle.
    _refuse_if_ach_processing(
        invoice, tenant=getattr(request.state, "tenant", {}) or {}, op="ach-charge"
    )

    if not body.setup_intent_id:
        # Fail closed. An ACH tab opened before this deploy has no
        # setup_intent_id; retrying from a fresh page costs the customer one
        # reload, whereas charging an unverifiable bank account is a debit we
        # cannot justify.
        raise HTTPException(
            status_code=409,
            detail="This payment session is out of date. Please refresh the page and try again.",
        )

    try:
        si = stripe.SetupIntent.retrieve(body.setup_intent_id)
    except stripe.StripeError as exc:
        logger.error("Stripe ach_charge setup-intent retrieve error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    si_invoice_id = str((getattr(si, "metadata", None) or {}).get("invoice_id") or "")
    si_payment_method = str(getattr(si, "payment_method", "") or "")
    if si_invoice_id != str(invoice.id) or si_payment_method != body.payment_method_id:
        logger.warning(
            "ach_charge_binding_mismatch si=%s si_invoice=%s si_pm=%s "
            "requested_invoice=%s requested_pm=%s",
            body.setup_intent_id,
            si_invoice_id or "<none>",
            si_payment_method or "<none>",
            invoice.id,
            body.payment_method_id,
        )
        raise HTTPException(
            status_code=409,
            detail="This bank account was not set up for this invoice.",
        )

    amount_cents = _amount_cents(invoice)
    tenant: dict = getattr(request.state, "tenant", {}) or {}

    try:
        pi = _create_usable_intent(
            amount=amount_cents,
            currency="usd",
            payment_method=body.payment_method_id,
            payment_method_types=["us_bank_account"],
            confirm=True,
            metadata={
                "invoice_id": str(invoice.id),
                "tenant_id": str(tenant.get("id", "")),
            },
            idempotency_key=_idempotency_key(invoice, amount_cents, "ach"),
            **_stripe_extra(tenant),
        )
    except stripe.StripeError as exc:
        logger.error("Stripe ach_charge error: %s", exc)
        raise HTTPException(status_code=402, detail=str(exc)) from None

    # ACH payments may be processing (not yet succeeded); mark partial if needed
    if pi.status == "succeeded":
        _mark_invoice_paid(
            invoice, db, external_ref=pi.id, method="ach",
            # M17.3: `amount_received`, not `amount`. Identical under
            # auto-capture (every intent here — no mint site sets
            # capture_method="manual"), divergent the moment manual capture
            # appears: `amount` is what was ASKED, `amount_received` is what
            # MOVED, and the webhook already records the latter. Recording
            # different figures for the same charge depending on which
            # message arrives first is a books divergence waiting for a
            # capture flow. Fallback to `amount` keeps legacy/test intents
            # without the field recording exactly as before.
            amount=(getattr(pi, "amount_received", None) or pi.amount or 0) / 100.0,
            source="stripe-ach-charge",
            connected_account=str(_stripe_extra(tenant).get("stripe_account", "") or ""),
        )

    return {"status": pi.status, "payment_intent_id": pi.id}


# NOTE: ``GET /api/payments/methods`` and ``DELETE /api/payments/methods/{pm_id}``
# were removed 2026-08-04. They were unauthenticated (leaking card brand/last4
# and bank details for any Stripe customer id, and detaching any payment
# method) and had no production caller — the payment-methods UI has always used
# the portal router's authenticated ``/payments/methods``.


# ---------------------------------------------------------------------------
# GET /pay/{invoice_token}  — public, no auth
# ---------------------------------------------------------------------------

@public_router.get("/pay/{invoice_token}", response_class=HTMLResponse)
def pay_invoice(
    invoice_token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Serve the Stripe Elements payment form for a public invoice link.

    The invoice is looked up by its ``public_token`` (a unique random string
    sent to customers in payment-request emails). No authentication is
    required — the token itself acts as the secret.
    """
    invoice = (
        db.query(Invoice)
        .filter(Invoice.public_token == invoice_token, Invoice.deleted_at.is_(None))
        .first()
    )
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found or expired")
    # §11 rail (2026-08-08 audit): the form rendered for DRAFTS — an
    # unreviewed autodraft presented a full Stripe payment page, and the
    # render also logged invoice_viewed_by_customer as if it were a
    # delivered invoice. Un-issued = not found (never reveal pre-issue
    # invoices to a leaked token).
    if str(invoice.status or "").lower() == "draft":
        raise HTTPException(status_code=404, detail="Invoice not found or expired")

    # The customer clicked the link we emailed them. Never blocks the page.
    record_customer_view(
        db,
        action="invoice_viewed_by_customer",
        entity_type="invoice",
        entity_id=invoice.id,
        tenant_id=getattr(invoice, "company_id", None),
        request=request,
        sent_at=getattr(invoice, "sent_at", None),
        details={"invoice_number": getattr(invoice, "invoice_number", None)},
    )

    # M16: while an ACH debit is processing, the page must say so instead of
    # presenting a live payment form. Best-effort — a Stripe outage renders
    # the normal form, and the mint-site gates remain the hard stop.
    ach_processing = None
    if str(invoice.status or "").lower() != "paid" and float(invoice.balance_due or 0) > 0:
        ach_processing = _ach_in_flight(
            invoice, tenant=getattr(request.state, "tenant", {}) or {}
        )

    return templates.TemplateResponse(
        request,
        "payment_form.html",
        {
            "invoice": invoice,
            "ach_processing": ach_processing,
            "stripe_publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
            # The photos the office attached to THIS invoice (Doug 2026-08-12:
            # photos are customer-facing). They already ride the PDF; showing
            # them on the page the customer actually opens is the same
            # disclosure, one click earlier — "here is the work you're paying
            # for". Strictly the attached set, never the job's whole roll.
            "job_photos": _invoice_public_photos(invoice, db),
        },
    )


# How many attached photos the pay page will inline. The picker allows up to
# 20 per invoice; twenty full photos inlined would be megabytes on a phone,
# and nobody scrolls past the first few above a card form. The PDF still
# carries the complete set.
_PAY_PAGE_MAX_PHOTOS = 6


def _invoice_public_photos(invoice: Any, db: Session) -> list[dict[str, Any]]:
    """The attached photos for the public pay page, as {src, label} rows.

    Same selection the PDF prints (invoice.attached_photo_ids, in pick order)
    resolved through the same helper, so the page and the attachment can never
    show the customer different pictures.

    Inlined as data: URIs rather than served from a new public route. The pay
    page is anonymous by design, and the ungated-route baseline is a ratchet to
    work DOWN — adding an endpoint to show a picture is the wrong trade when
    the bytes can ride inside the page the token already unlocked.
    """
    import json as _json_mod

    from gdx_dispatch.core.job_photos import photo_data_uri, resolve_photo_file
    from gdx_dispatch.models.tenant_models import JobPhoto

    raw = getattr(invoice, "attached_photo_ids", None)
    if not raw or getattr(invoice, "job_id", None) is None:
        return []
    try:
        ids = [str(i) for i in _json_mod.loads(raw) if i]
    except (ValueError, TypeError):
        return []
    if not ids:
        return []
    uuids = []
    for i in ids:
        with contextlib.suppress(ValueError, AttributeError):
            uuids.append(_uuid.UUID(i))
    if not uuids:
        return []
    rows = db.execute(
        select(JobPhoto).where(
            JobPhoto.id.in_(uuids),
            JobPhoto.job_id == invoice.job_id,
            # The same share gate the portal uses (migration 063). Attaching a
            # photo to an invoice SETS this flag, so the normal path needs no
            # second decision — but un-sharing a photo afterwards has to pull
            # it off the customer's page, or "internal" would mean internal
            # everywhere except the bill.
            JobPhoto.customer_visible.is_(True),
            JobPhoto.deleted_at.is_(None),
        )
    ).scalars().all()
    by_id = {str(p.id): p for p in rows}
    out: list[dict[str, Any]] = []
    for pid in ids:  # pick order is display order
        if len(out) >= _PAY_PAGE_MAX_PHOTOS:
            break
        photo = by_id.get(pid)
        if photo is None:
            continue
        resolved = resolve_photo_file(db, photo)
        if resolved is None:
            continue
        src = photo_data_uri(*resolved)
        if src is None:
            continue
        out.append({
            "src": src,
            "label": (photo.caption or "").strip() or (photo.kind or "").strip().title(),
        })
    return out




# ---------------------------------------------------------------------------
# Webhook helper (called from gdx_dispatch/routers/stripe_webhook.py)
# ---------------------------------------------------------------------------

def _apply_charge_refund(db: Session, data: dict) -> dict:
    """Split `charge.refunded` into the two things it actually means.

    M3. Stripe fires this event for PARTIAL refunds too, and the old code
    treated both as one: straight to the full void. Refunding $50 of a $500
    payment as goodwill voided the entire $500 — the balance came back, the
    invoice flipped paid→sent, and dunning chased a customer who had paid in
    full.

    * **Full** (``amount_refunded >= amount``) — the money genuinely left.
      Void the payment; the balance re-opens and the invoice un-pays. Unchanged.
    * **Partial** — the customer still paid. **Do not touch the payment.**

    Why a partial refund is not recorded here automatically
    ------------------------------------------------------
    It is tempting, and an adversarial review showed two ways it books money
    twice, both of which need a schema change to close properly:

    1. ``amount_refunded`` is cumulative, so a partial refund followed by a
       full one arrives as ``amount_refunded == amount`` and takes the void
       branch — which knows nothing about the partial row already written. Net
       paid goes NEGATIVE: $550 reversed on a $500 charge.
    2. If the office also records the refund by hand (the normal way to record
       one), nothing links the two rows, so $50 returned is booked as $100.

    Both need refunds keyed on Stripe's refund id in a real column, not
    inferred from free text. Until that exists, this records the FACT loudly —
    audit event plus log — and leaves the money entry to the office refund
    endpoint, which caps by net paid and posts to the ledger. Incomplete and
    visible beats wrong and silent on a money surface, and it is strictly
    better than the void it replaces.
    """
    from gdx_dispatch.core.audit import log_audit_event_sync

    reference = str(data.get("payment_intent") or "")
    if not reference:
        return {"status": "no_reference"}

    charge_total = _cents_to_dollars(data.get("amount"))
    refunded_total = _cents_to_dollars(data.get("amount_refunded"))

    # Absent is not zero. An older payload shape, or one that omits the
    # amounts, must not be read as a partial refund of nothing.
    if refunded_total is None or charge_total is None or refunded_total >= charge_total:
        return _reverse_recorded_payment(db, reference, "charge.refunded")

    payment = db.scalars(
        select(Payment).where(Payment.reference == reference, Payment.voided_at.is_(None))
    ).first()
    if payment is None:
        return {"status": "no_payment_to_reverse", "reference": reference}

    logger.warning(
        "stripe_partial_refund_not_recorded reference=%s refunded=%.2f of charge=%.2f "
        "invoice=%s — the payment was left intact (a partial refund must not void it); "
        "record the refund via POST /api/invoices/{id}/refund so it reaches the books",
        reference, refunded_total, charge_total, payment.invoice_id,
    )
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id="stripe:webhook",
        action="stripe_partial_refund_received",
        entity_type="invoice",
        entity_id=str(payment.invoice_id),
        details={
            "reference": reference,
            "charge_total": charge_total,
            "refunded_total": refunded_total,
            "recorded": False,
            "why": "partial refunds are recorded by the office refund endpoint; "
                   "see M3 in money-audit-2026-08-04",
        },
    )
    db.commit()
    return {
        "status": "partial_refund_not_recorded",
        "reference": reference,
        "charge_total": charge_total,
        "refunded_total": refunded_total,
    }


def _cents_to_dollars(value) -> float | None:
    """Stripe amounts are integer minor units. ``None`` means "not supplied",
    which is different from zero and must not be read as a full refund."""
    if value is None:
        return None
    try:
        return round(int(value) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _failure_event_is_superseded(
    intent_id: str, failed_charge_id: str, connected_account: str = ""
) -> bool | None:
    """Is this failure event about an attempt the intent has already moved on from?

    Returns True/False, or **None when we could not find out** — which the
    caller must not read as False.

    M14. Stripe does not guarantee delivery order, and a card retry reuses the
    same PaymentIntent. So "attempt one declines, customer retries, attempt two
    succeeds" can deliver as `succeeded` (recorded, $500) then the delayed
    `charge.failed` from attempt one — which reversed the good payment,
    re-opened the invoice, and put a customer who had paid back into dunning.

    **The discriminator is the CHARGE, not the intent's status.** Each attempt
    on an intent is its own charge, and a superseded attempt names a charge the
    intent no longer points at. Comparing `latest_charge` answers the actual
    question — "is this event about the current attempt?" — without depending
    on what a PaymentIntent's `status` does in any particular flow.

    **Scope, stated honestly.** This guard covers CARD RETRIES, which is where
    the ordering hazard actually lives. A `us_bank_account` failure after the
    intent reaches `succeeded` does NOT arrive here at all — Stripe raises a
    **dispute** for that case and the intent stays `succeeded`; these two
    events only fire while the intent is still `processing`. An earlier draft
    of this docstring justified the design by an ACH-return-versus-stale-decline
    dilemma that does not occur on this path; an adversarial review checked the
    Stripe documentation and killed it.

    Keying on the charge rather than the status is still the better rule — it
    answers the question actually being asked, and it does not depend on being
    right about intent status transitions in any flow — but it is a
    robustness choice, not a fix for a live ACH hole.

    Without a charge id to compare (nothing in the event names one), this
    returns None rather than guessing — an unknown, not a False.
    """
    if not intent_id or not failed_charge_id:
        return None
    try:
        stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
        # PaymentIntents are CREATED with `**_stripe_extra(tenant)`, which
        # carries `stripe_account` on a Connect tenant. A retrieve without it
        # looks in the platform account, 404s, and this guard degrades to
        # "unknown" on every event — inert, plus an ERROR line each time. The
        # webhook envelope carries the account, so pass it through.
        pi = stripe.PaymentIntent.retrieve(
            intent_id, **({"stripe_account": connected_account} if connected_account else {})
        )
        latest = getattr(pi, "latest_charge", None)
        # `latest_charge` is an id string unless the caller expanded it.
        latest_id = str(getattr(latest, "id", latest) or "")
        if not latest_id:
            return None
        return latest_id != str(failed_charge_id)
    except Exception:
        logger.exception("payment_intent_charge_check_failed intent=%s", intent_id)
        return None


def _reverse_unless_superseded(
    db: Session,
    reference: str,
    reason: str,
    failed_charge_id: str = "",
    connected_account: str = "",
) -> dict:
    """Reverse a recorded payment, UNLESS this event is a superseded attempt.

    M14's fix, and the single place both failure events go through so they
    cannot drift apart.

    A genuine late ACH return (which MUST reverse) and a stale pre-success card
    decline (which must NOT) arrive as the same event shape. What separates
    them is whether the event names the intent's CURRENT charge.

    When that cannot be established — no charge id in the event, or Stripe
    unreadable — this falls back to reversing, which is today's behaviour. The
    alternative would invent a new failure mode in which genuinely returned
    money silently stays recorded, and a missed reversal is far worse than a
    reversal that has to be undone: the invoice would read paid, dunning would
    stop chasing, and the cash would be gone with nothing on the record.
    """
    superseded = _failure_event_is_superseded(
        reference, failed_charge_id, connected_account
    )
    if superseded is True:
        logger.warning(
            "stale_failure_event_ignored reference=%s charge=%s reason=%s — the "
            "intent has moved on to a later charge, so this event is a "
            "superseded attempt. NOT reversing.",
            reference, failed_charge_id, reason,
        )
        return {
            "status": "ignored_stale_failure",
            "reference": reference,
            "reason": reason,
        }
    if superseded is None and reference:
        logger.error(
            "failure_event_unverified reference=%s charge=%s reason=%s — could not "
            "establish whether this attempt is current; reversing on the event "
            "alone, which is the pre-M14 behaviour.",
            reference, failed_charge_id, reason,
        )
    return _reverse_recorded_payment(db, reference, reason)


def _reverse_recorded_payment(db: Session, reference: str, reason: str) -> dict:
    """Void the Payment row recorded for ``reference`` and re-open the invoice.

    Money that arrived can leave again: an ACH debit can be returned days later
    (R01 insufficient funds, R10 unauthorized), a card charge can be refunded,
    a customer can dispute. Recording only the arrival is how books drift —
    the invoice reads paid, dunning stops chasing, and the cash is gone.

    Voiding (rather than deleting) keeps the history; ``_recalculate_invoice``
    excludes voided payments, so the balance comes back and the status flips
    off "paid" on its own.
    """
    from sqlalchemy import select as _select

    from gdx_dispatch.routers.invoices import _recalculate_invoice

    if not reference:
        return {"status": "no_reference"}

    payment = db.scalars(
        _select(Payment).where(Payment.reference == reference, Payment.voided_at.is_(None))
    ).first()
    if payment is None:
        # Nothing recorded for this charge (e.g. the reversal arrived before we
        # ever saw the success). Not an error.
        return {"status": "no_payment_to_reverse", "reference": reference}

    payment.voided_at = datetime.now(timezone.utc)
    # M15 / migration 076. `reason` was taken and thrown away; without it a
    # later reinstatement cannot tell a dispute's void from a refund's or the
    # office's, and un-voiding the wrong one invents money.
    payment.voided_reason = (reason or "")[:64] or None
    # Flush explicitly: _recalculate_invoice SUMs non-voided payments with a
    # SELECT, and on an autoflush=False session the pending void would not be
    # visible to it — the balance would come back unchanged and the invoice
    # would stay "paid" with the money gone.
    db.flush()
    invoice = db.get(Invoice, payment.invoice_id)
    if invoice is not None:
        _recalculate_invoice(invoice, db)
        # _recalculate_invoice only ever flips an invoice TO "paid"; there is
        # no reverse edge, so an invoice whose payment just got voided keeps
        # reading "paid" with a positive balance. Un-pay it explicitly, back
        # to "sent" (it was, in fact, sent to the customer) so it re-enters
        # aging and dunning.
        if invoice.status == "paid" and float(invoice.balance_due or 0) > 0:
            from gdx_dispatch.modules.ledger.service import transition_invoice_status

            transition_invoice_status(db, invoice, "sent", actor="stripe-webhook")
            invoice.paid_at = None
    _audit_payment_reversal(
        db, payment, action="payment_reversed", reason=reason,
        detail={"invoice_reopened": True},
    )
    db.commit()
    logger.warning(
        "payment_reversed reference=%s invoice=%s reason=%s — invoice re-opened",
        reference, payment.invoice_id, reason,
    )
    return {"status": "reversed", "invoice_id": str(payment.invoice_id), "reason": reason}


def _audit_payment_reversal(db: Session, payment, *, action: str, reason: str, detail: dict) -> None:
    """Record a webhook-driven money movement.

    Invariant #1: every state-changing action answers who did it, what
    changed, when. Both of these move money on an invoice off a signed Stripe
    event, and both used to leave nothing but a `logger.warning` — which is
    not a record, it is a hope that somebody greps.

    The actor is the webhook, named as such: this is machine-initiated, and
    saying so is more honest than attributing it to whoever last logged in.
    Never raises — a failed trail must not 500 a webhook Stripe will retry,
    turning one lost audit row into a redelivery loop.
    """
    try:
        from gdx_dispatch.core.audit import log_audit_event_sync

        log_audit_event_sync(
            db=db,
            tenant_id=None,
            user_id="stripe-webhook",
            action=action,
            entity_type="payment",
            entity_id=str(payment.id),
            details={
                "invoice_id": str(payment.invoice_id),
                "amount": float(payment.amount or 0),
                "reference": payment.reference,
                "stripe_event": reason,
                **detail,
            },
        )
    except Exception:
        logger.exception("%s_audit_failed reference=%s", action, payment.reference)


# The only void reasons a dispute reinstatement may undo. Anything else — a
# refund, an office void, or a NULL from before migration 076 — is somebody
# else's reversal, and putting it back would invent money.
_DISPUTE_VOID_REASONS = frozenset(
    {"charge.dispute.created", "charge.dispute.funds_withdrawn"}
)


def _reinstate_reversed_payment(db: Session, reference: str, reason: str) -> dict:
    """Un-void the Payment row for ``reference`` and settle the invoice again.

    M15. `charge.dispute.created` voided the payment (correct at the time) and
    there was no handler for the other end of the lifecycle. Winning a dispute
    reinstates the money at Stripe and nothing restored it here: the payment
    stayed voided, the invoice stayed open, dunning chased a customer whose
    charge had stood, and the cash sat in the bank with no `Payment` row.

    The mirror of `_reverse_recorded_payment`. Un-voiding rather than inserting
    a second row keeps one payment per charge — a new row would double-count
    against `core/invoice_paid.py`, which sums non-voided payments.
    """
    from sqlalchemy import select as _select

    from gdx_dispatch.routers.invoices import _recalculate_invoice

    if not reference:
        return {"status": "no_reference"}

    voided = db.scalars(
        _select(Payment)
        .where(Payment.reference == reference, Payment.voided_at.is_not(None))
        .order_by(Payment.voided_at.desc())
    ).all()
    if not voided:
        # Nothing was reversed for this charge — e.g. the dispute was an
        # inquiry we correctly declined to reverse, and it closed in our
        # favour. There is nothing to put back. Not an error.
        return {"status": "no_payment_to_reinstate", "reference": reference}

    # THREE things void a payment: this dispute path, a full Stripe refund
    # (`_apply_charge_refund`, which writes NO InvoiceAdjustment — it is a
    # bare `return _reverse_recorded_payment(...)`), and the office's own
    # void-payment action. Un-voiding the wrong one INVENTS money: the invoice
    # returns to paid on cash that was refunded or deliberately reversed.
    #
    # An earlier attempt guarded on "does this invoice carry a refund
    # adjustment", which CANNOT FIRE on the Stripe path — it was checking for
    # a row that path never writes. Migration 076 records the reason instead,
    # so the guard is on the fact rather than on a proxy for it.
    #
    # Refusing when it cannot tell is the safe direction: recoverable off a
    # loud log, where inventing money is not.
    if len(voided) > 1:
        logger.error(
            "dispute_reinstate_ambiguous reference=%s voided_rows=%d reason=%s — "
            "NOT reinstating; a human has to say which row the dispute voided.",
            reference, len(voided), reason,
        )
        return {
            "status": "reinstate_needs_review",
            "reference": reference,
            "why": "more than one voided payment for this charge",
        }

    payment = voided[0]
    voided_reason = str(getattr(payment, "voided_reason", None) or "")
    if voided_reason not in _DISPUTE_VOID_REASONS:
        logger.error(
            "dispute_reinstate_not_ours reference=%s invoice=%s voided_reason=%r "
            "reason=%s — NOT reinstating; this payment was not voided by a "
            "dispute (a refund, an office void, or a row from before the reason "
            "was recorded). Un-voiding it would invent money.",
            reference, payment.invoice_id, voided_reason or None, reason,
        )
        return {
            "status": "reinstate_needs_review",
            "reference": reference,
            "why": f"voided_reason={voided_reason or 'unrecorded'}, not a dispute",
        }

    payment.voided_at = None
    payment.voided_reason = None
    # Flush for the same reason the reversal does: `_recalculate_invoice` SUMs
    # non-voided payments with a SELECT, and on autoflush=False the pending
    # un-void would not be visible to it.
    db.flush()
    invoice = db.get(Invoice, payment.invoice_id)
    if invoice is not None:
        _recalculate_invoice(invoice, db)
        # The invoice was paid when the customer paid it, not when a dispute
        # closed months later. `_recalculate_invoice` re-stamps `paid_at` on
        # the flip back to paid, which would move this invoice's revenue into
        # the wrong period on every report grouping by it — a silent
        # restatement of a closed month.
        #
        # The invoice's own `paid_at` cannot be read back: the REVERSAL nulled
        # it on the way out (that is what re-opened the invoice), so by the
        # time this runs it is already gone. The payment row is the surviving
        # record of when the money actually arrived, so take it from there.
        when_paid = getattr(payment, "created_at", None)
        if when_paid is None:
            pay_date = getattr(payment, "payment_date", None)
            if pay_date is not None:
                when_paid = datetime.combine(pay_date, dt_time.min, tzinfo=timezone.utc)
        if when_paid is not None and invoice.paid_at is not None:
            invoice.paid_at = when_paid
    _audit_payment_reversal(
        db, payment, action="payment_reinstated", reason=reason,
        detail={"dispute_event": reason},
    )
    db.commit()
    logger.warning(
        "payment_reinstated reference=%s invoice=%s reason=%s — the money stood",
        reference, payment.invoice_id, reason,
    )
    return {"status": "reinstated", "invoice_id": str(payment.invoice_id), "reason": reason}


def handle_payment_webhook(event: dict, db: Session) -> dict:
    """Process Stripe payment events from the webhook router.

    Raises on unexpected failure — the router turns that into a 500 so Stripe
    retries with backoff. Swallowing the error and returning 200 (the pre-
    2026-08-04 behavior) tells Stripe the event was handled and it is never
    redelivered, so a transient DB failure silently loses a payment.
    """
    event_type: str = event.get("type", "")
    data: dict = event.get("data", {}).get("object", {})
    # Connect: the envelope names the account the event belongs to. Any live
    # read we make has to look in the same place the object lives.
    connected_account: str = str(event.get("account") or "")

    if event_type == "payment_intent.processing":
        # M16. Nothing to record as money — the debit has not settled — but a
        # customer initiating a bank transfer is an action the office should
        # find on the invoice's trail, and it is what "why is the pay page
        # refusing?" reconstructs from later.
        invoice_id = (data.get("metadata") or {}).get("invoice_id", "")
        if not invoice_id:
            return {"status": "no_invoice_id"}
        # Adversarial review: a CARD intent also transits `processing` (briefly,
        # e.g. some 3DS flows). Writing "ach_payment_processing" for it would be
        # a trail row that lies about the method — and the M16 gate keys off
        # live status, not this event, so skipping costs nothing.
        if "us_bank_account" not in (data.get("payment_method_types") or []):
            return {"status": "not_ach", "invoice_id": str(invoice_id)}
        try:
            from gdx_dispatch.core.audit import log_audit_event_sync

            log_audit_event_sync(
                db=db,
                tenant_id=None,
                user_id="stripe-webhook",
                action="ach_payment_processing",
                entity_type="invoice",
                entity_id=str(invoice_id),
                details={
                    "intent_id": str(data.get("id") or ""),
                    "amount_cents": int(data.get("amount") or 0),
                },
            )
            db.commit()
        except Exception:
            # The trail is wanted; a failure to write it must not make Stripe
            # retry an event that moves no money.
            logger.exception("ach_processing_audit_failed invoice=%s", invoice_id)
        return {"status": "ach_processing_noted", "invoice_id": str(invoice_id)}

    if event_type == "payment_intent.succeeded":
        invoice_id: str = (data.get("metadata") or {}).get("invoice_id", "")
        if not invoice_id:
            return {"status": "no_invoice_id"}
        # M4: `amount_received` is in the intent's own minor unit. Recording it
        # as dollars is only correct for USD, so refuse anything else rather
        # than booking a foreign-currency charge at face value. Loud, because
        # a mismatch here means money moved that we cannot record truthfully.
        event_currency = str(data.get("currency") or CURRENCY).lower()
        if event_currency != CURRENCY:
            logger.error(
                "payment_currency_mismatch invoice=%s currency=%s intent=%s — "
                "NOT recorded; refund at the processor",
                invoice_id, event_currency, data.get("id"),
            )
            return {"status": "currency_mismatch", "currency": event_currency}
        # ACH settles asynchronously (1-2 business days), so this webhook —
        # not /confirm — is how most bank payments get recorded. Label the
        # method from the intent instead of assuming "card", or every ACH
        # payment lands in the books as a card payment.
        pm_types = data.get("payment_method_types") or []
        method = "ach" if "us_bank_account" in pm_types else "card"
        invoice = db.get(Invoice, UUID(invoice_id))
        if invoice is None or invoice.deleted_at is not None:
            return {"status": "no_invoice", "invoice_id": invoice_id}
        # NOTE: no `status != "paid"` guard. _mark_invoice_paid is idempotent
        # on (invoice_id, reference), so a redelivery is a no-op, but a SECOND
        # genuine payment on an already-paid invoice must still be recorded —
        # otherwise the customer's money sits at Stripe with no Payment row
        # and nothing to refund against.
        _mark_invoice_paid(
            invoice, db,
            external_ref=data.get("id"),
            method=method,
            amount=(data.get("amount_received") or 0) / 100.0,
            source="stripe-webhook",
            # The same account the rest of this handler reads from: "any live
            # read we make has to look in the same place the object lives."
            connected_account=connected_account,
        )
        logger.info("Invoice %s marked paid via webhook", invoice_id)
        # Receipt email placeholder — wire up notification service here
        # send_receipt_email(invoice)
        return {"status": "paid", "invoice_id": invoice_id}

    # Money leaving again. `charge.*` events carry the PaymentIntent id in
    # `payment_intent`; that is the same value stored as Payment.reference.
    if event_type == "charge.refunded":
        # M3 (money audit 2026-08-04): Stripe fires `charge.refunded` for
        # PARTIAL refunds too. This used to route straight to the full void, so
        # refunding $50 of a $500 payment as goodwill voided the entire $500 —
        # the balance came back, the invoice flipped from paid to sent, and
        # dunning chased a customer who had paid in full.
        return _apply_charge_refund(db, data)

    if event_type == "charge.dispute.created":
        # A dispute is not a stale-ordering problem, so it does NOT go through
        # the superseded check — the intent keeps pointing at the disputed
        # charge, and routing disputes through that check would refuse every
        # one of them.
        #
        # But it must not reverse unconditionally either. Stripe's `warning_*`
        # dispute statuses are INQUIRIES: the bank is asking a question and
        # **no funds have been withdrawn**. ACH raises these routinely. Voiding
        # a real payment over a paperwork request re-opens the invoice, puts a
        # customer who paid back into dunning, and books cash as gone that is
        # still sitting there. Reverse only once money has actually moved.
        dispute_status = str(data.get("status") or "")
        if dispute_status.startswith("warning_"):
            logger.warning(
                "dispute_inquiry_no_funds_moved charge=%s intent=%s status=%s — "
                "NOT reversing; the bank is asking, not taking.",
                data.get("id"), data.get("payment_intent"), dispute_status,
            )
            return {
                "status": "dispute_inquiry_noted",
                "dispute_status": dispute_status,
                "reference": str(data.get("payment_intent") or ""),
            }
        return _reverse_recorded_payment(
            db, str(data.get("payment_intent") or ""), event_type
        )

    # M15. Stripe publishes the money movement itself, and these are the only
    # two events that mean cash actually moved because of a dispute:
    #
    #   charge.dispute.funds_withdrawn  "Occurs when funds are removed from
    #                                    your account due to a dispute."
    #   charge.dispute.funds_reinstated "Occurs when funds are reinstated to
    #                                    your account after a dispute is closed."
    #
    # Keying on these rather than on `closed`+status is deliberate. It closes
    # the lifecycle at both ends AND covers the case the inquiry guard above
    # opens: an inquiry we correctly declined to reverse can still ESCALATE,
    # and when it does the withdrawal arrives here. `dispute.created` alone
    # could never have seen that.
    if event_type == "charge.dispute.funds_withdrawn":
        return _reverse_recorded_payment(
            db, str(data.get("payment_intent") or ""), event_type
        )

    if event_type == "charge.dispute.funds_reinstated":
        return _reinstate_reversed_payment(
            db, str(data.get("payment_intent") or ""), event_type
        )

    if event_type == "charge.failed":
        # M14: a card retry reuses the intent, and Stripe does not guarantee
        # delivery order, so this can be attempt one's decline arriving after
        # attempt two's success.
        return _reverse_unless_superseded(
            db,
            str(data.get("payment_intent") or ""),
            event_type,
            # `data.object` IS the charge here, so its id is the attempt that
            # failed — exactly what has to be compared against the intent's
            # current charge.
            failed_charge_id=str(data.get("id") or ""),
            connected_account=connected_account,
        )

    if event_type == "payment_intent.payment_failed":
        invoice_id = (data.get("metadata") or {}).get("invoice_id", "")
        failure_msg = (data.get("last_payment_error") or {}).get("message", "unknown")
        logger.warning(
            "PaymentIntent failed for invoice %s: %s",
            invoice_id,
            failure_msg,
        )
        # An ACH return can arrive as payment_failed AFTER a succeeded event,
        # so anything already recorded for this intent must come back off —
        # UNLESS the intent is currently succeeded, in which case this event is
        # a superseded earlier attempt rather than a return (M14). A returned
        # ACH leaves the intent not-succeeded, which is what separates them.
        reversal = _reverse_unless_superseded(
            db,
            str(data.get("id") or ""),
            event_type,
            # `data.object` is the INTENT here; the charge that failed is the
            # one it pointed at when the event fired.
            failed_charge_id=str(data.get("latest_charge") or ""),
            connected_account=connected_account,
        )
        # Tenant notification placeholder — wire up notification service here
        # notify_tenant_payment_failed(invoice_id, failure_msg)
        return {
            "status": "failed",
            "invoice_id": invoice_id,
            "reason": failure_msg,
            "reversal": reversal["status"],
        }

    return {"status": "ignored"}
