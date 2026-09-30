"""Text a customer one link — the engine under "text an invoice" and "text an
estimate".

Moved out of core/invoice_sms.py (#820) unchanged in behaviour so estimates
share every rule the invoice path was audited into, instead of a copy that
drifts. The document adapters (core/invoice_sms.py, core/estimate_sms.py) own
what is document-specific — who may receive it, the default wording, what
makes the customer's link live, and what "delivered" stamps; this module owns
how the text goes out and what it leaves behind.

Order matters because an SMS cannot be recalled:

1. every refusal happens before anything is written;
2. the document row is locked (Postgres) so two simultaneous sends serialize,
   then the attempt is committed as a "sending" message row;
3. the adapter's ``stage`` makes the customer's link live (an invoice leaves
   draft — /pay 404s a draft; an estimate gets sent_at — /proposals 404s
   without it), staged but not committed;
4. Phone.com is called once — the client does not re-POST a text after a read
   timeout or 5xx. A 4xx or no-connection means it never left: the staging
   rolls back and the row is "failed". A read timeout or 5xx means it may have
   arrived: the row is "unknown" and the staging IS committed, so a link that
   did arrive works — but the delivery stamp (sent_via) stays unset;
5. on success the staging, the adapter's ``stamp`` and the message row commit
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
correct — but the document keeps its delivery stamp unset, because nothing
links the imported row back to it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# A second tap, a retried request, a second tab: the same link to the same
# number inside this window is refused as a duplicate.
DUPLICATE_WINDOW_SECONDS = 60


class SendLinkSmsIn(BaseModel):
    """Composer payload for every /sms-preview and /send-sms route (office and
    tech, invoices and estimates) — empty keeps the customer's phone and the
    default message."""

    model_config = ConfigDict(extra="forbid")
    to: str | None = Field(default=None, max_length=40)
    body: str | None = Field(default=None, max_length=1600)
    # The operator checked the thread after an unconfirmed attempt and wants
    # to send anyway (see "Re-sending" above).
    resend_unconfirmed: bool = False


def refuse(status: int, code: str, message: str) -> HTTPException:
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


def company_name(db: Session) -> str:
    from gdx_dispatch.models.tenant_models import AppSettings

    app = db.query(AppSettings).first()
    return ((app.company_name if app else "") or "").strip()


def as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def active_customer(db: Session, customer_id: Any):
    from gdx_dispatch.models.tenant_models import Customer

    if not customer_id:
        return None
    return db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()


def recipient(customer: Any, to_override: str | None) -> tuple[str | None, dict[str, str] | None]:
    """(E.164 number, refusal) for the customer-side checks every document
    shares, in the order the operator should see them."""
    from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164

    raw_to = (to_override or "").strip() or (customer.phone if customer is not None else None)
    to = normalize_e164(raw_to) if raw_to else None
    if customer is None:
        return to, {"code": "no_customer", "message": "There is no customer to text."}
    if customer.sms_opt_out:
        return to, {"code": "sms_opt_out", "message": "This customer has opted out of text messages."}
    if to is None:
        return None, {
            "code": "no_valid_phone",
            "message": "No valid mobile number — add one to the customer or type it here.",
        }
    return to, None


def ensure_link(body: str, link: str) -> str:
    """The link is the whole point of the text — an edit that dropped it gets
    it appended rather than sending something the customer cannot open."""
    if link not in body:
        body = f"{body.rstrip()}\n{link}"
    return body


def prior_attempt(db: Session, to: str, link: str) -> str | None:
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

    mine = [r for r in rows if link in (r.body or "")]
    if any(_at(r) is not None and _at(r) >= cutoff for r in mine):
        return "recent"
    # An unconfirmed attempt stops mattering once a later copy of the same
    # link to the same number is confirmed — the operator's deliberate "send
    # anyway", or Phone.com's sync importing the copy that actually went out.
    # Without this, one timeout would block the document forever: nothing
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


def definitely_not_sent(exc: Exception) -> bool:
    """True when a failed send provably never reached the customer: a 4xx
    (Phone.com refused it) or no connection at all. Anything else — a read
    timeout, a 5xx — may have been delivered. Shared by every sender that must
    never text a customer twice (this module, the scheduled-text drain)."""
    status_code = getattr(exc, "status_code", None)
    return (status_code is not None and 400 <= status_code < 500) or isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
    )


def adopt_message_id(db: Session, msg: Any, message_id: str) -> None:
    """Give the pending row Phone.com's real id — unless Phone.com's
    webhook/poll already imported this message under it (UNIQUE): then the
    pending row keeps its own id rather than colliding and failing a text that
    did go out."""
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage

    taken = db.execute(
        select(PhoneComMessage.id).where(
            PhoneComMessage.phone_com_message_id == message_id, PhoneComMessage.id != msg.id
        )
    ).first()
    if not taken:
        msg.phone_com_message_id = message_id


def send_link(
    db: Session,
    doc: Any,
    *,
    noun: str,
    entity_type: str,
    customer: Any,
    to: str,
    link: str,
    body: str,
    stage: Callable[[], None],
    stamp: Callable[[datetime], None],
    tenant_id: UUID,
    actor_id: str | None,
    resend_unconfirmed: bool,
    audit_action: str,
    request: Any,
) -> dict[str, Any]:
    """Send ``body`` (carrying ``link``) to ``to`` for ``doc``. The adapter
    has already refused anything document-specific. Raises HTTPException with a
    ``{code, message}`` body on every refusal; returns the delivery facts.

    ``stage()`` makes the link live and must be idempotent (it runs again after
    a rollback); ``stamp(now)`` records confirmed delivery."""
    from gdx_dispatch.core.audit import audit_best_effort
    from gdx_dispatch.modules.phone_com.client import PhoneComAPIError
    from gdx_dispatch.modules.phone_com.models import PhoneComMessage
    from gdx_dispatch.modules.phone_com.outbound_did import resolve_outbound_did
    from gdx_dispatch.modules.phone_com.router import _get_phone_com_client, _thread_key_for

    from_number = resolve_outbound_did(
        db, customer_id=doc.customer_id, to_number=to, sending_user_id=actor_id
    )
    if not from_number:
        raise refuse(
            409, "no_outbound_number",
            "No outbound texting number is configured (Settings → Phone.com → Default outbound number).",
        )
    client = _get_phone_com_client(tenant_id, db, db)

    # Serialize simultaneous sends of this document (a second tab, two
    # devices, a proxy retry): the lock holds until the pending row below
    # commits, so the second request sees it. No-op on SQLite.
    db.execute(select(type(doc)).where(type(doc).id == doc.id).with_for_update()).first()
    prior = prior_attempt(db, to, link)
    if prior == "recent":
        raise refuse(409, "duplicate_send_suppressed", f"This {noun} was just texted to that number.")
    if prior == "unconfirmed" and not resend_unconfirmed:
        raise refuse(
            409, "prior_attempt_unconfirmed",
            f"An earlier text of this {noun} to that number was never confirmed by Phone.com — "
            "it may have arrived. Check the customer's SMS thread; send again only if it did not.",
        )

    body = ensure_link(body, link)
    try:
        body.encode("utf-8")
    except UnicodeEncodeError:
        # Refused here so the ValueError branch around the send below can
        # only ever mean "a 2xx whose reply would not parse".
        raise refuse(422, "body_not_encodable", "The message contains characters that cannot be sent.") from None
    if len(body) > client._SMS_MAX_BODY:
        raise refuse(422, "body_too_long", f"Message is {len(body)} characters; the limit is {client._SMS_MAX_BODY}.")

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
        customer_id=doc.customer_id,
        job_id=getattr(doc, "job_id", None),
        sent_by_user_id=as_uuid(actor_id),
        raw_payload={},
    )
    db.add(msg)
    db.commit()

    prior_status = doc.status

    def _trail(action: str, **extra: Any) -> None:
        # Every outcome leaves a row — a failed or unconfirmed attempt still
        # wrote a message row, and an unconfirmed one moved the document.
        audit_best_effort(
            db,
            action=action,
            entity_type=entity_type,
            entity_id=str(doc.id),
            tenant_id=str(tenant_id),
            user_id=str(actor_id) if actor_id else None,
            request=request,
            details={
                "channel": "sms",
                "to_last4": to[-4:],
                "from_number": from_number,
                "prior_status": prior_status,
                "status": doc.status,
                "customer_name": getattr(customer, "name", None),
                **extra,
            },
        )

    stage()
    db.flush()

    try:
        result = client.send_message(from_number=from_number, to_number=to, body=body)
    except (ValueError, TypeError):
        # PhoneComAPIError covers every non-2xx, so this is a 2xx whose body
        # would not parse (empty / not JSON): Phone.com ACCEPTED the text. It
        # is on the customer's phone — record it as sent, not as a failure.
        log.warning("%s_sms_unreadable_reply id=%s", entity_type, doc.id)
        result = {}
    except (PhoneComAPIError, httpx.HTTPError) as exc:
        db.rollback()
        status_code = getattr(exc, "status_code", None)
        definite = definitely_not_sent(exc)
        msg.delivery_status = "failed" if definite else "unknown"
        msg.delivery_failed_reason = str(exc)[:500]
        if not definite:
            # It may be on the customer's phone: keep its link alive. The
            # delivery stamp stays unset — unconfirmed.
            stage()
        db.commit()
        log.warning("%s_sms_send_failed id=%s status=%s definite=%s",
                    entity_type, doc.id, status_code, definite)
        _trail(
            f"{entity_type}_sms_failed" if definite else f"{entity_type}_sms_unconfirmed",
            source=audit_action, provider_status=status_code, error=type(exc).__name__,
        )
        if definite:
            raise refuse(502, "sms_provider_error", f"The text was not sent — Phone.com: {exc}") from exc
        raise refuse(
            504, "sms_outcome_unknown",
            f"Phone.com did not confirm the text — it may still have been delivered, so the {noun} "
            "is marked sent and its link works. Check the customer's SMS thread before sending again.",
        ) from exc

    if not isinstance(result, dict):  # a 2xx with a JSON list / scalar body
        result = {}
    message_id = str(result.get("id") or msg.phone_com_message_id)

    def _record() -> None:
        # Re-read the document under a row lock before staging/stamping: the
        # POST took seconds, and another request (the customer accepting from
        # the texted link, the office voiding) may have committed meanwhile.
        # Without this the adapters' guards would judge the object as it was
        # loaded before the call. (Postgres waits for, then returns, the
        # latest committed row; the lock is a no-op on SQLite.)
        db.refresh(doc, with_for_update=True)
        adopt_message_id(db, msg, message_id)
        msg.delivery_status = result.get("status") or "queued"
        msg.raw_payload = result
        stage()
        stamp(now)
        db.commit()

    try:
        _record()
    except Exception:
        # The text IS on the customer's phone. Try once more in a fresh
        # transaction before giving up — the rollback discarded the real
        # message id and the staging along with the stamp.
        log.exception("%s_sms_record_failed_retrying id=%s message=%s", entity_type, doc.id, message_id)
        db.rollback()
        try:
            _record()
        except Exception as exc:
            log.exception("%s_sms_sent_but_record_failed id=%s message=%s", entity_type, doc.id, message_id)
            db.rollback()
            _trail(f"{entity_type}_sms_sent_unrecorded", source=audit_action, phone_com_message_id=message_id)
            raise refuse(
                500, "sent_but_not_recorded",
                f"The text went out, but the {noun} could not be updated. Do not resend — "
                "mark it sent by hand or ask the office.",
            ) from exc
    db.refresh(doc)

    _trail(audit_action, phone_com_message_id=message_id)
    return {
        "sms_sent": True,
        "to": to,
        "phone_com_message_id": message_id,
        "delivery_status": msg.delivery_status,
    }
