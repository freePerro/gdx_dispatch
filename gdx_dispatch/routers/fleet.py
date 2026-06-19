from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import FleetServiceLog, FleetVehicle
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["fleet-router"], dependencies=[Depends(require_module("fleet"))])


class VehicleCreateRequest(BaseModel):
    make: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    year: int = Field(ge=1900, le=2100)
    vin: str | None = Field(default=None, max_length=17)
    license_plate: str | None = Field(default=None, max_length=20)
    odometer: int = Field(default=0, ge=0, le=10_000_000)
    service_interval_miles: int = Field(default=3000, ge=0, le=1_000_000)
    next_service_due_on: date | None = None


class VehicleUpdateRequest(BaseModel):
    make: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1900, le=2100)
    vin: str | None = Field(default=None, max_length=17)
    license_plate: str | None = Field(default=None, max_length=20)
    odometer: int | None = Field(default=None, ge=0, le=10_000_000)
    service_interval_miles: int | None = Field(default=None, ge=0, le=1_000_000)
    next_service_due_on: date | None = None


class VehicleResponse(BaseModel):
    id: str
    make: str
    model: str
    year: int
    vin: str | None
    license_plate: str | None
    odometer: int
    last_service_odometer: int | None
    service_interval_miles: int
    next_service_due_on: str | None


class VehicleServiceCreateRequest(BaseModel):
    service_type: str = Field(min_length=1, max_length=100)
    mileage_at_service: int = Field(ge=0, le=10_000_000)
    service_date: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


class VehicleServiceResponse(BaseModel):
    id: str
    vehicle_id: str
    service_type: str
    mileage_at_service: int
    service_date: str
    notes: str | None


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _validate_uuid(value: str, entity: str = "Resource") -> None:
    try:
        _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None



def _to_response(v: FleetVehicle) -> VehicleResponse:
    return VehicleResponse(
        id=str(v.id),
        make=str(v.make),
        model=str(v.model),
        year=int(v.year),
        vin=v.vin,
        license_plate=v.license_plate,
        odometer=int(v.odometer),
        last_service_odometer=v.last_service_odometer,
        service_interval_miles=int(v.service_interval_miles),
        next_service_due_on=v.next_service_due_on,
    )


@router.get("/api/fleet/vehicles", response_model=list[VehicleResponse])
def list_vehicles(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VehicleResponse]:
    _ = current_user
    tenant_id = _tenant_id(request)
    try:

        vehicles = db.execute(
            select(FleetVehicle)
            .where(
                FleetVehicle.tenant_id == tenant_id,
                FleetVehicle.deleted_at.is_(None),
            )
            .order_by(FleetVehicle.created_at.desc())
        ).scalars().all()
        return [_to_response(v) for v in vehicles]
    except SQLAlchemyError:
        log.exception("fleet_vehicles_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list vehicles") from None


