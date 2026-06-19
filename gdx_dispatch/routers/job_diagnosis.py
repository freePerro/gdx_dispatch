"""Sprint 5 / S5-B1 — structured per-service-type diagnosis form.

Each `service_type` keys a small schema describing the fields the tech
fills out on-site. Schemas live in code so dispatch can rely on stable
field names when searching ("all spring jobs done last month").
"""
from __future__ import annotations

import logging
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import JobDiagnosis
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["job-diagnosis"])


# -- Schema catalog ---------------------------------------------------------
# Each entry: list of {key, label, type, options?}.
# `type` ∈ {"text", "number", "boolean", "select"}.
DIAGNOSIS_SCHEMAS: dict[str, list[dict[str, Any]]] = {
    "broken_spring": [
        {"key": "spring_type", "label": "Spring Type", "type": "select",
         "options": ["torsion", "extension"]},
        {"key": "wire_gauge", "label": "Wire Gauge", "type": "text"},
        {"key": "length_inches", "label": "Length (in)", "type": "number"},
        {"key": "estimated_cycles", "label": "Est. Cycles", "type": "number"},
        {"key": "both_sides_replaced", "label": "Replaced both sides", "type": "boolean"},
    ],
    "opener_replacement": [
        {"key": "old_make", "label": "Old Make", "type": "text"},
        {"key": "old_model", "label": "Old Model", "type": "text"},
        {"key": "new_make", "label": "New Make", "type": "text"},
        {"key": "new_model", "label": "New Model", "type": "text"},
        {"key": "horsepower", "label": "HP", "type": "select",
         "options": ["1/2", "3/4", "1", "1.25"]},
        {"key": "drive_type", "label": "Drive Type", "type": "select",
         "options": ["chain", "belt", "screw", "direct"]},
        {"key": "battery_backup", "label": "Battery Backup", "type": "boolean"},
    ],
    "panel_damage": [
        {"key": "sections_damaged", "label": "Sections Damaged", "type": "number"},
        {"key": "section_position", "label": "Position", "type": "select",
         "options": ["top", "middle", "bottom", "multiple"]},
        {"key": "cause", "label": "Cause", "type": "select",
         "options": ["impact", "weather", "rust", "wear", "unknown"]},
        {"key": "replacement_recommended", "label": "Replacement Recommended", "type": "boolean"},
    ],
    "off_track": [
        {"key": "side", "label": "Side", "type": "select",
         "options": ["left", "right", "both"]},
        {"key": "cause", "label": "Cause", "type": "select",
         "options": ["broken_cable", "broken_roller", "bent_track", "impact", "other"]},
        {"key": "track_bent", "label": "Track Bent", "type": "boolean"},
        {"key": "rollers_replaced", "label": "Rollers Replaced", "type": "boolean"},
    ],
    "tune_up": [
        {"key": "rollers_lubricated", "label": "Rollers Lubricated", "type": "boolean"},
        {"key": "hinges_lubricated", "label": "Hinges Lubricated", "type": "boolean"},
        {"key": "spring_balance_ok", "label": "Spring Balance OK", "type": "boolean"},
        {"key": "safety_reverse_ok", "label": "Safety Reverse OK", "type": "boolean"},
        {"key": "cycles_remaining_pct", "label": "Cycles Remaining (%)", "type": "number"},
    ],
}


class DiagnosisIn(BaseModel):
    service_type: str = Field(min_length=1, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=5000)


class DiagnosisOut(BaseModel):
    id: str
    job_id: str
    service_type: str
    data: dict[str, Any]
    notes: str | None
    created_by: str | None
    created_at: str
    updated_at: str


def _validate_uuid(value: str, entity: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None


def _validate_service_type(service_type: str) -> None:
    if service_type not in DIAGNOSIS_SCHEMAS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown service_type. Allowed: {sorted(DIAGNOSIS_SCHEMAS.keys())}",
        )


def _to_response(d: JobDiagnosis) -> DiagnosisOut:
    return DiagnosisOut(
        id=str(d.id),
        job_id=str(d.job_id),
        service_type=d.service_type,
        data=d.data or {},
        notes=d.notes,
        created_by=d.created_by,
        created_at=d.created_at.isoformat() if d.created_at else "",
        updated_at=d.updated_at.isoformat() if d.updated_at else "",
    )


@router.get("/api/diagnosis/schemas")
def list_schemas() -> dict[str, Any]:
    """Return the per-service-type field catalog so the UI can render forms."""
    return {"schemas": DIAGNOSIS_SCHEMAS}


@router.get("/api/jobs/{job_id}/diagnosis", response_model=list[DiagnosisOut])
def list_diagnoses(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[DiagnosisOut]:
    _ = current_user
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")
    try:
        rows = db.execute(
            select(JobDiagnosis)
            .where(JobDiagnosis.job_id == job_uuid, JobDiagnosis.deleted_at.is_(None))
            .order_by(JobDiagnosis.created_at.desc())
        ).scalars().all()
        return [_to_response(d) for d in rows]
    except SQLAlchemyError:
        log.exception("diagnosis_list_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to list diagnoses") from None


@router.post("/api/jobs/{job_id}/diagnosis", response_model=DiagnosisOut, status_code=201)
def create_diagnosis(
    job_id: str,
    payload: DiagnosisIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiagnosisOut:
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")
    _validate_service_type(payload.service_type)
    user = current_user or {}
    created_by = str(user.get("user_id") or user.get("sub") or "system")

    try:
        diag = JobDiagnosis(
            id=uuid4(),
            job_id=job_uuid,
            service_type=payload.service_type,
            data=payload.data,
            notes=payload.notes,
            created_by=created_by,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(diag)
        db.commit()
        db.refresh(diag)
        return _to_response(diag)
    except SQLAlchemyError:
        db.rollback()
        log.exception("diagnosis_create_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to save diagnosis") from None


@router.patch("/api/diagnosis/{diagnosis_id}", response_model=DiagnosisOut)
def update_diagnosis(
    diagnosis_id: str,
    payload: DiagnosisIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiagnosisOut:
    _ = current_user
    _ = request
    diag_uuid = _validate_uuid(diagnosis_id, "Diagnosis")
    _validate_service_type(payload.service_type)
    try:
        diag = db.execute(
            select(JobDiagnosis).where(
                JobDiagnosis.id == diag_uuid,
                JobDiagnosis.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not diag:
            raise HTTPException(status_code=404, detail="Diagnosis not found")
        diag.service_type = payload.service_type
        diag.data = payload.data
        diag.notes = payload.notes
        diag.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(diag)
        return _to_response(diag)
    except SQLAlchemyError:
        db.rollback()
        log.exception("diagnosis_update_failed", extra={"diagnosis_id": diagnosis_id})
        raise HTTPException(status_code=500, detail="Failed to update diagnosis") from None


@router.delete("/api/diagnosis/{diagnosis_id}")
def delete_diagnosis(
    diagnosis_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _ = current_user
    _ = request
    diag_uuid = _validate_uuid(diagnosis_id, "Diagnosis")
    try:
        diag = db.execute(
            select(JobDiagnosis).where(
                JobDiagnosis.id == diag_uuid,
                JobDiagnosis.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not diag:
            raise HTTPException(status_code=404, detail="Diagnosis not found")
        diag.deleted_at = datetime.now(UTC)
        db.commit()
        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("diagnosis_delete_failed", extra={"diagnosis_id": diagnosis_id})
        raise HTTPException(status_code=500, detail="Failed to delete diagnosis") from None
