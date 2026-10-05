"""Text an estimate to the customer — one SMS carrying the approval-page link.

Shared by the desktop route (``POST /api/estimates/{id}/send-sms``, gated on
``estimates.send``) and the tech's phone (``POST /api/mobile/quotes/{id}/
send-sms``, job ownership — docs/tech_mobile.md: techs do not hold
``estimates.send``). This adapter owns what is estimate-specific; how the
text goes out and what it leaves behind is core/link_sms.py, shared with
invoices.

The /proposals/{token} page is the estimate — tiers, signature, deposit — and
it only resolves once ``sent_at`` is set (modules/proposals/router.py). So
``stage`` stamps sent_at: a text whose outcome is unconfirmed still leaves a
working link, while ``sent_via`` — the confirmed-delivery fact — is only set on
success, alongside everything the email send does (status, expiry, the
``estimate.sent`` event).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core import customer_page_preview, link_sms

FINALIZED = {"accepted", "declined"}
# The only statuses a text may move back to "sent" (see stage()).
REVIVABLE = {"draft", "expired"}


def _link(estimate: Any) -> str:
    from gdx_dispatch.routers.estimates import _public_proposal_url

    return _public_proposal_url(estimate)


def default_body(db: Session, estimate: Any, link: str) -> str:
    who = link_sms.company_name(db)
    number = estimate.estimate_number or str(estimate.id)[:8]
    prefix = f"{who}: " if who else ""
    return f"{prefix}Your estimate #{number} is ready. Review and approve: {link}"


def as_texted(estimate: Any, now: Any) -> dict[str, Any] | None:
    """What a text delivered at ``now`` leaves the customer looking at:
    ``{"status", "valid_until"}``, or None for an accepted/declined estimate,
    which a text never changes.

    Read-only twin of ``stamp()`` in ``send()`` below — the last write a
    delivered text makes, so the one the customer sees: any estimate not
    finalized goes to "sent" with sent_at = now and the send expiry
    re-applied, including one already "sent" whose date has lapsed. For the
    staff preview of the customer page (core/customer_page_preview.py).
    Change one, change both."""
    from gdx_dispatch.routers.estimates import send_expiry

    if estimate.status in FINALIZED:
        return None
    return {"status": "sent", "valid_until": send_expiry(estimate, now)}


def prepare(db: Session, estimate: Any, *, to_override: str | None = None) -> dict[str, Any]:
    """Everything the composer shows, or the reason it cannot be sent. Never
    writes; ``blocked`` is ``None`` or a ``{code, message}`` shown verbatim."""
    customer = link_sms.active_customer(db, estimate.customer_id)
    to, who_blocked = link_sms.recipient(customer, to_override)
    link = _link(estimate)

    blocked: dict[str, str] | None = None
    if estimate.status in FINALIZED:
        blocked = {"code": "estimate_finalized", "message": f"This estimate is already {estimate.status}."}
    elif who_blocked:
        blocked = who_blocked
    elif not link:
        blocked = {
            "code": "link_unavailable",
            "message": "The public approval link is not set up (public URL missing), so there is nothing to text.",
        }
    return {
        "to": to,
        "customer_name": customer.name if customer is not None else None,
        "body": default_body(db, estimate, link) if link else None,
        "blocked": blocked,
        # The approval page the link opens, for staff, without counting as
        # the customer opening it (core/customer_page_preview.py).
        "preview_url": customer_page_preview.preview_url("estimate", estimate.id, estimate.public_token),
    }


def send(
    db: Session,
    estimate: Any,
    *,
    tenant_id: UUID,
    actor_id: str | None,
    to_override: str | None = None,
    body_override: str | None = None,
    resend_unconfirmed: bool = False,
    audit_action: str = "estimate_sent_sms",
    request: Any = None,
) -> dict[str, Any]:
    """Send the estimate by SMS. Raises HTTPException with a ``{code, message}``
    body on every refusal; returns the delivery facts on success."""
    from gdx_dispatch.core.audit import utcnow
    from gdx_dispatch.routers.estimates import _apply_send_expiry, _emit_estimate_sent

    prep = prepare(db, estimate, to_override=to_override)
    if prep["blocked"]:
        b = prep["blocked"]
        raise link_sms.refuse(409 if b["code"] != "no_valid_phone" else 422, b["code"], b["message"])
    link = _link(estimate)

    def stage() -> None:
        # A texted link must be one the customer can ACT on, not just open:
        # /proposals 404s until sent_at is set, and the public accept 409s
        # unless status is sent/rejected (modules/proposals/router.py). So a
        # never-sent draft, an expired estimate and a reopened one (status
        # draft, sent_at kept) get sent_at restamped — expiry refreshes
        # relative to it — and status back to sent.
        #
        # An explicit list of what may be revived, never a negated set: after
        # a rollback this re-reads the row, and the customer may have accepted
        # or declined while Phone.com was answering — a decision is never
        # undone by a text.
        if estimate.status in FINALIZED:
            return
        if estimate.sent_at is None or estimate.status in REVIVABLE:
            estimate.sent_at = utcnow()
            estimate.status = "sent"
            _apply_send_expiry(estimate)

    def stamp(now) -> None:
        # Mirrors the email send's success block (routers/estimates.py) — but
        # if the customer accepted/declined while the text was in flight, only
        # the delivery channel is recorded: status, expiry and the
        # estimate.sent event would all contradict their decision.
        # as_texted() above is this block's read-only twin (the staff
        # preview of the customer page). Change one, change both.
        estimate.sent_via = "sms"
        estimate.updated_at = now
        if estimate.status in FINALIZED:
            return
        estimate.status = "sent"
        estimate.sent_at = now
        _apply_send_expiry(estimate)
        _emit_estimate_sent(db, estimate)

    return link_sms.send_link(
        db,
        estimate,
        noun="estimate",
        entity_type="estimate",
        customer=link_sms.active_customer(db, estimate.customer_id),
        to=prep["to"],
        link=link,
        body=(body_override or "").strip() or default_body(db, estimate, link),
        stage=stage,
        stamp=stamp,
        tenant_id=tenant_id,
        actor_id=actor_id,
        resend_unconfirmed=resend_unconfirmed,
        audit_action=audit_action,
        request=request,
    )

