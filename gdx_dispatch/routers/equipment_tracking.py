from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import EquipmentAsset, EquipmentAssetHistory
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["equipment-tracking"],
    dependencies=[Depends(require_module("equipment_tracking"))],
)

ALLOWED_EQUIPMENT_TYPES = {
    "torsion_spring",
    "extension_spring",
    "opener",
    "door_panel",
    "track",
    "roller",
}


class EquipmentCreateRequest(BaseModel):
    customer_id: str = Field(min_length=1, max_length=64)
    equipment_type: str = Field(min_length=1, max_length=64)
    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    warranty_expires_on: date | None = None
    install_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)


class EquipmentUpdateRequest(BaseModel):
    equipment_type: str | None = Field(default=None, max_length=64)
    manufacturer: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=120)
    serial_number: str | None = Field(default=None, max_length=120)
    warranty_expires_on: date | None = None
    install_date: date | None = None
    notes: str | None = Field(default=None, max_length=5000)


class EquipmentResponse(BaseModel):
    id: str
    customer_id: str
    equipment_type: str
    manufacturer: str | None
    model: str | None
    serial_number: str | None
    warranty_expires_on: str | None
    install_date: str | None
    notes: str | None
    created_at: str
    updated_at: str


class EquipmentHistoryCreateRequest(BaseModel):
    service_type: str = Field(min_length=1)
    service_date: datetime | None = None
    technician_id: str = Field(min_length=1)
    notes: str | None = None


class EquipmentHistoryResponse(BaseModel):
    id: str
    equipment_id: str
    service_type: str
    service_date: str
    technician_id: str
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


def _validate_type(equipment_type: str) -> None:
    if equipment_type not in ALLOWED_EQUIPMENT_TYPES:
        raise HTTPException(status_code=422, detail="Invalid equipment_type")


def _to_response(eq: EquipmentAsset) -> EquipmentResponse:
    return EquipmentResponse(
        id=str(eq.id),
        customer_id=str(eq.customer_id),
        equipment_type=str(eq.equipment_type),
        manufacturer=eq.manufacturer,
        model=eq.model,
        serial_number=eq.serial_number,
        warranty_expires_on=eq.warranty_expires_on,
        install_date=getattr(eq, "install_date", None),
        notes=eq.notes,
        created_at=str(eq.created_at),
        updated_at=str(eq.updated_at),
    )


@router.get("/api/equipment", response_model=list[EquipmentResponse])
def list_equipment(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[EquipmentResponse]:
    _ = current_user
    tenant_id = _tenant_id(request)
    try:

        rows = db.execute(
            select(EquipmentAsset)
            .where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.deleted_at.is_(None),
            )
            .order_by(EquipmentAsset.created_at.desc())
        ).scalars().all()
        return [_to_response(eq) for eq in rows]
    except SQLAlchemyError:
        log.exception("equipment_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list equipment") from None


