"""Text an invoice to the customer — one SMS carrying the public pay link.

Shared by the desktop route (``POST /api/invoices/{id}/send-sms``) and the
tech's phone (``POST /api/mobile/invoices/{id}/send-sms``). Each route owns its
own authorization (permission gate vs. job ownership + office verification).
This adapter owns what is invoice-specific — who may receive it, what it says,
what makes the link live and what "delivered" stamps; how the text goes out
and what it leaves behind is core/link_sms.py, shared with estimates.

No PDF, no MMS: the /pay/{token} page IS the invoice, readable on a phone, and
a link is the one thing every handset renders. So a text without a working pay
link is refused rather than sent — there would be nothing for the customer to
open.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core import link_sms
from gdx_dispatch.core.link_sms import prior_attempt as _prior_attempt  # noqa: F401 — re-exported for callers/tests
from gdx_dispatch.core.link_sms import tenant_uuid  # noqa: F401 — the routes import it from here


def _balance(invoice: Any) -> float:
    try:
        return float(invoice.balance_due or 0)
    except (TypeError, ValueError):
        return 0.0


def default_body(db: Session, invoice: Any, pay_url: str) -> str:
    who = link_sms.company_name(db)
    number = invoice.invoice_number or str(invoice.id)[:8]
    prefix = f"{who}: " if who else ""
    return f"{prefix}Invoice #{number} — ${_balance(invoice):,.2f} due. View and pay: {pay_url}"


def prepare(db: Session, invoice: Any, *, to_override: str | None = None) -> dict[str, Any]:
    """Everything the composer shows, or the reason it cannot be sent.

    Never writes. ``blocked`` is ``None`` when the text can go, else a
    ``{code, message}`` the UI shows verbatim. Every invoice carries its pay
    token from creation (``invoices.public_token`` is NOT NULL), so the body
    shown here is exactly the body that would be sent.
    """
    from gdx_dispatch.core.invoice_delivery import AWAITING_VERIFICATION_DETAIL, draft_needs_verification
    from gdx_dispatch.core.payments import public_pay_url

    customer = link_sms.active_customer(db, invoice.customer_id)
    to, who_blocked = link_sms.recipient(customer, to_override)
    pay_url = public_pay_url(invoice.public_token)

    blocked: dict[str, str] | None = None
    if draft_needs_verification(invoice):
        blocked = {"code": "awaiting_verification", "message": AWAITING_VERIFICATION_DETAIL}
    elif invoice.status == "void":
        blocked = {"code": "invoice_void", "message": "This invoice is void — it cannot be sent."}
    elif _balance(invoice) <= 0:
        blocked = {"code": "no_balance_due", "message": "Nothing is owed on this invoice, so there is nothing to pay by text."}
    elif who_blocked:
        blocked = who_blocked
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
    from gdx_dispatch.core.payments import public_pay_url
    from gdx_dispatch.modules.ledger.service import transition_invoice_status

    prep = prepare(db, invoice, to_override=to_override)
    if prep["blocked"]:
        b = prep["blocked"]
        raise link_sms.refuse(409 if b["code"] != "no_valid_phone" else 422, b["code"], b["message"])
    pay_url: str = public_pay_url(invoice.public_token)  # prepare() refused when None

    def stage() -> None:
        # /pay 404s a draft, so the link is live only once the invoice is sent.
        if invoice.status == "draft":
            transition_invoice_status(db, invoice, "sent", actor=actor_id)  # GL S5

    def stamp(now) -> None:
        invoice.sent_at = now
        invoice.sent_via = "sms"

    return link_sms.send_link(
        db,
        invoice,
        noun="invoice",
        entity_type="invoice",
        customer=link_sms.active_customer(db, invoice.customer_id),
        to=prep["to"],
        link=pay_url,
        body=(body_override or "").strip() or default_body(db, invoice, pay_url),
        stage=stage,
        stamp=stamp,
        tenant_id=tenant_id,
        actor_id=actor_id,
        resend_unconfirmed=resend_unconfirmed,
        audit_action=audit_action,
        request=request,
    )
