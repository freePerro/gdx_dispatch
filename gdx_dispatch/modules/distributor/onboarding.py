from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow


class DealerInvitation(TenantBase):
    """Invitation token for onboarding a new dealer into a distributor's network."""

    __tablename__ = "dealer_invitations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    distributor_tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    invitee_email: Mapped[str] = mapped_column(String(200), nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending → accepted | expired | cancelled
    dealer_tenant_id: Mapped[str | None] = mapped_column(String(100))  # set on acceptance
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def create_dealer_invitation(
    distributor_tenant_id: str,
    invitee_email: str,
    db: Session,
    expires_hours: int = 72,
) -> DealerInvitation:
    """Create an invitation token for a prospective dealer."""
    token = secrets.token_urlsafe(48)
    inv = DealerInvitation(
        distributor_tenant_id=distributor_tenant_id,
        invitee_email=invitee_email,
        token=token,
        expires_at=datetime.now(UTC) + timedelta(hours=expires_hours),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def accept_dealer_invitation(token: str, dealer_tenant_id: str, db: Session) -> DealerInvitation:
    """Mark invitation as accepted and link the new dealer tenant."""
    now = datetime.now(UTC)
    inv = db.execute(
        select(DealerInvitation).where(DealerInvitation.token == token)
    ).scalar_one_or_none()
    if not inv:
        raise ValueError("Invitation not found")
    if inv.status != "pending":
        raise ValueError(f"Invitation already {inv.status}")
    exp = inv.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp <= now:
        inv.status = "expired"
        db.commit()
        raise ValueError("Invitation expired")
    inv.status = "accepted"
    inv.dealer_tenant_id = dealer_tenant_id
    inv.accepted_at = now
    db.commit()
    db.refresh(inv)
    return inv


def cancel_invitation(invitation_id: UUID, db: Session) -> DealerInvitation:
    inv = db.execute(
        select(DealerInvitation).where(DealerInvitation.id == invitation_id)
    ).scalar_one_or_none()
    if not inv:
        raise ValueError("Invitation not found")
    if inv.status != "pending":
        raise ValueError(f"Cannot cancel invitation with status: {inv.status}")
    inv.status = "cancelled"
    db.commit()
    db.refresh(inv)
    return inv


def list_pending_invitations(distributor_tenant_id: str, db: Session) -> list[DealerInvitation]:
    return list(
        db.execute(
            select(DealerInvitation).where(
                DealerInvitation.distributor_tenant_id == distributor_tenant_id,
                DealerInvitation.status == "pending",
            ).order_by(DealerInvitation.created_at.desc())
        ).scalars().all()
    )
