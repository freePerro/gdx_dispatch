from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.modules.locations.models import Location, LocationTechnician
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(prefix="/api/locations", tags=["locations"])

# ---------------------------------------------------------------------------
# Auth dependency alias: "get_current_tenant_user" resolves to get_current_user
# ---------------------------------------------------------------------------

get_current_tenant_user = get_current_user


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class LocationIn(BaseModel):
    name: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    is_primary: bool = False


class LocationPatch(BaseModel):
    name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    phone: str | None = None
    is_primary: bool | None = None
    is_active: bool | None = None


class TechnicianAssign(BaseModel):
    technician_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin(user: dict) -> None:
    if user.get("role") not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Admin or owner role required")


def _get_location_or_404(location_id: UUID, tenant_id: str, db: Session) -> Location:
    loc = db.execute(
        select(Location).where(
            Location.id == location_id,
            Location.tenant_id == tenant_id,
        )
    ).scalar_one_or_none()
    if loc is None:
        raise HTTPException(status_code=404, detail="Location not found")
    return loc


def _location_dict(loc: Location) -> dict:
    return {
        "id": str(loc.id),
        "tenant_id": loc.tenant_id,
        "name": loc.name,
        "address": loc.address,
        "city": loc.city,
        "state": loc.state,
        "zip": loc.zip,
        "phone": loc.phone,
        "is_primary": loc.is_primary,
        "is_active": loc.is_active,
        "created_at": loc.created_at.isoformat() if loc.created_at else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/", response_model=None)
def list_locations(
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all active locations for the current tenant."""
    tenant_id = user["tenant_id"]
    locations = db.execute(
        select(Location).where(
            Location.tenant_id == tenant_id,
            Location.is_active.is_(True),
        ).order_by(Location.created_at)
    ).scalars().all()
    return [_location_dict(loc) for loc in locations]


@router.post("/", response_model=None, status_code=201)
def create_location(
    payload: LocationIn,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a new location for the tenant. Admin/owner only."""
    _require_admin(user)
    tenant_id = user["tenant_id"]

    # If the new location should be primary, demote all existing ones first
    if payload.is_primary:
        db.execute(
            update(Location)
            .where(Location.tenant_id == tenant_id, Location.is_active.is_(True))
            .values(is_primary=False)
        )

    # First location for this tenant is automatically primary
    existing_count = db.execute(
        select(Location).where(
            Location.tenant_id == tenant_id,
            Location.is_active.is_(True),
        )
    ).scalars().all()
    is_primary = payload.is_primary or len(existing_count) == 0

    if is_primary and not payload.is_primary:
        # Auto-primary (first location) — no need to demote again
        pass

    loc = Location(
        tenant_id=tenant_id,
        name=payload.name,
        address=payload.address,
        city=payload.city,
        state=payload.state,
        zip=payload.zip,
        phone=payload.phone,
        is_primary=is_primary,
        is_active=True,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return _location_dict(loc)


@router.get("/{location_id}", response_model=None)
def get_location(
    location_id: UUID,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> dict:
    """Get a single location by ID."""
    loc = _get_location_or_404(location_id, user["tenant_id"], db)
    return _location_dict(loc)


@router.patch("/{location_id}", response_model=None)
def update_location(
    location_id: UUID,
    payload: LocationPatch,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> dict:
    """Update location fields. Admin/owner only."""
    _require_admin(user)
    tenant_id = user["tenant_id"]
    loc = _get_location_or_404(location_id, tenant_id, db)

    # If promoting to primary, demote all other locations
    if payload.is_primary is True:
        db.execute(
            update(Location)
            .where(
                Location.tenant_id == tenant_id,
                Location.is_active.is_(True),
                Location.id != loc.id,
            )
            .values(is_primary=False)
        )

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(loc, field, value)

    db.commit()
    db.refresh(loc)
    return _location_dict(loc)


@router.delete("/{location_id}", response_model=None)
def deactivate_location(
    location_id: UUID,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> dict:
    """Deactivate (soft-delete) a location. Cannot deactivate the primary location."""
    _require_admin(user)
    loc = _get_location_or_404(location_id, user["tenant_id"], db)

    if loc.is_primary:
        raise HTTPException(status_code=400, detail="Cannot deactivate the primary location")

    loc.is_active = False
    db.commit()
    return {"id": str(location_id), "deactivated": True}


@router.get("/{location_id}/technicians", response_model=None)
def list_location_technicians(
    location_id: UUID,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List all technicians assigned to a location."""
    _get_location_or_404(location_id, user["tenant_id"], db)
    assignments = db.execute(
        select(LocationTechnician).where(LocationTechnician.location_id == location_id)
    ).scalars().all()
    return [
        {"location_id": str(a.location_id), "technician_id": a.technician_id}
        for a in assignments
    ]


@router.post("/{location_id}/technicians", response_model=None, status_code=201)
def assign_technician(
    location_id: UUID,
    payload: TechnicianAssign,
    user: dict = Depends(get_current_tenant_user),
    db: Session = Depends(get_db),
) -> dict:
    """Assign a technician to a location. Admin/owner only. 409 if already assigned."""
    _require_admin(user)
    _get_location_or_404(location_id, user["tenant_id"], db)

    existing = db.execute(
        select(LocationTechnician).where(
            LocationTechnician.location_id == location_id,
            LocationTechnician.technician_id == payload.technician_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Technician already assigned to this location")

    assignment = LocationTechnician(
        location_id=location_id,
        technician_id=payload.technician_id,
    )
    db.add(assignment)
    db.commit()
    return {
        "location_id": str(location_id),
        "technician_id": payload.technician_id,
    }
