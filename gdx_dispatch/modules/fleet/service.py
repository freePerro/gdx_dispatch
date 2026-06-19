from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event, utcnow
from gdx_dispatch.modules.fleet.models import Vehicle, VehicleServiceRecord


def log_service(vehicle_id: UUID, service_type: str, mileage: int, service_date: datetime, cost: float | None, notes: str | None, db: Session) -> VehicleServiceRecord:
    vehicle = db.execute(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None))).scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    row = VehicleServiceRecord(vehicle_id=vehicle_id, service_type=service_type, mileage_at_service=mileage, service_date=service_date, cost=cost, notes=notes)
    vehicle.last_service_odometer, vehicle.last_service_at = mileage, service_date
    db.add(row); db.flush(); asyncio.run(log_audit_event(db, "vehicle_service_logged", "system", "vehicle", str(vehicle_id), {"service_type": service_type, "mileage": mileage})); db.commit(); db.refresh(row)  # noqa: E701,E702
    return row


def get_due_maintenance(db: Session) -> list[Vehicle]:
    cutoff = utcnow() - timedelta(days=90)
    q = select(Vehicle).where(
        Vehicle.deleted_at.is_(None),
        Vehicle.status != "retired",
        or_(Vehicle.odometer - func.coalesce(Vehicle.last_service_odometer, 0) >= Vehicle.service_interval_miles, Vehicle.last_service_at < cutoff),
    )
    return list(db.execute(q.order_by(Vehicle.created_at.desc())).scalars().all())


def update_odometer(vehicle_id: UUID, new_odometer: int, db: Session) -> Vehicle:
    vehicle = db.execute(select(Vehicle).where(Vehicle.id == vehicle_id, Vehicle.deleted_at.is_(None))).scalar_one_or_none()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle.odometer = new_odometer
    asyncio.run(log_audit_event(db, "odometer_updated", "system", "vehicle", str(vehicle_id), {"odometer": new_odometer})); db.commit(); db.refresh(vehicle)  # noqa: E701,E702
    return vehicle
