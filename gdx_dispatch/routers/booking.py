from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import BookingJob, BookingRequest
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["booking"], dependencies=[Depends(require_module("customer_portal"))])

DEFAULT_SLOTS = ["09:00", "11:00", "13:00", "15:00"]


class BookingRequestCreate(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    service: str = Field(min_length=1)
    preferred_date: date
    preferred_slot: str | None = None


class BookingRequestResponse(BaseModel):
    id: str; name: str; phone: str; service: str
    preferred_date: str; preferred_slot: str | None
    status: str; decline_reason: str | None; created_at: str


class BookingDeclineRequest(BaseModel):
    reason: str = Field(min_length=1)


class BookingApproveResponse(BaseModel):
    request_id: str; status: str; job_id: str


class AvailableSlotsResponse(BaseModel):
    date: str; slots: list[str]


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _uid(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "system")


def _serialize(b: BookingRequest) -> BookingRequestResponse:
    return BookingRequestResponse(
        id=b.id, name=b.name, phone=b.phone, service=b.service,
        preferred_date=b.preferred_date, preferred_slot=b.preferred_slot,
        status=b.status, decline_reason=b.decline_reason, created_at=b.created_at,
    )


@router.get("/api/booking/available-slots", response_model=AvailableSlotsResponse)
def get_available_slots(
    request: Request, date: date = Query(...),
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> AvailableSlotsResponse:
    tenant_id = _tid(request)
    try:
        rows = db.execute(
            select(BookingRequest.preferred_slot).where(
                BookingRequest.tenant_id == tenant_id,
                BookingRequest.preferred_date == date.isoformat(),
                BookingRequest.status.in_(["pending", "approved"]),
                BookingRequest.preferred_slot.isnot(None),
            )
        ).scalars().all()
        blocked = set(rows)
        return AvailableSlotsResponse(date=date.isoformat(), slots=[s for s in DEFAULT_SLOTS if s not in blocked])
    except SQLAlchemyError:
        log.exception("booking_available_slots_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to load available slots") from None


@router.post("/api/booking/request", response_model=BookingRequestResponse, status_code=201)
def create_booking_request(
    payload: BookingRequestCreate, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> BookingRequestResponse:
    tenant_id = _tid(request)
    now = datetime.now(UTC).isoformat()
    try:
        b = BookingRequest(
            id=str(uuid4()), tenant_id=tenant_id, name=payload.name, phone=payload.phone,
            service=payload.service, preferred_date=payload.preferred_date.isoformat(),
            preferred_slot=payload.preferred_slot, status="pending",
            decline_reason=None, approved_job_id=None, created_at=now, updated_at=now,
        )
        db.add(b)
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_uid(current_user),
                                     action="booking_request_created", entity_type="booking_request",
                                     entity_id=b.id, details=payload.model_dump(mode="json"), request=request))
        db.commit()
        return _serialize(b)
    except SQLAlchemyError:
        db.rollback()
        log.exception("booking_request_create_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to create booking request") from None


@router.get("/api/booking/requests", response_model=list[BookingRequestResponse])
def list_booking_requests(
    request: Request, status: str = Query(default="pending"),
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> list[BookingRequestResponse]:
    tenant_id = _tid(request)
    try:
        rows = db.execute(
            select(BookingRequest).where(BookingRequest.tenant_id == tenant_id, BookingRequest.status == status)
            .order_by(BookingRequest.created_at.asc())
        ).scalars().all()
        return [_serialize(r) for r in rows]
    except SQLAlchemyError:
        log.exception("booking_requests_list_failed", extra={"tenant_id": tenant_id})
        raise HTTPException(status_code=500, detail="Failed to list booking requests") from None


@router.post("/api/booking/requests/{request_id}/approve", response_model=BookingApproveResponse)
def approve_booking_request(
    request_id: str, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> BookingApproveResponse:
    tenant_id = _tid(request)
    now = datetime.now(UTC).isoformat()
    try:
        b = db.execute(
            select(BookingRequest).where(BookingRequest.tenant_id == tenant_id, BookingRequest.id == request_id)
        ).scalar_one_or_none()
        if not b:
            raise HTTPException(status_code=404, detail="Booking request not found")
        if b.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending requests can be approved")

        job_id = str(uuid4())
        b.status = "approved"
        b.approved_job_id = job_id
        b.updated_at = now
        db.add(BookingJob(id=job_id, tenant_id=tenant_id, booking_request_id=request_id, created_at=now))
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_uid(current_user),
                                     action="booking_request_approved", entity_type="booking_request",
                                     entity_id=request_id, details={"job_id": job_id}, request=request))
        db.commit()
        return BookingApproveResponse(request_id=request_id, status="approved", job_id=job_id)
    except SQLAlchemyError:
        db.rollback()
        log.exception("booking_request_approve_failed", extra={"tenant_id": tenant_id, "request_id": request_id})
        raise HTTPException(status_code=500, detail="Failed to approve booking request") from None


@router.post("/api/booking/requests/{request_id}/decline", response_model=BookingRequestResponse)
def decline_booking_request(
    request_id: str, payload: BookingDeclineRequest, request: Request,
    current_user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db),
) -> BookingRequestResponse:
    tenant_id = _tid(request)
    now = datetime.now(UTC).isoformat()
    try:
        b = db.execute(
            select(BookingRequest).where(BookingRequest.tenant_id == tenant_id, BookingRequest.id == request_id)
        ).scalar_one_or_none()
        if not b:
            raise HTTPException(status_code=404, detail="Booking request not found")
        if b.status != "pending":
            raise HTTPException(status_code=400, detail="Only pending requests can be declined")

        b.status = "declined"
        b.decline_reason = payload.reason
        b.updated_at = now
        db.commit()
        asyncio.run(log_audit_event(db=db, tenant_id=tenant_id, user_id=_uid(current_user),
                                     action="booking_request_declined", entity_type="booking_request",
                                     entity_id=request_id, details={"reason": payload.reason}, request=request))
        db.commit()
        return _serialize(b)
    except SQLAlchemyError:
        db.rollback()
        log.exception("booking_request_decline_failed", extra={"tenant_id": tenant_id, "request_id": request_id})
        raise HTTPException(status_code=500, detail="Failed to decline booking request") from None
