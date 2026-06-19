"""Outbound DID resolver — UX audit F-55 / 2026-04-29.

Picks the right phone.com number to send an SMS / place an outbound
call from, given the tenant's strategy setting. Priority chain:

  1. conversation_sticky   →  if a prior outbound message in this
                              thread used a particular from_number,
                              keep using it. New customers see ONE
                              number per conversation, not a roulette.
  2. tech_override         →  the sending tech's PhoneComExtension
                              .preferred_outbound_did (if set).
  3. tenant_default        →  AppSettings.phone_com_default_caller_id
                              — existing behavior.

Per Doug 2026-04-29: "d with tenant options to set it. Some tenants
might have 5 or more numbers so they can track advertising so the
inbound called number is important also."

The strategy enum toggles WHICH layers fire. Inbound attribution
(which DID a customer dialed) lives on PhoneComCall.to_number /
PhoneComMessage.to_number — already captured by the webhook layer.
The new bit is `PhoneComNumber.campaign_tag` for human-readable
labels in the marketing-attribution dashboard.
"""
from __future__ import annotations

import logging
from typing import Iterable
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.phone_com.models import PhoneComExtension, PhoneComMessage

log = logging.getLogger(__name__)


_VALID_STRATEGIES = {"tenant_default", "tech_override", "conversation_sticky", "priority_chain"}


def _strategy_layers(strategy: str | None) -> tuple[bool, bool, bool]:
    """Returns (use_sticky, use_tech, use_default)."""
    s = (strategy or "tenant_default").strip().lower()
    if s == "tech_override":
        return False, True, True
    if s == "conversation_sticky":
        return True, False, True
    if s == "priority_chain":
        return True, True, True
    # tenant_default + anything unrecognized
    return False, False, True


def _last_outbound_did_for_thread(
    db: Session, *, customer_id: UUID | str | None, to_number: str | None
) -> str | None:
    """The most recent from_number we used to message this customer/thread."""
    if not (customer_id or to_number):
        return None
    q = select(PhoneComMessage.from_number).where(
        PhoneComMessage.direction == "out",
        PhoneComMessage.from_number.is_not(None),
    )
    if customer_id:
        q = q.where(PhoneComMessage.customer_id == customer_id)
    elif to_number:
        q = q.where(PhoneComMessage.to_number == to_number)
    q = q.order_by(desc(PhoneComMessage.sent_at)).limit(1)
    row = db.execute(q).first()
    return row[0] if row else None


def _tech_preferred_did(db: Session, user_id: UUID | str | None) -> str | None:
    if not user_id:
        return None
    row = db.execute(
        select(PhoneComExtension.preferred_outbound_did).where(
            PhoneComExtension.user_id == user_id,
            PhoneComExtension.preferred_outbound_did.is_not(None),
        ).limit(1)
    ).first()
    return row[0] if row else None


def resolve_outbound_did(
    tenant_db: Session,
    *,
    customer_id: UUID | str | None = None,
    to_number: str | None = None,
    sending_user_id: UUID | str | None = None,
) -> str | None:
    """Pick the right outbound from-number per the tenant's strategy."""
    app = tenant_db.query(AppSettings).first()
    strategy = (app.phone_com_outbound_strategy if app else None) or "tenant_default"
    use_sticky, use_tech, use_default = _strategy_layers(strategy)

    if use_sticky:
        sticky = _last_outbound_did_for_thread(
            tenant_db, customer_id=customer_id, to_number=to_number
        )
        if sticky:
            return sticky

    if use_tech:
        tech_did = _tech_preferred_did(tenant_db, sending_user_id)
        if tech_did:
            return tech_did

    if use_default and app:
        return app.phone_com_default_caller_id

    return None


def candidate_strategies() -> Iterable[str]:
    """Stable list for UI dropdowns / Pydantic Literals."""
    return tuple(sorted(_VALID_STRATEGIES))
