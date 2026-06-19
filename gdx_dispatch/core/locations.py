from __future__ import annotations

import contextlib

# Multi-location support for tenants with multiple service offices.
# Generated via local LLM (qwen2.5-coder:14b) — reviewed and applied by Claude.
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.types import Uuid

from gdx_dispatch.core.audit import TenantBase, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class ServiceLocation(TenantBase):
    __tablename__ = "service_locations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(50), nullable=True)
    zip: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str] = mapped_column(String(60), nullable=False, default="America/Chicago")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    manager_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserLocation(TenantBase):
    __tablename__ = "user_locations"
    __table_args__ = (UniqueConstraint("user_id", "location_id", name="uq_user_locations_user_location"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    location_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("service_locations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    can_dispatch_for: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class LocationCreate(BaseModel):
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    email: str | None = None
    timezone: str = "America/Chicago"
    is_primary: bool = False
    manager_user_id: str | None = None


class LocationUpdate(BaseModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    email: str | None = None
    timezone: str | None = None
    is_primary: bool | None = None
    is_active: bool | None = None
    manager_user_id: str | None = None


class UserLocationAssign(BaseModel):
    user_id: str
    can_dispatch_for: bool = True


# ---------------------------------------------------------------------------
# Location filter dependency
# ---------------------------------------------------------------------------

def get_location_filter(
    request: Request,
    location_id: str | None = Query(None),
) -> str | None:
    """Extract location_id from X-Location-Id header or ?location_id= query param."""
    return request.headers.get("X-Location-Id") or location_id


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/locations", tags=["locations"])


def _require_admin(user: dict) -> None:
    if user.get("role") not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Insufficient role")


def _get_location_or_404(location_id: str, db: Session) -> ServiceLocation:
    try:
        lid = UUID(location_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Location not found") from None
    loc = db.execute(
        select(ServiceLocation).where(
            ServiceLocation.id == lid,
            ServiceLocation.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if loc is None:
        raise HTTPException(status_code=404, detail="Location not found")
    return loc


@router.get("/")
def list_locations(
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all active (non-deleted) locations. Admins/owners see all; users see only assigned."""
    try:
        base_q = select(ServiceLocation).where(ServiceLocation.deleted_at.is_(None))

        if user.get("role") in ("owner", "admin"):
            locations = db.execute(base_q).scalars().all()
        else:
            assigned_ids = db.execute(
                select(UserLocation.location_id).where(UserLocation.user_id == user["sub"])
            ).scalars().all()
            locations = db.execute(
                base_q.where(ServiceLocation.id.in_(assigned_ids))
            ).scalars().all()

        result = []
        for loc in locations:
            count = db.execute(
                select(UserLocation).where(UserLocation.location_id == loc.id)
            ).scalars().all()
            result.append({
                "id": str(loc.id),
                "tenant_id": loc.tenant_id,
                "name": loc.name,
                "address": loc.address,
                "city": loc.city,
                "state": loc.state,
                "zip": loc.zip,
                "phone": loc.phone,
                "email": loc.email,
                "timezone": loc.timezone,
                "is_primary": loc.is_primary,
                "is_active": loc.is_active,
                "manager_user_id": loc.manager_user_id,
                "created_at": loc.created_at.isoformat() if loc.created_at else None,
                "active_user_count": len(count),
            })
        return result
    except Exception:
        import logging
        logging.getLogger(__name__).exception("list_locations: service_locations table may not exist")
        with contextlib.suppress(Exception):
            db.rollback()
        raise RuntimeError("Failed to list locations due to a database error") from None


@router.post("/", status_code=201)
def create_location(
    payload: LocationCreate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a new location. Admin/owner only. First location is automatically primary."""
    _require_admin(user)

    tenant_id = user.get("tenant_id", "")

    # Count existing non-deleted locations to determine if this should be primary
    existing_count = db.execute(
        select(ServiceLocation).where(
            ServiceLocation.tenant_id == tenant_id,
            ServiceLocation.deleted_at.is_(None),
        )
    ).scalars().all()

    is_primary = payload.is_primary or len(existing_count) == 0

    if is_primary:
        # Clear primary flag from all other locations
        db.execute(
            update(ServiceLocation)
            .where(
                ServiceLocation.tenant_id == tenant_id,
                ServiceLocation.deleted_at.is_(None),
            )
            .values(is_primary=False)
        )

    loc = ServiceLocation(
        tenant_id=tenant_id,
        name=payload.name,
        address=payload.address,
        city=payload.city,
        state=payload.state,
        zip=payload.zip,
        phone=payload.phone,
        email=payload.email,
        timezone=payload.timezone,
        is_primary=is_primary,
        is_active=True,
        manager_user_id=payload.manager_user_id,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return {
        "id": str(loc.id),
        "tenant_id": loc.tenant_id,
        "name": loc.name,
        "address": loc.address,
        "city": loc.city,
        "state": loc.state,
        "zip": loc.zip,
        "phone": loc.phone,
        "email": loc.email,
        "timezone": loc.timezone,
        "is_primary": loc.is_primary,
        "is_active": loc.is_active,
        "manager_user_id": loc.manager_user_id,
        "created_at": loc.created_at.isoformat() if loc.created_at else None,
    }


@router.put("/{location_id}")
def update_location(
    location_id: str,
    payload: LocationUpdate,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Update a location. Admin/owner only."""
    _require_admin(user)
    loc = _get_location_or_404(location_id, db)

    if payload.is_primary is True:
        # Clear primary on all others in this tenant first
        tenant_id = user.get("tenant_id", "")
        db.execute(
            update(ServiceLocation)
            .where(
                ServiceLocation.tenant_id == tenant_id,
                ServiceLocation.deleted_at.is_(None),
                ServiceLocation.id != loc.id,
            )
            .values(is_primary=False)
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(loc, field, value)

    db.commit()
    db.refresh(loc)
    return {
        "id": str(loc.id),
        "name": loc.name,
        "address": loc.address,
        "city": loc.city,
        "state": loc.state,
        "zip": loc.zip,
        "phone": loc.phone,
        "email": loc.email,
        "timezone": loc.timezone,
        "is_primary": loc.is_primary,
        "is_active": loc.is_active,
        "manager_user_id": loc.manager_user_id,
    }


@router.delete("/{location_id}")
def delete_location(
    location_id: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Soft-delete a location. Cannot delete the primary location."""
    _require_admin(user)
    loc = _get_location_or_404(location_id, db)

    if loc.is_primary:
        raise HTTPException(status_code=400, detail="Cannot delete the primary location")

    loc.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


@router.post("/{location_id}/assign-user", status_code=201)
def assign_user_to_location(
    location_id: str,
    payload: UserLocationAssign,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Assign a user to a location. Admin/owner only. 409 if already assigned."""
    _require_admin(user)
    loc = _get_location_or_404(location_id, db)

    existing = db.execute(
        select(UserLocation).where(
            UserLocation.user_id == payload.user_id,
            UserLocation.location_id == loc.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="User already assigned to this location")

    assignment = UserLocation(
        user_id=payload.user_id,
        location_id=loc.id,
        can_dispatch_for=payload.can_dispatch_for,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return {
        "id": str(assignment.id),
        "user_id": assignment.user_id,
        "location_id": str(assignment.location_id),
        "can_dispatch_for": assignment.can_dispatch_for,
    }


@router.delete("/{location_id}/users/{user_id}")
def remove_user_from_location(
    location_id: str,
    user_id: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Remove a user's assignment from a location. Admin/owner only."""
    _require_admin(user)
    loc = _get_location_or_404(location_id, db)

    assignment = db.execute(
        select(UserLocation).where(
            UserLocation.user_id == user_id,
            UserLocation.location_id == loc.id,
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="User is not assigned to this location")

    db.delete(assignment)
    db.commit()
    return {"ok": True}


@router.get("/{location_id}/technicians")
def list_location_technicians(
    location_id: str,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all users assigned to a location."""
    loc = _get_location_or_404(location_id, db)

    assignments = db.execute(
        select(UserLocation).where(UserLocation.location_id == loc.id)
    ).scalars().all()

    return [
        {"user_id": a.user_id, "can_dispatch_for": a.can_dispatch_for}
        for a in assignments
    ]
