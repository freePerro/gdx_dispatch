"""Sprint 5 / S5-B2 + S5-B3 — hazards and receipts attached to a job."""
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
from gdx_dispatch.models.tenant_models import JobHazard, JobReceipt, Job
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["job-hazards-receipts"])


SEVERITY_LEVELS = {"low", "medium", "high", "critical"}


# -- Hazards ----------------------------------------------------------------

class HazardIn(BaseModel):
    description: str = Field(min_length=1, max_length=5000)
    severity: str = Field(default="medium")
    photo_url: str | None = Field(default=None, max_length=2000)
    applies_to_customer: bool = False


class HazardOut(BaseModel):
    id: str
    job_id: str
    customer_id: str | None
    description: str
    severity: str
    photo_url: str | None
    applies_to_customer: bool
    created_by: str | None
    created_at: str


def _validate_uuid(value: str, entity: str) -> _uuid.UUID:
    try:
        return _uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail=f"{entity} not found") from None


def _validate_severity(severity: str) -> None:
    if severity not in SEVERITY_LEVELS:
        raise HTTPException(
            status_code=422, detail=f"severity must be one of {sorted(SEVERITY_LEVELS)}"
        )


def _hazard_to_response(h: JobHazard) -> HazardOut:
    return HazardOut(
        id=str(h.id),
        job_id=str(h.job_id),
        customer_id=str(h.customer_id) if h.customer_id else None,
        description=h.description,
        severity=h.severity,
        photo_url=h.photo_url,
        applies_to_customer=bool(h.applies_to_customer),
        created_by=h.created_by,
        created_at=h.created_at.isoformat() if h.created_at else "",
    )


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


@router.get("/api/jobs/{job_id}/hazards", response_model=list[HazardOut])
def list_hazards(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[HazardOut]:
    _ = current_user
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")

    job = db.execute(
        select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        rows = db.execute(
            select(JobHazard)
            .where(JobHazard.job_id == job_uuid, JobHazard.deleted_at.is_(None))
            .order_by(JobHazard.created_at.desc())
        ).scalars().all()
        out = list(rows)
        if job.customer_id:
            sticky = db.execute(
                select(JobHazard)
                .where(
                    JobHazard.customer_id == job.customer_id,
                    JobHazard.applies_to_customer.is_(True),
                    JobHazard.job_id != job_uuid,
                    JobHazard.deleted_at.is_(None),
                )
                .order_by(JobHazard.created_at.desc())
            ).scalars().all()
            seen = {h.id for h in out}
            for h in sticky:
                if h.id not in seen:
                    out.append(h)
        return [_hazard_to_response(h) for h in out]
    except SQLAlchemyError:
        log.exception("hazards_list_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to list hazards") from None


@router.post("/api/jobs/{job_id}/hazards", response_model=HazardOut, status_code=201)
def create_hazard(
    job_id: str,
    payload: HazardIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> HazardOut:
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")
    _validate_severity(payload.severity)

    job = db.execute(
        select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        haz = JobHazard(
            id=uuid4(),
            job_id=job_uuid,
            customer_id=job.customer_id if payload.applies_to_customer else None,
            description=payload.description,
            severity=payload.severity,
            photo_url=payload.photo_url,
            applies_to_customer=payload.applies_to_customer,
            created_by=_user_id(current_user),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(haz)
        db.commit()
        db.refresh(haz)
        return _hazard_to_response(haz)
    except SQLAlchemyError:
        db.rollback()
        log.exception("hazard_create_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to save hazard") from None


@router.delete("/api/hazards/{hazard_id}")
def delete_hazard(
    hazard_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _ = current_user
    _ = request
    haz_uuid = _validate_uuid(hazard_id, "Hazard")
    try:
        haz = db.execute(
            select(JobHazard).where(
                JobHazard.id == haz_uuid, JobHazard.deleted_at.is_(None)
            )
        ).scalar_one_or_none()
        if not haz:
            raise HTTPException(status_code=404, detail="Hazard not found")
        haz.deleted_at = datetime.now(UTC)
        db.commit()
        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("hazard_delete_failed", extra={"hazard_id": hazard_id})
        raise HTTPException(status_code=500, detail="Failed to delete hazard") from None


# -- Receipts ---------------------------------------------------------------

class ReceiptIn(BaseModel):
    vendor: str | None = Field(default=None, max_length=200)
    amount: Decimal | None = None
    photo_url: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)
    purchased_at: datetime | None = None


class ReceiptOut(BaseModel):
    id: str
    job_id: str
    vendor: str | None
    amount: float | None
    photo_url: str | None
    notes: str | None
    purchased_at: str | None
    created_by: str | None
    created_at: str


def _receipt_to_response(r: JobReceipt) -> ReceiptOut:
    return ReceiptOut(
        id=str(r.id),
        job_id=str(r.job_id),
        vendor=r.vendor,
        amount=float(r.amount) if r.amount is not None else None,
        photo_url=r.photo_url,
        notes=r.notes,
        purchased_at=r.purchased_at.isoformat() if r.purchased_at else None,
        created_by=r.created_by,
        created_at=r.created_at.isoformat() if r.created_at else "",
    )


@router.get("/api/jobs/{job_id}/receipts", response_model=list[ReceiptOut])
def list_receipts(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ReceiptOut]:
    _ = current_user
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")
    try:
        rows = db.execute(
            select(JobReceipt)
            .where(JobReceipt.job_id == job_uuid, JobReceipt.deleted_at.is_(None))
            .order_by(JobReceipt.created_at.desc())
        ).scalars().all()
        return [_receipt_to_response(r) for r in rows]
    except SQLAlchemyError:
        log.exception("receipts_list_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to list receipts") from None


@router.post("/api/jobs/{job_id}/receipts", response_model=ReceiptOut, status_code=201)
def create_receipt(
    job_id: str,
    payload: ReceiptIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReceiptOut:
    _ = request
    job_uuid = _validate_uuid(job_id, "Job")
    try:
        rec = JobReceipt(
            id=uuid4(),
            job_id=job_uuid,
            vendor=payload.vendor,
            amount=payload.amount,
            photo_url=payload.photo_url,
            notes=payload.notes,
            purchased_at=payload.purchased_at,
            created_by=_user_id(current_user),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return _receipt_to_response(rec)
    except SQLAlchemyError:
        db.rollback()
        log.exception("receipt_create_failed", extra={"job_id": job_id})
        raise HTTPException(status_code=500, detail="Failed to save receipt") from None


@router.delete("/api/receipts/{receipt_id}")
def delete_receipt(
    receipt_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _ = current_user
    _ = request
    rec_uuid = _validate_uuid(receipt_id, "Receipt")
    try:
        rec = db.execute(
            select(JobReceipt).where(
                JobReceipt.id == rec_uuid, JobReceipt.deleted_at.is_(None)
            )
        ).scalar_one_or_none()
        if not rec:
            raise HTTPException(status_code=404, detail="Receipt not found")
        rec.deleted_at = datetime.now(UTC)
        db.commit()
        return {"deleted": True}
    except SQLAlchemyError:
        db.rollback()
        log.exception("receipt_delete_failed", extra={"receipt_id": receipt_id})
        raise HTTPException(status_code=500, detail="Failed to delete receipt") from None
