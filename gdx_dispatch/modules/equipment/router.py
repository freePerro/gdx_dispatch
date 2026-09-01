from __future__ import annotations

import contextlib
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.modules.equipment.models import CustomerEquipment, EquipmentServiceHistory

router = APIRouter(prefix="/api", tags=["equipment"], dependencies=[Depends(require_module("equipment_tracking")), Depends(get_current_user)])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


def _audit(
    db: Session,
    request: Request,
    user: dict,
    *,
    action: str,
    entity_type: str,
    entity_id: object,
    details: dict | None = None,
) -> None:
    """Invariant #1: every create/update/delete here answers who, what, when.

    Equipment edits went unrecorded from 2026-05-03 (router unwired) until
    #451 made `update_equipment` reachable again — the audit row is what
    makes "who changed this door's serial number" answerable (#452).
    """
    log_audit_event_sync(
        db,
        tenant_id=company_id(),
        user_id=user.get("sub") or user.get("user_id") or user.get("id") or "system",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=details or {},
        request=request,
    )


class EquipmentIn(BaseModel):
    equipment_type: str
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    installation_date: date | None = None
    last_service_date: date | None = None
    notes: str | None = None
    metadata_: dict | None = None


class EquipmentPatch(BaseModel):
    equipment_type: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    installation_date: date | None = None
    last_service_date: date | None = None
    notes: str | None = None
    metadata_: dict | None = None
    deleted_at: datetime | None = None


class EquipmentCreate(BaseModel):
    """Flat create schema for /api/equipment POST.

    Field aliases preserved for legacy clients: `type` ↔ `equipment_type`,
    `make` ↔ `manufacturer`, `install_date` ↔ `installation_date`.
    """
    customer_id: UUID
    type: str | None = None
    equipment_type: str | None = None
    make: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    install_date: date | None = None
    installation_date: date | None = None
    warranty_expires_on: date | None = None
    notes: str | None = None


class EquipmentUpdate(BaseModel):
    """Flat update schema for /api/equipment/{id} PUT."""
    type: str | None = None
    equipment_type: str | None = None
    make: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None
    install_date: date | None = None
    installation_date: date | None = None
    warranty_expires_on: date | None = None
    notes: str | None = None


class ServiceEventIn(BaseModel):
    service_type: str
    technician_id: str
    service_date: datetime | None = None
    notes: str | None = None
    parts_used: list[dict] | None = None


class ServiceLogIn(BaseModel):
    """Flat service log schema for /api/equipment/{id}/service POST."""
    job_id: UUID | None = None
    service_type: str
    technician_notes: str | None = None
    parts_used: list[dict] | None = None
    date: datetime | None = None


# ---------------------------------------------------------------------------
# Existing routes (customer-scoped)
# ---------------------------------------------------------------------------

@router.get("/customers/{customer_id}/equipment", response_model=None)
def list_equipment(customer_id: UUID, db: Session = Depends(get_db)) -> list[CustomerEquipment]:
    return list(
        db.execute(
            select(CustomerEquipment)
            .where(CustomerEquipment.customer_id == customer_id, CustomerEquipment.deleted_at.is_(None))
            .order_by(CustomerEquipment.created_at.desc())
        ).scalars().all()
    )


