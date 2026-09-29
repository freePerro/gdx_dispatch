"""Text an invoice to the customer — one SMS carrying the public pay link.

Shared by the desktop route (``POST /api/invoices/{id}/send-sms``) and the
tech's phone (``POST /api/mobile/invoices/{id}/send-sms``). Each route owns its
own authorization (permission gate vs. job ownership + office verification);
everything after that — who may receive it, what it says, how it goes out and
what it leaves behind — lives here so the two cannot drift.

No PDF, no MMS: the /pay/{token} page IS the invoice, readable on a phone, and
a link is the one thing every handset renders. So a text without a working pay
link is refused rather than sent — there would be nothing for the customer to
open.

Order matters because an SMS cannot be recalled:

1. every refusal happens before anything is written;
2. the invoice row is locked (Postgres) so two simultaneous sends serialize,
   then the attempt is committed as a "sending" message row;
3. a draft's transition to sent is staged (the /pay page 404s a draft, so the
   link must be live by the time the customer taps it);
4. Phone.com is called once — the client does not re-POST a text after a read
   timeout or 5xx. A 4xx or no-connection means it never left: the transition
   rolls back and the row is "failed". A read timeout or 5xx means it may have
   arrived: the row is "unknown" and the invoice IS moved to sent, so a link
   that did arrive works;
5. on success the transition, the delivery stamp and the message row commit
   together (retried once if that commit fails), then the audit row is written
   best-effort — the text is already on the customer's phone, so a failed
   trail row must not become a 500 that invites a second send.

Re-sending: the same link to the same number inside a minute is refused
outright. An attempt that never confirmed ("sending"/"unknown") blocks later
re-sends — with no clock — until a LATER confirmed copy of that link to that
number exists: either the operator checked the thread and chose "send anyway"
(``resend_unconfirmed``), or Phone.com's poll/webhook imported the copy that
did go out (it lands as its own row; its id cannot match ``pending-…``). In
that second case the text is known to have arrived, so the block lifting is
correct — but the invoice keeps sent_at/sent_via unset, because nothing links
the imported row back to it.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# A second tap, a retried request, a second tab: the same link to the same
# number inside this window is refused as a duplicate.
DUPLICATE_WINDOW_SECONDS = 60


def _refuse(status: int, code: str, message: str) -> HTTPException:
    # {code, message}: the shape useApi renders (message) and branches on (code).
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def tenant_uuid(user: dict | None, request: Any) -> UUID:
    """The id the Phone.com token is stored under. Settings → Phone.com saves it
    keyed by the login token's ``tenant_id`` claim (routers/phone_com_settings),
    so both the office and the tech route read that claim first — reading a
    different source on one route would make texting work on one screen and
    503 "not configured" on the other."""
    raw = (user or {}).get("tenant_id") or (getattr(getattr(request, "state", None), "tenant", None) or {}).get("id")
    if not raw:
        raise HTTPException(status_code=400, detail="missing tenant context")
    return raw if isinstance(raw, UUID) else UUID(str(raw))


def _balance(invoice: Any) -> float:
    try:
        return float(invoice.balance_due or 0)
    except (TypeError, ValueError):
        return 0.0


def _company_name(db: Session) -> str:
    from gdx_dispatch.models.tenant_models import AppSettings

    app = db.query(AppSettings).first()
    return ((app.company_name if app else "") or "").strip()


def default_body(db: Session, invoice: Any, pay_url: str) -> str:
    who = _company_name(db)
    number = invoice.invoice_number or str(invoice.id)[:8]
    prefix = f"{who}: " if who else ""
    return f"{prefix}Invoice #{number} — ${_balance(invoice):,.2f} due. View and pay: {pay_url}"


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _customer(db: Session, invoice: Any):
    from gdx_dispatch.models.tenant_models import Customer

    if not invoice.customer_id:
        return None
    return db.execute(
        select(Customer).where(Customer.id == invoice.customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()


def prepare(db: Session, invoice: Any, *, to_override: str | None = None) -> dict[str, Any]:
    """Everything the composer shows, or the reason it cannot be sent.

    Never writes. ``blocked`` is ``None`` when the text can go, else a
    ``{code, message}`` the UI shows verbatim. Every invoice carries its pay
    token from creation (``invoices.public_token`` is NOT NULL), so the body
    shown here is exactly the body that would be sent.
    """
    from gdx_dispatch.core.payments import public_pay_url
    from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164

    customer = _customer(db, invoice)
    raw_to = (to_override or "").strip() or (customer.phone if customer is not None else None)
    to = normalize_e164(raw_to) if raw_to else None
    pay_url = public_pay_url(invoice.public_token)

    from gdx_dispatch.core.invoice_delivery import AWAITING_VERIFICATION_DETAIL, draft_needs_verification

    blocked: dict[str, str] | None = None
    if draft_needs_verification(invoice):
        blocked = {"code": "awaiting_verification", "message": AWAITING_VERIFICATION_DETAIL}
    elif invoice.status == "void":
        blocked = {"code": "invoice_void", "message": "This invoice is void — it cannot be sent."}
    elif _balance(invoice) <= 0:
        blocked = {"code": "no_balance_due", "message": "Nothing is owed on this invoice, so there is nothing to pay by text."}
    elif customer is None:
        blocked = {"code": "no_customer", "message": "This invoice has no customer to text."}
    elif customer.sms_opt_out:
        blocked = {"code": "sms_opt_out", "message": "This customer has opted out of text messages."}
    elif to is None:
        blocked = {
            "code": "no_valid_phone",
            "message": "No valid mobile number — add one to the customer or type it here.",
        }
    elif pay_url is None:
        blocked = {
            "code": "pay_link_unavailable",
            "message": "Online payment is not set up (Stripe keys or public URL missing), so there is no link to text.",
        }
    return {
        "to": to,
        "customer_name": customer.name if customer is not None else None,
        "body": default_body(db, invoice, pay_url) if pay_url else None,
        "blocked": blocked,
    }


def _ensure_link(body: str, pay_url: str) -> str:
    """The link is the whole point of the text — an edit that dropped it gets
    it appended rather than sending a bill the customer cannot open."""
    if pay_url not in body:
        body = f"{body.rstrip()}\n{pay_url}"
    return body


def _prior_attempt(db: Session, to: str, pay_url: str) -> str | None:
    """``"recent"`` when this link went to this number inside the duplicate
    window, ``"unconfirmed"`` when an earlier attempt never confirmed (any
    age), else ``None``. A definite failure sent nothing and never blocks."""
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage

    cutoff = datetime.now(UTC) - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    rows = db.execute(
        select(PhoneComMessage.body, PhoneComMessage.sent_at, PhoneComMessage.delivery_status).where(
            PhoneComMessage.direction == "out",
            PhoneComMessage.to_number == to,
            PhoneComMessage.delivery_status.is_distinct_from("failed"),
        )
    ).all()
    def _at(r):
        if r.sent_at is None:
            return None
        return r.sent_at if r.sent_at.tzinfo else r.sent_at.replace(tzinfo=UTC)

    mine = [r for r in rows if pay_url in (r.body or "")]
    if any(_at(r) is not None and _at(r) >= cutoff for r in mine):
        return "recent"
    # An unconfirmed attempt stops mattering once a later copy of the same
    # link to the same number is confirmed — the operator's deliberate "send
    # anyway", or Phone.com's sync importing the copy that actually went out.
    # Without this, one timeout would block the invoice forever: nothing
    # rewrites a pending row.
    confirmed = [_at(r) for r in mine if r.delivery_status not in ("sending", "unknown") and _at(r)]
    latest_confirmed = max(confirmed) if confirmed else None
    # Compared at whole seconds: an imported copy carries Phone.com's
    # created_at (epoch seconds), ours carries microseconds taken before the
    # POST — the same moment would otherwise sort the copy first.
    unresolved = [
        r for r in mine
        if r.delivery_status in ("sending", "unknown")
        and (
            latest_confirmed is None
            or (_at(r) is not None and _at(r).replace(microsecond=0) > latest_confirmed)
        )
    ]
    return "unconfirmed" if unresolved else None


def send(
    db: Session,
    invoice: Any,
    *,
    tenant_id: UUID,
    actor_id: str | None,
    to_override: str | None = None,
    body_override: str | None = None,
    resend_unconfirmed: bool = False,
    audit_action: str = "invoice_sent_sms",
    request: Any = None,
) -> dict[str, Any]:
    """Send the invoice by SMS. Raises HTTPException with a ``{code, message}``
    body on every refusal; returns the delivery facts on success."""
    from gdx_dispatch.core.audit import audit_best_effort
    from gdx_dispatch.core.payments import public_pay_url
    from gdx_dispatch.modules.ledger.service import transition_invoice_status
    from gdx_dispatch.modules.phone_com.client import PhoneComAPIError
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did
    from gdx_dispatch.modules.phone_com.router import _get_phone_com_client, _thread_key_for

    prep = prepare(db, invoice, to_override=to_override)
    if prep["blocked"]:
        b = prep["blocked"]
        raise _refuse(409 if b["code"] != "no_valid_phone" else 422, b["code"], b["message"])
    to: str = prep["to"]
    customer = _customer(db, invoice)

    from_number = resolve_outbound_did(
        db, customer_id=invoice.customer_id, to_number=to, sending_user_id=actor_id
    )
    if not from_number:
        raise _refuse(
            409, "no_outbound_number",
            "No outbound texting number is configured (Settings → Phone.com → Default outbound number).",
        )
    client = _get_phone_com_client(tenant_id, db, db)

    # Serialize simultaneous sends of this invoice (a second tab, two devices,
    # a proxy retry): the lock holds until the pending row below commits, so
    # the second request sees it. No-op on SQLite.
    db.execute(select(type(invoice)).where(type(invoice).id == invoice.id).with_for_update()).first()
    pay_url: str = public_pay_url(invoice.public_token)  # prepare() refused when None
    prior = _prior_attempt(db, to, pay_url)
    if prior == "recent":
        raise _refuse(409, "duplicate_send_suppressed", "This invoice was just texted to that number.")
    if prior == "unconfirmed" and not resend_unconfirmed:
        raise _refuse(
            409, "prior_attempt_unconfirmed",
            "An earlier text of this invoice to that number was never confirmed by Phone.com — "
            "it may have arrived. Check the customer's SMS thread; send again only if it did not.",
        )

    body = (body_override or "").strip() or default_body(db, invoice, pay_url)
    body = _ensure_link(body, pay_url)
    try:
        body.encode("utf-8")
    except UnicodeEncodeError:
        # Refused here so the ValueError branch around the send below can
        # only ever mean "a 2xx whose reply would not parse".
        raise _refuse(422, "body_not_encodable", "The message contains characters that cannot be sent.") from None
    if len(body) > client._SMS_MAX_BODY:
        raise _refuse(422, "body_too_long", f"Message is {len(body)} characters; the limit is {client._SMS_MAX_BODY}.")

    # The attempt is recorded BEFORE Phone.com is called, in its own commit:
    # if the outcome is ambiguous (timeout, 5xx) or our final commit fails
    # after the text went out, this row is what stops a retry from texting
    # the customer twice.
    now = datetime.now(UTC)
    msg = PhoneComMessage(
        phone_com_message_id=f"pending-{uuid4()}",
        thread_key=_thread_key_for(from_number, to),
        direction="out",
        from_number=from_number,
        to_number=to,
        body=body,
        sent_at=now,
        delivery_status="sending",
        attachments=[],
        customer_id=invoice.customer_id,
        job_id=getattr(invoice, "job_id", None),
        sent_by_user_id=_as_uuid(actor_id),
        raw_payload={},
    )
    db.add(msg)
    db.commit()

    def _trail(action: str, **extra: Any) -> None:
        # Every outcome leaves a row — a failed or unconfirmed attempt still
        # wrote a message row, and an unconfirmed one moved the invoice.
        audit_best_effort(
            db,
            action=action,
            entity_type="invoice",
            entity_id=str(invoice.id),
            tenant_id=str(tenant_id),
            user_id=str(actor_id) if actor_id else None,
            request=request,
            details={
                "channel": "sms",
                "to_last4": to[-4:],
                "from_number": from_number,
                "prior_status": prior_status,
                "status": invoice.status,
                "customer_name": getattr(customer, "name", None),
                **extra,
            },
        )

    prior_status = invoice.status
    if invoice.status == "draft":
        transition_invoice_status(db, invoice, "sent", actor=actor_id)  # GL S5
    db.flush()

    try:
        result = client.send_message(from_number=from_number, to_number=to, body=body)
    except (ValueError, TypeError):
        # PhoneComAPIError covers every non-2xx, so this is a 2xx whose body
        # would not parse (empty / not JSON): Phone.com ACCEPTED the text. It
        # is on the customer's phone — record it as sent, not as a failure.
        log.warning("invoice_sms_unreadable_reply invoice=%s", invoice.id)
        result = {}
    except (PhoneComAPIError, httpx.HTTPError) as exc:
        db.rollback()
        status_code = getattr(exc, "status_code", None)
        # 4xx: Phone.com refused it. No connection at all: it never left.
        # Anything else (read timeout, 5xx) may have been delivered.
        definite = (status_code is not None and 400 <= status_code < 500) or isinstance(
            exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        )
        msg.delivery_status = "failed" if definite else "unknown"
        msg.delivery_failed_reason = str(exc)[:500]
        if not definite and prior_status == "draft":
            # It may be on the customer's phone: keep its link alive. The
            # delivery stamp (sent_at / sent_via) stays unset — unconfirmed.
            transition_invoice_status(db, invoice, "sent", actor=actor_id)  # GL S5
        db.commit()
        log.warning("invoice_sms_send_failed invoice=%s status=%s definite=%s",
                    invoice.id, status_code, definite)
        _trail(
            "invoice_sms_failed" if definite else "invoice_sms_unconfirmed",
            source=audit_action, provider_status=status_code, error=type(exc).__name__,
        )
        if definite:
            raise _refuse(502, "sms_provider_error", f"The text was not sent — Phone.com: {exc}") from exc
        raise _refuse(
            504, "sms_outcome_unknown",
            "Phone.com did not confirm the text — it may still have been delivered, so the invoice "
            "is marked sent and its link works. Check the customer's SMS thread before sending again.",
        ) from exc

    if not isinstance(result, dict):  # a 2xx with a JSON list / scalar body
        result = {}
    message_id = str(result.get("id") or msg.phone_com_message_id)

    def _record() -> None:
        # Phone.com's webhook/poll may already have imported this message
        # under its real id (UNIQUE); then the pending row keeps its own id
        # rather than colliding and failing a text that did go out.
        taken = db.execute(
            select(PhoneComMessage.id).where(
                PhoneComMessage.phone_com_message_id == message_id, PhoneComMessage.id != msg.id
            )
        ).first()
        if not taken:
            msg.phone_com_message_id = message_id
        msg.delivery_status = result.get("status") or "queued"
        msg.raw_payload = result
        if invoice.status == "draft":
            transition_invoice_status(db, invoice, "sent", actor=actor_id)  # GL S5
        invoice.sent_at = now
        invoice.sent_via = "sms"
        db.commit()

    try:
        _record()
    except Exception:
        # The text IS on the customer's phone. Try once more in a fresh
        # transaction before giving up — the rollback discarded the real
        # message id and the transition along with the stamp.
        log.exception("invoice_sms_record_failed_retrying invoice=%s message=%s", invoice.id, message_id)
        db.rollback()
        try:
            _record()
        except Exception as exc:
            log.exception("invoice_sms_sent_but_record_failed invoice=%s message=%s", invoice.id, message_id)
            db.rollback()
            _trail("invoice_sms_sent_unrecorded", source=audit_action, phone_com_message_id=message_id)
            raise _refuse(
                500, "sent_but_not_recorded",
                "The text went out, but the invoice could not be updated. Do not resend — "
                "mark it sent by hand or ask the office.",
            ) from exc
    db.refresh(invoice)

    _trail(audit_action, phone_com_message_id=message_id)
    return {
        "sms_sent": True,
        "to": to,
        "phone_com_message_id": message_id,
        "delivery_status": msg.delivery_status,
    }
