"""
gdx_dispatch/core/office_notifications.py — broadcast in-app alerts to the office.

A `Notification` row with user_id=NULL is tenant-broadcast: the topbar bell
count query matches `user_id = :me OR user_id IS NULL`, the 60s poll in
frontend `stores/notifications.js` turns it into the red badge, and
`NotificationsDrawer.vue` deep-links the row by `category` ("estimate" →
/estimates, "lead" → /leads, …). Same mechanism the public landing-lead
endpoint uses for "New lead".

Everything here is best-effort by contract: these alerts ride customer-facing
actions (a customer just accepted an estimate from the emailed link) and a
failed badge write must never fail — or roll back — the action itself.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


def notify_office(
    db: Session,
    tenant_id: str,
    *,
    title: str,
    message: str,
    category: str = "system",
) -> None:
    """Insert one broadcast Notification row. Commits itself; never raises."""
    try:
        from gdx_dispatch.models.tenant_models import Notification  # noqa: PLC0415

        db.add(Notification(
            id=uuid.uuid4().hex,
            tenant_id=tenant_id,
            user_id=None,  # broadcast: every user on this tenant sees it
            title=title,
            message=message,
            category=category,
            is_read=0,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("office notification write failed: %s", title)


def notify_estimate_decision(
    db: Session,
    tenant_id: str,
    estimate,
    *,
    verb: str,
    tier_name: str | None = None,
    amount: float = 0.0,
    reason: str | None = None,
) -> None:
    """Bell alert for a customer's accept/decline of an estimate.

    Shared by the public /proposals/{token} page and the customer portal —
    the two self-service surfaces where a decision lands with no staff member
    in the loop to see it happen. `verb` is "accepted" or "declined" and is
    used verbatim in both title and message.
    """
    try:
        from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415

        who = "Customer"
        if getattr(estimate, "customer_id", None) is not None:
            name = db.execute(
                select(Customer.name).where(Customer.id == estimate.customer_id)
            ).scalar_one_or_none()
            who = (name or "").strip() or who

        number = getattr(estimate, "estimate_number", None) or "an estimate"
        message = f"{who} {verb} {number}"
        if tier_name:
            message += f" — {tier_name.capitalize()} package"
        if amount > 0:
            message += f" — ${amount:,.2f}"
        if reason:
            message += f' — "{reason}"'

        notify_office(
            db, tenant_id,
            title=f"Estimate {verb}",
            message=message,
            category="estimate",
        )
    except Exception:
        db.rollback()
        log.exception(
            "estimate decision notification failed estimate=%s",
            getattr(estimate, "id", None),
        )


# Stripe's method codes are not office English. "ach" on a bell row reads as
# jargon to whoever is at the desk; the books keep the code, the alert says
# what happened.
_METHOD_LABEL = {
    "card": "card",
    "ach": "bank transfer",
}


def notify_payment_received(
    db: Session,
    invoice,
    *,
    amount: float,
    method: str,
    overpaid: float = 0.0,
) -> None:
    """Bell alert for processor money landing on an invoice.

    Every Stripe surface is staff-absent by construction: the customer pays
    from the emailed pay page or the portal, and an ACH debit settles one to
    two business days later with nobody in the building. Until this ran, the
    office found out a customer had paid only by reopening the invoice — the
    payment wrote a `Payment` row, a ledger entry and an audit trail, and rang
    nothing. Same shape, and the same never-raises contract, as
    `notify_estimate_decision`: the money is already committed when this runs
    and a failed badge write must not be able to disturb it.

    **The tenant id is read HERE, not passed in.** The caller reaches this
    immediately after `db.commit()`, which expires the invoice, so every
    attribute read is a lazy refresh SELECT that can raise. An earlier draft
    took `tenant_id` as a parameter, which put `invoice.company_id` on the
    caller's line — outside this guard, in three call sites that do not wrap
    it (`/confirm`, the ACH charge, the webhook). A transient DB hiccup there
    would have 500'd a request whose money was already committed: the pay page
    telling a customer their successful card charge failed, and on the webhook
    a Stripe retry that takes the idempotent early return and loses the bell
    for good. Everything that can touch the database now lives inside the try.

    `overpaid` is passed as a plain float for the same reason — and because
    `balance_due` is clamped at zero, so an overcharge is invisible in the
    invoice's own columns.

    Deliberately NOT called from the office's own record-a-payment endpoint —
    the person who typed in a check does not need to be told about it.
    """
    try:
        from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415

        # `company_id` is the value the bell query filters `tenant_id` on
        # (leads.py states the same equality). Empty is not a tenant: writing
        # `tenant_id=""` would file the row where no bell query can find it —
        # a silent no-op wearing a success's clothes. Say so instead.
        tenant_id = str(getattr(invoice, "company_id", "") or "")
        if not tenant_id:
            log.error(
                "payment notification skipped — invoice %s has no company_id, so "
                "there is no tenant whose bell this would ring",
                getattr(invoice, "id", None),
            )
            return

        who = "Customer"
        customer_id = getattr(invoice, "customer_id", None)
        if customer_id is not None:
            name = db.execute(
                select(Customer.name).where(Customer.id == customer_id)
            ).scalar_one_or_none()
            who = (name or "").strip() or who

        number = getattr(invoice, "invoice_number", None) or "an invoice"
        label = _METHOD_LABEL.get((method or "").strip().lower(), "online")
        message = f"{who} paid ${float(amount or 0):,.2f} on {number} by {label}"

        # A partial payment is the case the office most needs to see, so say
        # what is left rather than letting "paid" imply settled in full.
        # Sub-cent residue is rounding, not a balance.
        balance = float(getattr(invoice, "balance_due", 0) or 0)
        if balance > 0.009:
            message += f" — ${balance:,.2f} still due"
        elif float(overpaid or 0) > 0.009:
            # M12's other half. A stale pay page can collect more than the
            # invoice still owes; `balance_due` clamps at zero, so without
            # this an overcharge rings exactly like a clean settlement and
            # the customer credit nobody has decided about goes unmentioned.
            message += f" — ${float(overpaid):,.2f} MORE than owed"

        notify_office(
            db, tenant_id,
            title="Payment received",
            message=message,
            category="payment",
        )
    except Exception:
        db.rollback()
        log.exception(
            "payment notification failed invoice=%s",
            getattr(invoice, "id", None),
        )