@router.post("/customers/{customer_id}/equipment", response_model=None)
def create_equipment_for_customer(
    customer_id: UUID,
    payload: EquipmentIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerEquipment:
    row = CustomerEquipment(customer_id=customer_id, **payload.model_dump())
    db.add(row)
    db.flush()
    _audit(
        db, request, user,
        action="equipment_created", entity_type="equipment", entity_id=row.id,
        details={"customer_id": str(customer_id), **payload.model_dump(mode="json")},
    )
    db.commit()
    db.refresh(row)
    return row


@router.put("/customers/{customer_id}/equipment/{equipment_id}", response_model=None)
def update_equipment_for_customer(
    customer_id: UUID,
    equipment_id: UUID,
    payload: EquipmentPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerEquipment:
    row = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.customer_id == customer_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Equipment not found")
    changes = payload.model_dump(exclude_unset=True)
    before = {k: getattr(row, k) for k in changes}
    for k, v in changes.items():
        setattr(row, k, v)
    _audit(
        db, request, user,
        action="equipment_updated", entity_type="equipment", entity_id=row.id,
        details={
            "customer_id": str(customer_id),
            "changes": payload.model_dump(exclude_unset=True, mode="json"),
            "before": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in before.items()},
        },
    )
    db.commit()
    db.refresh(row)
    return row


@router.get("/customers/{customer_id}/equipment/{equipment_id}/history", response_model=None)
def equipment_history(
    customer_id: UUID,
    equipment_id: UUID,
    db: Session = Depends(get_db),
) -> list[EquipmentServiceHistory]:
    exists = db.execute(
        select(CustomerEquipment.id).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.customer_id == customer_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not exists:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return list(
        db.execute(
            select(EquipmentServiceHistory)
            .where(EquipmentServiceHistory.equipment_id == equipment_id)
            .order_by(EquipmentServiceHistory.service_date.desc())
        ).scalars().all()
    )


@router.post("/jobs/{job_id}/equipment/{equipment_id}/service", response_model=None)
def log_service_event(
    job_id: UUID,
    equipment_id: UUID,
    payload: ServiceEventIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EquipmentServiceHistory:
    exists = db.execute(
        select(CustomerEquipment.id).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not exists:
        raise HTTPException(status_code=404, detail="Equipment not found")
    row = EquipmentServiceHistory(
        equipment_id=equipment_id,
        job_id=job_id,
        **payload.model_dump(exclude_unset=True),
    )
    db.add(row)
    db.flush()
    _audit(
        db, request, user,
        action="equipment_service_logged", entity_type="equipment_service", entity_id=row.id,
        details={"equipment_id": str(equipment_id), "job_id": str(job_id),
                 **payload.model_dump(exclude_unset=True, mode="json")},
    )
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# New tenant-wide routes
# ---------------------------------------------------------------------------

@router.get("/equipment", response_model=None)
def list_all_equipment(
    customer_id: UUID | None = None,
    db: Session = Depends(get_db),
) -> list[CustomerEquipment]:
    """List all equipment for the tenant, with optional customer_id filter."""
    q = select(CustomerEquipment).where(CustomerEquipment.deleted_at.is_(None))
    if customer_id is not None:
        q = q.where(CustomerEquipment.customer_id == customer_id)
    return list(db.execute(q.order_by(CustomerEquipment.created_at.desc())).scalars().all())


@router.post("/equipment", response_model=None)
def create_equipment(
    payload: EquipmentCreate,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerEquipment:
    """Create equipment record for any customer."""
    row = CustomerEquipment(
        customer_id=payload.customer_id,
        equipment_type=payload.equipment_type or payload.type or "other",
        manufacturer=payload.manufacturer or payload.make,
        model=payload.model,
        serial_number=payload.serial_number,
        installation_date=payload.installation_date or payload.install_date,
        warranty_expires_on=payload.warranty_expires_on,
        notes=payload.notes,
    )
    db.add(row)
    db.flush()
    _audit(
        db, request, user,
        action="equipment_created", entity_type="equipment", entity_id=row.id,
        details=payload.model_dump(exclude_unset=True, mode="json"),
    )
    db.commit()
    db.refresh(row)
    return row


@router.get("/equipment/{equipment_id}", response_model=None)
def get_equipment(
    equipment_id: UUID,
    db: Session = Depends(get_db),
) -> dict:
    """Get a single equipment record with full service history."""
    row = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Equipment not found")

    history = list(
        db.execute(
            select(EquipmentServiceHistory)
            .where(EquipmentServiceHistory.equipment_id == equipment_id)
            .order_by(EquipmentServiceHistory.service_date.desc())
        ).scalars().all()
    )

    def _svc_dict(s: EquipmentServiceHistory) -> dict:
        return {
            "id": str(s.id),
            "equipment_id": str(s.equipment_id),
            "job_id": str(s.job_id) if s.job_id else None,
            "service_type": s.service_type,
            "technician_id": s.technician_id,
            "service_date": s.service_date.isoformat() if s.service_date else None,
            "notes": s.notes,
            "parts_used": s.parts_used,
        }

    return {
        "id": str(row.id),
        "customer_id": str(row.customer_id),
        "equipment_type": row.equipment_type,
        "manufacturer": row.manufacturer,
        "model": row.model,
        "serial_number": row.serial_number,
        "installation_date": row.installation_date.isoformat() if row.installation_date else None,
        "install_date": row.installation_date.isoformat() if row.installation_date else None,
        "last_service_date": row.last_service_date.isoformat() if row.last_service_date else None,
        "warranty_expires_on": row.warranty_expires_on.isoformat() if row.warranty_expires_on else None,
        "notes": row.notes,
        "metadata_": row.metadata_,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "deleted_at": row.deleted_at.isoformat() if row.deleted_at else None,
        "service_history": [_svc_dict(s) for s in history],
    }


@router.post("/equipment/{equipment_id}/service", response_model=None)
def log_equipment_service(
    equipment_id: UUID,
    payload: ServiceLogIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> EquipmentServiceHistory:
    """Log a service record for an equipment item."""
    row = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Equipment not found")

    service_date = payload.date or utcnow()
    svc = EquipmentServiceHistory(
        equipment_id=equipment_id,
        job_id=payload.job_id,
        service_type=payload.service_type,
        technician_id="system",
        notes=payload.technician_notes,
        parts_used=payload.parts_used,
        service_date=service_date,
    )
    db.add(svc)

    # Update last_service_date on the equipment
    if row.last_service_date is None or (
        hasattr(service_date, "date") and service_date.date() > row.last_service_date
    ) or (
        not hasattr(service_date, "date") and service_date > row.last_service_date  # type: ignore[operator]
    ):
        with contextlib.suppress(Exception):
            row.last_service_date = service_date.date() if hasattr(service_date, "date") else service_date

    db.flush()
    _audit(
        db, request, user,
        action="equipment_service_logged", entity_type="equipment_service", entity_id=svc.id,
        details={"equipment_id": str(equipment_id), **payload.model_dump(exclude_unset=True, mode="json")},
    )
    db.commit()
    db.refresh(svc)
    return svc


@router.get("/equipment/{equipment_id}/service-history", response_model=None)
def get_equipment_service_history(
    equipment_id: UUID,
    db: Session = Depends(get_db),
) -> list[EquipmentServiceHistory]:
    """List all service records for an equipment item."""
    exists = db.execute(
        select(CustomerEquipment.id).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not exists:
        raise HTTPException(status_code=404, detail="Equipment not found")
    return list(
        db.execute(
            select(EquipmentServiceHistory)
            .where(EquipmentServiceHistory.equipment_id == equipment_id)
            .order_by(EquipmentServiceHistory.service_date.desc())
        ).scalars().all()
    )


@router.put("/equipment/{equipment_id}", response_model=None)
def update_equipment(
    equipment_id: UUID,
    payload: EquipmentUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CustomerEquipment:
    """Update an equipment record."""
    row = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Equipment not found")

    field_map = {
        "type": "equipment_type",
        "equipment_type": "equipment_type",
        "make": "manufacturer",
        "manufacturer": "manufacturer",
        "model": "model",
        "serial_number": "serial_number",
        "install_date": "installation_date",
        "installation_date": "installation_date",
        "warranty_expires_on": "warranty_expires_on",
        "notes": "notes",
    }
    changes: dict[str, object] = {}
    before: dict[str, object] = {}
    for src, dst in field_map.items():
        val = getattr(payload, src, None)
        if val is not None:
            prev = getattr(row, dst)
            if prev != val:
                before[dst] = prev.isoformat() if hasattr(prev, "isoformat") else prev
                changes[dst] = val.isoformat() if hasattr(val, "isoformat") else val
            setattr(row, dst, val)

    _audit(
        db, request, user,
        action="equipment_updated", entity_type="equipment", entity_id=row.id,
        details={"changes": changes, "before": before},
    )
    db.commit()
    db.refresh(row)
    return row


@router.delete("/equipment/{equipment_id}", response_model=None)
def delete_equipment(
    equipment_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Soft-delete an equipment record."""
    row = db.execute(
        select(CustomerEquipment).where(
            CustomerEquipment.id == equipment_id,
            CustomerEquipment.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Equipment not found")
    row.deleted_at = utcnow()
    _audit(
        db, request, user,
        action="equipment_deleted", entity_type="equipment", entity_id=row.id,
        details={"customer_id": str(row.customer_id), "equipment_type": row.equipment_type,
                 "serial_number": row.serial_number},
    )
    db.commit()
    return {"id": str(equipment_id), "deleted": True}
