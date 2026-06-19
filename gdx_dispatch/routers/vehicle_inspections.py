"""Sprint 6 / S6-B1 — vehicle inspection + fuel log."""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import VehicleInspection
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["vehicle-inspections"])


VALID_TYPES = {"pre_trip", "post_trip", "fueling", "ad_hoc"}


class InspectionIn(BaseModel):
    vehicle_id: str | None = Field(default=None, max_length=64)
    vehicle_label: str | None = Field(default=None, max_length=200)
    inspection_type: str = Field(default="pre_trip")
    odometer: int | None = Field(default=None, ge=0, le=2_000_000)
    fuel_cost: Decimal | None = None
    fuel_gallons: Decimal | None = None
    photo_url: str | None = Field(default=None, max_length=2000)
    issues_found: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)
    inspection_at: datetime | None = None


class InspectionOut(BaseModel):
    id: str
    vehicle_id: str | None
    vehicle_label: str | None
    technician_id: str
    inspection_type: str
    odometer: int | None
    fuel_cost: float | None
    fuel_gallons: float | None
    photo_url: str | None
    issues_found: str | None
    notes: str | None
    inspection_at: str
    created_at: str


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _to_response(insp: VehicleInspection) -> InspectionOut:
    return InspectionOut(
        id=str(insp.id),
        vehicle_id=insp.vehicle_id,
        vehicle_label=insp.vehicle_label,
        technician_id=insp.technician_id,
        inspection_type=insp.inspection_type,
        odometer=insp.odometer,
        fuel_cost=float(insp.fuel_cost) if insp.fuel_cost is not None else None,
        fuel_gallons=float(insp.fuel_gallons) if insp.fuel_gallons is not None else None,
        photo_url=insp.photo_url,
        issues_found=insp.issues_found,
        notes=insp.notes,
        inspection_at=insp.inspection_at.isoformat() if insp.inspection_at else "",
        created_at=insp.created_at.isoformat() if insp.created_at else "",
    )


@router.get("/api/vehicle-inspections", response_model=list[InspectionOut])
def list_inspections(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
    technician_id: str | None = None,
    vehicle_id: str | None = None,
    limit: int = 100,
) -> list[InspectionOut]:
    _ = current_user
    _ = request
    limit = max(1, min(int(limit), 500))
    try:
        q = select(VehicleInspection).where(VehicleInspection.deleted_at.is_(None))
        if technician_id:
            q = q.where(VehicleInspection.technician_id == technician_id)
        if vehicle_id:
            q = q.where(VehicleInspection.vehicle_id == vehicle_id)
        q = q.order_by(VehicleInspection.inspection_at.desc()).limit(limit)
        rows = db.execute(q).scalars().all()
        return [_to_response(r) for r in rows]
    except SQLAlchemyError:
        log.exception("inspections_list_failed")
        raise HTTPException(status_code=500, detail="Failed to list inspections") from None


@router.post("/api/vehicle-inspections", response_model=InspectionOut, status_code=201)
def create_inspection(
    payload: InspectionIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InspectionOut:
    _ = request
    if payload.inspection_type not in VALID_TYPES:
        raise HTTPException(
            status_code=422, detail=f"inspection_type must be one of {sorted(VALID_TYPES)}"
        )
    try:
        insp = VehicleInspection(
            id=uuid4(),
            vehicle_id=payload.vehicle_id,
            vehicle_label=payload.vehicle_label,
            technician_id=_user_id(current_user),
            inspection_type=payload.inspection_type,
            odometer=payload.odometer,
            fuel_cost=payload.fuel_cost,
            fuel_gallons=payload.fuel_gallons,
            photo_url=payload.photo_url,
            issues_found=payload.issues_found,
            notes=payload.notes,
            inspection_at=payload.inspection_at or datetime.now(UTC),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(insp)
        db.commit()
        db.refresh(insp)
        return _to_response(insp)
    except SQLAlchemyError:
        db.rollback()
        log.exception("inspection_create_failed")
        raise HTTPException(status_code=500, detail="Failed to save inspection") from None


@router.delete("/api/vehicle-inspections/{inspection_id}")
def delete_inspection(
    inspection_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _ = current_user
    _ = request
    try:
        insp_uuid = _uuid.UUID(inspection_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Inspection not found") from None
    try:
        insp = db.execute(
            select(VehicleInspection).where(
                VehicleInspection.id == insp_uuid,
                VehicleInspection.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not insp:
            raise HTTPException(status_code=404, detail="Inspection not found")
        insp.deleted_at = datetime.now(UTC)
        db.commit()
        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("inspection_delete_failed")
        raise HTTPException(status_code=500, detail="Failed to delete inspection") from None