@router.post("/api/equipment", response_model=EquipmentResponse, status_code=201)
def create_equipment(
    payload: EquipmentCreateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EquipmentResponse:
    _validate_type(payload.equipment_type)
    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    try:

        asset = EquipmentAsset(
            id=row_id,
            tenant_id=tenant_id,
            customer_id=payload.customer_id,
            equipment_type=payload.equipment_type,
            manufacturer=payload.manufacturer,
            model=payload.model,
            serial_number=payload.serial_number,
            warranty_expires_on=payload.warranty_expires_on.isoformat() if payload.warranty_expires_on else None,
            install_date=payload.install_date.isoformat() if payload.install_date else None,
            notes=payload.notes,
            created_at=now,
            updated_at=now,
        )
        db.add(asset)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="equipment_created",
                entity_type="equipment",
                entity_id=row_id,
                details=payload.model_dump(mode="json"),
                request=request,
            )
        )
        db.commit()

        return EquipmentResponse(
            id=row_id,
            customer_id=payload.customer_id,
            equipment_type=payload.equipment_type,
            manufacturer=payload.manufacturer,
            model=payload.model,
            serial_number=payload.serial_number,
            warranty_expires_on=payload.warranty_expires_on.isoformat() if payload.warranty_expires_on else None,
            install_date=payload.install_date.isoformat() if payload.install_date else None,
            notes=payload.notes,
            created_at=now,
            updated_at=now,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("equipment_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to create equipment") from None


@router.patch("/api/equipment/{equipment_id}", response_model=EquipmentResponse)
def update_equipment(
    equipment_id: str,
    payload: EquipmentUpdateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EquipmentResponse:
    _validate_uuid(equipment_id, "Equipment")
    tenant_id = _tenant_id(request)
    if payload.equipment_type is not None:
        _validate_type(payload.equipment_type)

    try:

        asset = db.execute(
            select(EquipmentAsset).where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.id == equipment_id,
                EquipmentAsset.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Equipment not found")

        updates = payload.model_dump(exclude_unset=True, mode="json")
        for field, value in updates.items():
            setattr(asset, field, value)
        asset.updated_at = datetime.now(UTC).isoformat()

        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="equipment_updated",
                entity_type="equipment",
                entity_id=equipment_id,
                details=updates,
                request=request,
            )
        )
        db.commit()

        return _to_response(asset)
    except SQLAlchemyError:
        db.rollback()
        log.exception("equipment_update_failed", extra={"tenant_id": tenant_id, "equipment_id": equipment_id})
        raise HTTPException(status_code=500, detail="Failed to update equipment") from None


@router.delete("/api/equipment/{equipment_id}")
def delete_equipment(
    equipment_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _validate_uuid(equipment_id, "Equipment")
    tenant_id = _tenant_id(request)
    deleted_at = datetime.now(UTC).isoformat()
    try:

        asset = db.execute(
            select(EquipmentAsset).where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.id == equipment_id,
                EquipmentAsset.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Equipment not found")

        asset.deleted_at = deleted_at
        asset.updated_at = deleted_at
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="equipment_deleted",
                entity_type="equipment",
                entity_id=equipment_id,
                details={"deleted_at": deleted_at},
                request=request,
            )
        )
        db.commit()

        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("equipment_delete_failed", extra={"tenant_id": tenant_id, "equipment_id": equipment_id})
        raise HTTPException(status_code=500, detail="Failed to delete equipment") from None


@router.get("/api/equipment/{equipment_id}/history", response_model=list[EquipmentHistoryResponse])
def get_equipment_history(
    equipment_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[EquipmentHistoryResponse]:
    _ = current_user
    _validate_uuid(equipment_id, "Equipment")
    tenant_id = _tenant_id(request)
    try:

        rows = db.execute(
            select(EquipmentAssetHistory)
            .where(
                EquipmentAssetHistory.tenant_id == tenant_id,
                EquipmentAssetHistory.equipment_id == equipment_id,
            )
            .order_by(EquipmentAssetHistory.service_date.desc())
        ).scalars().all()
        return [
            EquipmentHistoryResponse(
                id=str(row.id),
                equipment_id=str(row.equipment_id),
                service_type=str(row.service_type),
                service_date=str(row.service_date),
                technician_id=str(row.technician_id),
                notes=row.notes,
            )
            for row in rows
        ]
    except SQLAlchemyError:
        log.exception("equipment_history_get_failed", extra={"tenant_id": tenant_id, "equipment_id": equipment_id})
        raise HTTPException(status_code=500, detail="Failed to load equipment history") from None


@router.post("/api/equipment/{equipment_id}/history", response_model=EquipmentHistoryResponse, status_code=201)
def add_equipment_history(
    equipment_id: str,
    payload: EquipmentHistoryCreateRequest,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EquipmentHistoryResponse:
    _validate_uuid(equipment_id, "Equipment")
    tenant_id = _tenant_id(request)
    row_id = str(uuid4())
    service_date = (payload.service_date or datetime.now(UTC)).isoformat()
    try:

        asset = db.execute(
            select(EquipmentAsset).where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.id == equipment_id,
                EquipmentAsset.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not asset:
            raise HTTPException(status_code=404, detail="Equipment not found")

        history = EquipmentAssetHistory(
            id=row_id,
            tenant_id=tenant_id,
            equipment_id=equipment_id,
            service_type=payload.service_type,
            service_date=service_date,
            technician_id=payload.technician_id,
            notes=payload.notes,
        )
        db.add(history)
        db.commit()

        asyncio.run(
            log_audit_event(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="equipment_history_created",
                entity_type="equipment_history",
                entity_id=row_id,
                details=payload.model_dump(mode="json"),
                request=request,
            )
        )
        db.commit()

        return EquipmentHistoryResponse(
            id=row_id,
            equipment_id=equipment_id,
            service_type=payload.service_type,
            service_date=service_date,
            technician_id=payload.technician_id,
            notes=payload.notes,
        )
    except SQLAlchemyError:
        db.rollback()
        log.exception("equipment_history_create_failed", extra={"tenant_id": tenant_id, "equipment_id": equipment_id})
        raise HTTPException(status_code=500, detail="Failed to add equipment history") from None


@router.get("/api/equipment/expiring-warranties", response_model=list[EquipmentResponse])
def get_expiring_warranties(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[EquipmentResponse]:
    _ = current_user
    tenant_id = _tenant_id(request)
    today = date.today().isoformat()
    horizon = (date.today() + timedelta(days=30)).isoformat()
    try:

        rows = db.execute(
            select(EquipmentAsset)
            .where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.deleted_at.is_(None),
                EquipmentAsset.warranty_expires_on.isnot(None),
                EquipmentAsset.warranty_expires_on >= today,
                EquipmentAsset.warranty_expires_on <= horizon,
            )
            .order_by(EquipmentAsset.warranty_expires_on.asc())
        ).scalars().all()
        return [_to_response(eq) for eq in rows]
    except SQLAlchemyError:
        log.exception("equipment_expiring_warranties_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to fetch expiring warranties") from None


# ---------------------------------------------------------------------------
# Predictive Maintenance — flag equipment likely to need service soon
# ---------------------------------------------------------------------------

# Average service intervals by equipment type (in days)
_SERVICE_INTERVALS: dict[str, int] = {
    "torsion_spring": 1095,   # ~3 years
    "extension_spring": 730,  # ~2 years
    "opener": 1825,           # ~5 years
    "door_panel": 3650,       # ~10 years
    "track": 2555,            # ~7 years
    "roller": 730,            # ~2 years
}


class PredictiveMaintenanceItem(BaseModel):
    equipment_id: str
    customer_id: str
    equipment_type: str
    manufacturer: str | None
    model: str | None
    last_service_date: str | None
    days_since_service: int | None
    expected_interval_days: int
    risk_score: float  # 0.0 to 1.0
    recommendation: str


@router.get("/api/equipment/predictive-maintenance", response_model=list[PredictiveMaintenanceItem])
def get_predictive_maintenance(
    request: Request,
    risk_threshold: float = 0.5,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PredictiveMaintenanceItem]:
    """Flag equipment nearing failure based on service history and expected intervals."""
    _ = current_user
    tenant_id = _tenant_id(request)
    today = date.today()

    try:


        # Get all active equipment with their most recent service date via subquery
        last_service_subq = (
            select(
                EquipmentAssetHistory.equipment_id,
                func.max(EquipmentAssetHistory.service_date).label("last_service_date"),
            )
            .where(EquipmentAssetHistory.tenant_id == tenant_id)
            .group_by(EquipmentAssetHistory.equipment_id)
            .subquery()
        )

        rows = db.execute(
            select(
                EquipmentAsset,
                last_service_subq.c.last_service_date,
            )
            .outerjoin(
                last_service_subq,
                EquipmentAsset.id == last_service_subq.c.equipment_id,
            )
            .where(
                EquipmentAsset.tenant_id == tenant_id,
                EquipmentAsset.deleted_at.is_(None),
            )
        ).all()

        results: list[PredictiveMaintenanceItem] = []
        for row_tuple in rows:
            eq = row_tuple[0]
            last_svc = row_tuple[1]
            eq_type = str(eq.equipment_type)
            interval = _SERVICE_INTERVALS.get(eq_type, 1095)

            # Determine last service date
            if last_svc:
                try:
                    last_svc_date = datetime.fromisoformat(str(last_svc)).date()
                except (ValueError, TypeError):
                    log.exception("equipment_service_date_parse_failed")
                    last_svc_date = None
            else:
                last_svc_date = None

            # If never serviced, use creation date
            if not last_svc_date:
                try:
                    last_svc_date = datetime.fromisoformat(str(eq.created_at)).date()
                except (ValueError, TypeError):
                    log.exception("equipment_created_at_parse_failed")
                    last_svc_date = today - timedelta(days=interval)

            days_since = (today - last_svc_date).days
            # Risk score: ratio of days since last service to expected interval
            risk = min(round(days_since / interval, 2), 1.0) if interval > 0 else 0.0

            if risk < risk_threshold:
                continue

            if risk >= 0.9:
                rec = f"URGENT: {eq_type} overdue for service ({days_since} days since last service)"
            elif risk >= 0.7:
                rec = f"Schedule {eq_type} service soon — approaching maintenance interval"
            else:
                rec = f"Monitor {eq_type} — nearing expected service window"

            results.append(
                PredictiveMaintenanceItem(
                    equipment_id=str(eq.id),
                    customer_id=str(eq.customer_id),
                    equipment_type=eq_type,
                    manufacturer=eq.manufacturer,
                    model=eq.model,
                    last_service_date=str(last_svc) if last_svc else None,
                    days_since_service=days_since,
                    expected_interval_days=interval,
                    risk_score=risk,
                    recommendation=rec,
                )
            )

        results.sort(key=lambda x: x.risk_score, reverse=True)

        log.info(
            "predictive_maintenance_scan",
            extra={"tenant_id": tenant_id, "total_equipment": len(rows), "flagged": len(results)},
        )

        return results

    except SQLAlchemyError:
        log.exception("predictive_maintenance_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to run predictive maintenance scan") from None
