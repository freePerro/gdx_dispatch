from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import resolve_audit_actor
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.modules.fleet.models import Vehicle, VehicleServiceRecord
from gdx_dispatch.modules.fleet.service import get_due_maintenance, log_service, update_odometer

# GET/POST /fleet/vehicles left this module 2026-09-06 (#569): routers/fleet.py registers
# them first, so FastAPI never dispatched here. The four routes below are unique to
# this module.
router = APIRouter(prefix="/api", tags=["fleet"], dependencies=[Depends(require_module("fleet")), Depends(get_current_user)])

class VehiclePatch(BaseModel): vin: str | None = None; make: str | None = None; model: str | None = None; year: int | None = None; license_plate: str | None = None; assigned_technician_id: str | None = None; status: str | None = None; odometer: int | None = None; last_service_odometer: int | None = None; last_service_at: datetime | None = None; service_interval_miles: int | None = None  # noqa: E701,E702
class ServiceIn(BaseModel): service_type: str; mileage: int; service_date: datetime; cost: float | None = None; notes: str | None = None  # noqa: E701,E702


@router.put("/fleet/vehicles/{vehicle_id}", response_model=None)
def put_vehicle(vehicle_id: UUID, payload: VehiclePatch, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Vehicle:
    if payload.odometer is not None: return update_odometer(vehicle_id, payload.odometer, db, actor=resolve_audit_actor(user))  # noqa: E701,E702
    row = db.execute(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None))).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Vehicle not found")  # noqa: E701,E702
    for k, v in payload.model_dump(exclude_unset=True).items(): setattr(row, k, v)  # noqa: E701,E702
    db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.get("/fleet/vehicles/{vehicle_id}/service-history", response_model=None)
def service_history(vehicle_id: UUID, db: Session = Depends(get_db)) -> list[VehicleServiceRecord]:
    return list(db.execute(select(VehicleServiceRecord).where(VehicleServiceRecord.vehicle_id == vehicle_id).order_by(VehicleServiceRecord.service_date.desc())).scalars().all())

@router.post("/fleet/vehicles/{vehicle_id}/service", response_model=None)
def create_service(vehicle_id: UUID, payload: ServiceIn, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> VehicleServiceRecord:
    return log_service(vehicle_id, payload.service_type, payload.mileage, payload.service_date, payload.cost, payload.notes, db, actor=resolve_audit_actor(user))

@router.get("/fleet/due-maintenance", response_model=None)
def due_maintenance(db: Session = Depends(get_db)) -> list[Vehicle]:
    return get_due_maintenance(db)
