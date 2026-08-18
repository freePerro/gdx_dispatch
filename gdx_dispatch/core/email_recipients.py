"""Recipient resolution — email goes to a PERSON, not just an account.

One decision, made in one place: (address, To-header name, greeting name,
provenance). Before this, every send path greeted `Customer.name` and mailed
`Customer.email` — correct for a house, wrong for a business ("Dear <Company
Name>"), while the `customer_contacts` table (the people at an account) sat
unread by all email code.

Rules, in order:
1. An explicitly chosen contact (composer picker) wins — but only if it is
   live, belongs to this customer, and has an email address; a bad pick
   falls through rather than erroring, because a send the operator watched
   succeed matters more than a stale contact id.
2. Otherwise the customer's primary contact (is_primary, live, has email) —
   this is how automated paths (bulk, reminders, receipts, rules, plugins)
   greet the person on a business account.
3. Otherwise the account email + account name (a residential customer's
   name IS the person; nothing regresses).

`source` in the result feeds outbound_emails.recipient_source, so the audit
trail records not just where the email went but WHY that address.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRecipient:
    email: str
    to_name: str
    greeting_name: str
    source: str  # 'contact' | 'primary_contact' | 'account_email' | 'none'
    contact_id: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.email)


def _first_name(full_name: str) -> str:
    """Greeting uses the person's first name — 'Hi Bob,' not 'Hi Bob Smith,'.
    A single-token name passes through unchanged."""
    parts = (full_name or "").strip().split()
    return parts[0] if parts else ""


def resolve_recipient(
    db: Session,
    customer,
    contact_id: str | None = None,
) -> ResolvedRecipient:
    """Resolve who an email about `customer` actually goes to.

    Never raises — a resolution problem degrades toward the account email,
    and a customer with no email at all returns .ok == False for the caller
    to surface as a skip_reason.
    """
    from gdx_dispatch.models.tenant_models import CustomerContact

    account_email = (getattr(customer, "email", "") or "").strip()
    account_name = (getattr(customer, "name", "") or "").strip()

    def _contact_result(contact, source: str) -> ResolvedRecipient:
        name = (contact.name or "").strip()
        return ResolvedRecipient(
            email=(contact.email or "").strip(),
            to_name=name or account_name,
            greeting_name=_first_name(name) or name or account_name,
            source=source,
            contact_id=str(contact.id),
        )

    try:
        if contact_id:
            contact = db.execute(
                select(CustomerContact).where(
                    CustomerContact.id == str(contact_id),
                    CustomerContact.customer_id == customer.id,
                    CustomerContact.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if contact and (contact.email or "").strip():
                return _contact_result(contact, "contact")
            log.info(
                "recipient_contact_fallback contact=%s customer=%s (missing/stale/no-email)",
                contact_id, getattr(customer, "id", None),
            )

        primary = db.execute(
            select(CustomerContact).where(
                CustomerContact.customer_id == customer.id,
                CustomerContact.is_primary.is_(True),
                CustomerContact.deleted_at.is_(None),
            ).order_by(CustomerContact.created_at)
        ).scalars().first()
        if primary and (primary.email or "").strip():
            return _contact_result(primary, "primary_contact")
    except Exception:
        # Resolution must never block a send; fall through to the account.
        log.exception("recipient_resolution_failed customer=%s", getattr(customer, "id", None))

    if account_email:
        return ResolvedRecipient(
            email=account_email,
            to_name=account_name,
            greeting_name=account_name or "Valued Customer",
            source="account_email",
        )
    return ResolvedRecipient(email="", to_name="", greeting_name="", source="none")
