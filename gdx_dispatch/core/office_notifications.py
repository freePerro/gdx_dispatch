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
