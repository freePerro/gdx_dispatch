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
from gdx_dispatch.modules.fleet.models import Vehicle
from gdx_dispatch.modules.fleet.service import update_odometer

# GET/POST /fleet/vehicles left this module 2026-09-06 (#569): routers/fleet.py registers
# them first, so FastAPI never dispatched here.
# Its maintenance copy left 2026-09-10 (#637): GET /fleet/vehicles/{id}/service-history,
# POST /fleet/vehicles/{id}/service and GET /fleet/due-maintenance read `vehicles`, a
# table nothing writes. The Fleet page's vehicles live in `fleet_vehicles_router`, so every
# id it holds 404'd here. The kept copy is routers/fleet.py (service-log, due-for-service).
router = APIRouter(prefix="/api", tags=["fleet"], dependencies=[Depends(require_module("fleet")), Depends(get_current_user)])

class VehiclePatch(BaseModel): vin: str | None = None; make: str | None = None; model: str | None = None; year: int | None = None; license_plate: str | None = None; assigned_technician_id: str | None = None; status: str | None = None; odometer: int | None = None; last_service_odometer: int | None = None; last_service_at: datetime | None = None; service_interval_miles: int | None = None  # noqa: E701,E702


@router.put("/fleet/vehicles/{vehicle_id}", response_model=None)
def put_vehicle(vehicle_id: UUID, payload: VehiclePatch, user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Vehicle:
    if payload.odometer is not None: return update_odometer(vehicle_id, payload.odometer, db, actor=resolve_audit_actor(user))  # noqa: E701,E702
    row = db.execute(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None))).scalar_one_or_none()
    if not row: raise HTTPException(status_code=404, detail="Vehicle not found")  # noqa: E701,E702
    for k, v in payload.model_dump(exclude_unset=True).items(): setattr(row, k, v)  # noqa: E701,E702
    db.commit(); db.refresh(row); return row  # noqa: E701,E702