@router.post("/api/fleet/vehicles", response_model=VehicleResponse, status_code=201)
def create_vehicle(
    payload: VehicleCreateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleResponse:
    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    try:

        vehicle = FleetVehicle(
            id=row_id,
            tenant_id=tenant_id,
            make=payload.make,
            model=payload.model,
            year=payload.year,
            vin=payload.vin,
            license_plate=payload.license_plate,
            odometer=payload.odometer,
            last_service_odometer=None,
            service_interval_miles=payload.service_interval_miles,
            next_service_due_on=payload.next_service_due_on.isoformat() if payload.next_service_due_on else None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        db.add(vehicle)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="fleet_vehicle_created",
                entity_type="fleet_vehicle",
                entity_id=row_id,
                details=payload.model_dump(mode="json"),
                request=request,
            )
        )
        db.commit()

        return VehicleResponse(
            id=row_id,
            make=payload.make,
            model=payload.model,
            year=payload.year,
            vin=payload.vin,
            license_plate=payload.license_plate,
            odometer=payload.odometer,
            last_service_odometer=None,
            service_interval_miles=payload.service_interval_miles,
            next_service_due_on=payload.next_service_due_on.isoformat() if payload.next_service_due_on else None,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("fleet_vehicle_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to create vehicle") from None


@router.patch("/api/fleet/vehicles/{vehicle_id}", response_model=VehicleResponse)
def update_vehicle(
    vehicle_id: str,
    payload: VehicleUpdateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleResponse:
    _validate_uuid(vehicle_id, "Vehicle")
    tenant_id = _tenant_id(request)
    try:

        vehicle = db.execute(
            select(FleetVehicle).where(
                FleetVehicle.tenant_id == tenant_id,
                FleetVehicle.id == vehicle_id,
                FleetVehicle.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Vehicle not found")

        updates = payload.model_dump(exclude_unset=True, mode="json")
        for field, value in updates.items():
            setattr(vehicle, field, value)
        vehicle.updated_at = datetime.now(UTC).isoformat()

        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="fleet_vehicle_updated",
                entity_type="fleet_vehicle",
                entity_id=vehicle_id,
                details=updates,
                request=request,
            )
        )
        db.commit()

        return _to_response(vehicle)
    except SQLAlchemyError:
        db.rollback()
        log.exception("fleet_vehicle_update_failed", extra={"tenant_id": tenant_id, "vehicle_id": vehicle_id})
        raise HTTPException(status_code=500, detail="Failed to update vehicle") from None


@router.delete("/api/fleet/vehicles/{vehicle_id}")
def delete_vehicle(
    vehicle_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _validate_uuid(vehicle_id, "Vehicle")
    tenant_id = _tenant_id(request)
    deleted_at = datetime.now(UTC).isoformat()
    try:

        vehicle = db.execute(
            select(FleetVehicle).where(
                FleetVehicle.tenant_id == tenant_id,
                FleetVehicle.id == vehicle_id,
                FleetVehicle.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Vehicle not found")

        vehicle.deleted_at = deleted_at
        vehicle.updated_at = deleted_at
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="fleet_vehicle_deleted",
                entity_type="fleet_vehicle",
                entity_id=vehicle_id,
                details={"deleted_at": deleted_at},
                request=request,
            )
        )
        db.commit()

        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("fleet_vehicle_delete_failed", extra={"tenant_id": tenant_id, "vehicle_id": vehicle_id})
        raise HTTPException(status_code=500, detail="Failed to delete vehicle") from None


@router.get("/api/fleet/vehicles/{vehicle_id}/service-log", response_model=list[VehicleServiceResponse])
def list_vehicle_service_log(
    vehicle_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VehicleServiceResponse]:
    _ = current_user
    _validate_uuid(vehicle_id, "Vehicle")
    tenant_id = _tenant_id(request)
    try:

        logs = db.execute(
            select(FleetServiceLog)
            .where(
                FleetServiceLog.tenant_id == tenant_id,
                FleetServiceLog.vehicle_id == vehicle_id,
            )
            .order_by(FleetServiceLog.service_date.desc())
        ).scalars().all()
        return [
            VehicleServiceResponse(
                id=str(row.id),
                vehicle_id=str(row.vehicle_id),
                service_type=str(row.service_type),
                mileage_at_service=int(row.mileage_at_service),
                service_date=str(row.service_date),
                notes=row.notes,
            )
            for row in logs
        ]
    except SQLAlchemyError:
        log.exception("fleet_service_log_list_failed", extra={"tenant_id": tenant_id, "vehicle_id": vehicle_id})
        raise HTTPException(status_code=500, detail="Failed to list service log") from None


@router.post("/api/fleet/vehicles/{vehicle_id}/service-log", response_model=VehicleServiceResponse, status_code=201)
def create_vehicle_service_log(
    vehicle_id: str,
    payload: VehicleServiceCreateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VehicleServiceResponse:
    _validate_uuid(vehicle_id, "Vehicle")
    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    service_date = (payload.service_date or datetime.now(UTC)).isoformat()
    try:

        vehicle = db.execute(
            select(FleetVehicle).where(
                FleetVehicle.tenant_id == tenant_id,
                FleetVehicle.id == vehicle_id,
                FleetVehicle.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not vehicle:
            raise HTTPException(status_code=404, detail="Vehicle not found")

        svc_log = FleetServiceLog(
            id=row_id,
            tenant_id=tenant_id,
            vehicle_id=vehicle_id,
            service_type=payload.service_type,
            mileage_at_service=payload.mileage_at_service,
            service_date=service_date,
            notes=payload.notes,
        )
        db.add(svc_log)

        # Update vehicle odometer and last_service_odometer
        vehicle.last_service_odometer = payload.mileage_at_service
        if vehicle.odometer < payload.mileage_at_service:
            vehicle.odometer = payload.mileage_at_service
        vehicle.updated_at = datetime.now(UTC).isoformat()

        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="fleet_service_log_created",
                entity_type="fleet_service_log",
                entity_id=row_id,
                details=payload.model_dump(mode="json"),
                request=request,
            )
        )
        db.commit()

        return VehicleServiceResponse(
            id=row_id,
            vehicle_id=vehicle_id,
            service_type=payload.service_type,
            mileage_at_service=payload.mileage_at_service,
            service_date=service_date,
            notes=payload.notes,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("fleet_service_log_create_failed", extra={"tenant_id": tenant_id, "vehicle_id": vehicle_id})
        raise HTTPException(status_code=500, detail="Failed to create service log") from None


@router.get("/api/fleet/vehicles/due-for-service", response_model=list[VehicleResponse])
def list_due_for_service(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VehicleResponse]:
    _ = current_user
    tenant_id = _tenant_id(request)
    today = date.today().isoformat()
    try:

        vehicles = db.execute(
            select(FleetVehicle).where(
                FleetVehicle.tenant_id == tenant_id,
                FleetVehicle.deleted_at.is_(None),
                (
                    (FleetVehicle.next_service_due_on.isnot(None) & (FleetVehicle.next_service_due_on <= today))
                    | (
                        FleetVehicle.last_service_odometer.isnot(None)
                        & ((FleetVehicle.odometer - FleetVehicle.last_service_odometer) >= FleetVehicle.service_interval_miles)
                    )
                ),
            )
            .order_by(FleetVehicle.updated_at.desc())
        ).scalars().all()
        return [_to_response(v) for v in vehicles]
    except SQLAlchemyError:
        log.exception("fleet_due_for_service_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch due-for-service vehicles") from None
