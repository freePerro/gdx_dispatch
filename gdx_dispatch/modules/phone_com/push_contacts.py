"""P2.8 — push GDX customers to Phone.com as contacts.

One-way sync. The win: when a customer calls in, the desk phone /
mobile app sees the customer's name and company instead of just a
number. Saves the operator a lookup.

Strategy:
- For each Customer with a phone, find or create a row in
  ``phone_com_contact_push`` (per (customer_id, e164)).
- If ``phone_com_contact_id`` is null, POST a new contact and store
  the returned id. If non-null and the customer's display name has
  changed, PATCH the contact (TODO — Phase 2 follow-up; the create
  endpoint is the high-value path).
- Cap the per-tenant work at ``cap`` customers per run so a tenant
  with 50K customers doesn't lock the worker for hours.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient
from gdx_dispatch.modules.phone_com.customer_resolver import normalize_e164
from gdx_dispatch.modules.phone_com.models import PhoneComContactPush

log = logging.getLogger("gdx_dispatch.modules.phone_com.push_contacts")


def _display_name(customer: Any) -> str:
    """Customer name fields can be split or combined depending on tenant —
    tolerate both. Phone.com's contact UI shows ``first_name + last_name``
    or falls back to ``company`` when both are blank."""
    first = (getattr(customer, "first_name", None) or "").strip()
    last = (getattr(customer, "last_name", None) or "").strip()
    if first or last:
        return (first + " " + last).strip()
    name = (getattr(customer, "name", None) or "").strip()
    return name


def _split_name(display: str) -> tuple[str, str]:
    parts = (display or "").split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return display.strip(), ""


def push_contacts_for_tenant(
    tenant_db: Session,
    client: PhoneComClient,
    *,
    cap: int = 200,
) -> dict[str, Any]:
    """Push pending customers for one tenant. Idempotent."""
    from gdx_dispatch.models.tenant_models import Customer

    pushed = 0
    skipped = 0
    failed = 0

    customers = (
        tenant_db.query(Customer)
        .filter(Customer.phone.isnot(None))
        .order_by(Customer.created_at.desc())
        .limit(cap * 4)  # over-fetch since most rows already pushed
        .all()
    )

    for cust in customers:
        if pushed >= cap:
            break
        e164 = normalize_e164(cust.phone or "")
        if not e164:
            skipped += 1
            continue

        existing = (
            tenant_db.query(PhoneComContactPush)
            .filter(
                PhoneComContactPush.customer_id == cust.id,
                PhoneComContactPush.phone_e164 == e164,
            )
            .first()
        )
        display = _display_name(cust) or e164
        # Skip when we've already pushed this exact (customer, phone, name).
        if existing is not None and existing.phone_com_contact_id and existing.name_pushed == display:
            skipped += 1
            continue

        first, last = _split_name(display)
        try:
            res = client.create_contact(
                first_name=first or None,
                last_name=last or None,
                company=getattr(cust, "company", None),
                phone_numbers=[{"number": e164, "type": "mobile"}],
                external_id=str(cust.id),
            )
        except PhoneComAPIError as exc:
            failed += 1
            log.warning(
                "phone_com.push_contacts create_contact failed customer=%s err=%s",
                cust.id, exc,
            )
            if existing is None:
                existing = PhoneComContactPush(
                    customer_id=cust.id, phone_e164=e164,
                )
                tenant_db.add(existing)
            existing.last_pushed_at = datetime.now(timezone.utc)
            existing.last_error = str(exc)[:500]
            tenant_db.commit()
            continue

        if existing is None:
            existing = PhoneComContactPush(
                customer_id=cust.id, phone_e164=e164,
            )
            tenant_db.add(existing)
        existing.phone_com_contact_id = str(res.get("id") or "") or existing.phone_com_contact_id
        existing.name_pushed = display
        existing.last_pushed_at = datetime.now(timezone.utc)
        existing.last_error = None
        tenant_db.commit()
        pushed += 1

    return {
        "ok": True,
        "pushed": pushed,
        "skipped": skipped,
        "failed": failed,
    }
