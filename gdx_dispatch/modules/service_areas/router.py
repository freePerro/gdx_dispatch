from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.modules.service_areas.models import ServiceArea, ServiceAreaTechnician
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    prefix="/api",
    tags=["service_areas"],
    dependencies=[Depends(require_module("service_areas"))],
)


# --- Pydantic schemas ---

class ServiceAreaIn(BaseModel):
    name: str
    zip_codes: list[str] = []
    radius_miles: float | None = None
    center_lat: float | None = None
    center_lng: float | None = None


class ServiceAreaPatch(BaseModel):
    name: str | None = None
    zip_codes: list[str] | None = None
    radius_miles: float | None = None
    center_lat: float | None = None
    center_lng: float | None = None
    is_active: bool | None = None


class CoverageCheckIn(BaseModel):
    zip_code: str


class AssignTechIn(BaseModel):
    technician_id: UUID


# --- Helper ---

def _get_area_or_404(area_id: UUID, db: Session) -> ServiceArea:
    area = db.execute(
        select(ServiceArea).where(
            ServiceArea.id == area_id,
            ServiceArea.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if not area:
        raise HTTPException(status_code=404, detail="Service area not found")
    return area


# --- Routes ---

@router.get("/service-areas", response_model=None)
def list_service_areas(
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    areas = list(
        db.execute(
            select(ServiceArea)
            .where(ServiceArea.is_active.is_(True))
            .order_by(ServiceArea.created_at.desc())
        ).scalars().all()
    )
    result = []
    for area in areas:
        result.append({
            "id": area.id,
            "tenant_id": area.tenant_id,
            "name": area.name,
            "zip_codes": area.zip_codes or [],
            "zip_code_count": len(area.zip_codes or []),
            "radius_miles": area.radius_miles,
            "center_lat": area.center_lat,
            "center_lng": area.center_lng,
            "is_active": area.is_active,
            "created_at": area.created_at,
        })
    return result


@router.post("/service-areas", response_model=None)
def create_service_area(
    payload: ServiceAreaIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceArea:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    area = ServiceArea(**payload.model_dump())
    db.add(area)
    db.commit()
    db.refresh(area)
    return area


@router.get("/service-areas/check-coverage", response_model=None)
def check_coverage_get(
    zip_code: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """GET convenience endpoint — accepts zip_code as query param."""
    areas = list(
        db.execute(
            select(ServiceArea).where(ServiceArea.is_active.is_(True))
        ).scalars().all()
    )
    for area in areas:
        zips = area.zip_codes or []
        if zip_code in zips:
            return {
                "covered": True,
                "service_area": {
                    "id": area.id,
                    "name": area.name,
                    "zip_codes": zips,
                    "radius_miles": area.radius_miles,
                    "center_lat": area.center_lat,
                    "center_lng": area.center_lng,
                },
            }
    return {"covered": False, "service_area": None}


@router.post("/service-areas/check-coverage", response_model=None)
def check_coverage(
    payload: CoverageCheckIn,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """POST endpoint — body: {zip_code} → returns matching service area or null."""
    areas = list(
        db.execute(
            select(ServiceArea).where(ServiceArea.is_active.is_(True))
        ).scalars().all()
    )
    for area in areas:
        zips = area.zip_codes or []
        if payload.zip_code in zips:
            return {
                "covered": True,
                "service_area": {
                    "id": area.id,
                    "name": area.name,
                    "zip_codes": zips,
                    "radius_miles": area.radius_miles,
                    "center_lat": area.center_lat,
                    "center_lng": area.center_lng,
                },
            }
    return {"covered": False, "service_area": None}


@router.get("/service-areas/{area_id}", response_model=None)
def get_service_area(
    area_id: UUID,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    area = _get_area_or_404(area_id, db)
    technicians = list(
        db.execute(
            select(ServiceAreaTechnician).where(
                ServiceAreaTechnician.service_area_id == area_id
            )
        ).scalars().all()
    )
    return {
        "id": area.id,
        "tenant_id": area.tenant_id,
        "name": area.name,
        "zip_codes": area.zip_codes or [],
        "zip_code_count": len(area.zip_codes or []),
        "radius_miles": area.radius_miles,
        "center_lat": area.center_lat,
        "center_lng": area.center_lng,
        "is_active": area.is_active,
        "created_at": area.created_at,
        "technicians": [
            {
                "id": t.id,
                "technician_id": t.technician_id,
                "assigned_at": t.assigned_at,
            }
            for t in technicians
        ],
    }


@router.patch("/service-areas/{area_id}", response_model=None)
def update_service_area(
    area_id: UUID,
    payload: ServiceAreaPatch,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceArea:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    area = _get_area_or_404(area_id, db)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(area, k, v)
    db.commit()
    db.refresh(area)
    return area


@router.delete("/service-areas/{area_id}", response_model=None)
def deactivate_service_area(
    area_id: UUID,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    area = _get_area_or_404(area_id, db)
    area.is_active = False
    db.commit()
    return {"deactivated": True, "id": area.id}


@router.get("/service-areas/{area_id}/technicians", response_model=None)
def list_area_technicians(
    area_id: UUID,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    _get_area_or_404(area_id, db)
    technicians = list(
        db.execute(
            select(ServiceAreaTechnician).where(
                ServiceAreaTechnician.service_area_id == area_id
            ).order_by(ServiceAreaTechnician.assigned_at.asc())
        ).scalars().all()
    )
    return [
        {
            "id": t.id,
            "service_area_id": t.service_area_id,
            "technician_id": t.technician_id,
            "assigned_at": t.assigned_at,
        }
        for t in technicians
    ]


@router.post("/service-areas/{area_id}/technicians", response_model=None)
def assign_technician(
    area_id: UUID,
    payload: AssignTechIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ServiceAreaTechnician:
    if user.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    _get_area_or_404(area_id, db)
    # Prevent duplicate assignment
    existing = db.execute(
        select(ServiceAreaTechnician).where(
            ServiceAreaTechnician.service_area_id == area_id,
            ServiceAreaTechnician.technician_id == payload.technician_id,
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Technician already assigned to this area")
    assignment = ServiceAreaTechnician(
        service_area_id=area_id,
        technician_id=payload.technician_id,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    return assignment
