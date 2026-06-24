from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.modules.fleet.models import Vehicle, VehicleServiceRecord
from gdx_dispatch.modules.fleet.service import get_due_maintenance, log_service, update_odometer

router = APIRouter(prefix="/api", tags=["fleet"], dependencies=[Depends(require_module("fleet")), Depends(get_current_user)])

class VehicleIn(BaseModel): vin: str | None = None; make: str; model: str; year: int; license_plate: str | None = None; assigned_technician_id: str | None = None; status: str = "available"; odometer: int = 0; last_service_odometer: int | None = None; last_service_at: datetime | None = None; service_interval_miles: int = 3000  # noqa: E701,E702
class VehiclePatch(BaseModel): vin: str | None = None; make: str | None = None; model: str | None = None; year: int | None = None; license_plate: str | None = None; assigned_technician_id: str | None = None; status: str | None = None; odometer: int | None = None; last_service_odometer: int | None = None; last_service_at: datetime | None = None; service_interval_miles: int | None = None  # noqa: E701,E702
class ServiceIn(BaseModel): service_type: str; mileage: int; service_date: datetime; cost: float | None = None; notes: str | None = None  # noqa: E701,E702

@router.get("/fleet/vehicles", response_model=None)
def list_vehicles(db: Session = Depends(get_db)) -> list[Vehicle]:
    return list(db.execute(select(Vehicle).where(Vehicle.deleted_at.is_(None)).order_by(Vehicle.created_at.desc())).scalars().all())

@router.post("/fleet/vehicles", response_model=None)
def create_vehicle(payload: VehicleIn, db: Session = Depends(get_db)) -> Vehicle:
    row = Vehicle(**payload.model_dump()); db.add(row); db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.put("/fleet/vehicles/{vehicle_id}", response_model=None)
def put_vehicle(vehicle_id: UUID, payload: VehiclePatch, db: Session = Depends(get_db)) -> Vehicle:
    if payload.odometer is not None: return update_odometer(vehicle_id, payload.odometer, db)  # noqa: E701,E702
    row = db.execute(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None))).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Vehicle not found")  # noqa: E701,E702
    for k, v in payload.model_dump(exclude_unset=True).items(): setattr(row, k, v)  # noqa: E701,E702
    db.commit(); db.refresh(row); return row  # noqa: E701,E702

@router.get("/fleet/vehicles/{vehicle_id}/service-history", response_model=None)
def service_history(vehicle_id: UUID, db: Session = Depends(get_db)) -> list[VehicleServiceRecord]:
    return list(db.execute(select(VehicleServiceRecord).where(VehicleServiceRecord.vehicle_id == vehicle_id).order_by(VehicleServiceRecord.service_date.desc())).scalars().all())

@router.post("/fleet/vehicles/{vehicle_id}/service", response_model=None)
def create_service(vehicle_id: UUID, payload: ServiceIn, db: Session = Depends(get_db)) -> VehicleServiceRecord:
    return log_service(vehicle_id, payload.service_type, payload.mileage, payload.service_date, payload.cost, payload.notes, db)

@router.get("/fleet/due-maintenance", response_model=None)
def due_maintenance(db: Session = Depends(get_db)) -> list[Vehicle]:
    return get_due_maintenance(db)
