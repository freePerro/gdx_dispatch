from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Warranty
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/warranties", tags=["warranties"], dependencies=[Depends(require_module("warranties"))])

_ALLOWED_STATUSES = {"active", "voided", "claimed", "expired"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _serialize(w: Warranty) -> dict[str, Any]:
    return {
        "id": str(w.id), "job_id": str(w.job_id), "customer_id": str(w.customer_id),
        "description": w.description, "start_date": str(w.start_date), "end_date": str(w.end_date),
        "terms": w.terms, "status": w.status or "active",
        "claim_count": int(w.claim_count or 0),
        "last_claim_at": _iso(w.last_claim_at), "last_claim_notes": w.last_claim_notes,
        "created_at": _iso(w.created_at), "updated_at": _iso(w.updated_at), "deleted_at": _iso(w.deleted_at),
    }


def _get_or_404(db: Session, warranty_id: str) -> Warranty:
    w = db.execute(select(Warranty).where(Warranty.id == warranty_id, Warranty.deleted_at.is_(None))).scalar_one_or_none()
    if not w:
        raise HTTPException(status_code=404, detail="Warranty not found")
    return w


def _parse_iso_date(value: Any, field_name: str) -> date:
    if value in (None, ""):
        raise HTTPException(status_code=422, detail=f"{field_name} is required")
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field_name} must be YYYY-MM-DD") from None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
def list_warranties(_: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.execute(select(Warranty).where(Warranty.deleted_at.is_(None)).order_by(Warranty.end_date.asc(), Warranty.created_at.desc())).scalars().all()
    return [_serialize(w) for w in rows]


@router.post("", status_code=201)
def create_warranty(request: Request, payload: dict[str, Any] = Body(...), user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "").strip()
    customer_id = str(payload.get("customer_id") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not job_id: raise HTTPException(status_code=422, detail="job_id is required")
    if not customer_id: raise HTTPException(status_code=422, detail="customer_id is required")
    if not description: raise HTTPException(status_code=422, detail="description is required")

    start_date = _parse_iso_date(payload.get("start_date"), "start_date")
    end_date = _parse_iso_date(payload.get("end_date"), "end_date")
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")

    now = _now()
    try:
        w = Warranty(id=str(uuid4()), job_id=job_id, customer_id=customer_id, description=description,
                     start_date=start_date, end_date=end_date, terms=payload.get("terms"),
                     status="active", claim_count=0, created_at=now, updated_at=now)
        db.add(w)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("create_warranty_failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(db=db, tenant_id=tid, user_id=str(user.get("sub") or user.get("user_id") or "system"),
                         action="warranty_created", entity_type="warranty", entity_id=str(w.id),
                         details={"job_id": job_id, "customer_id": customer_id, "description": description})
    db.commit()
    db.refresh(w)
    return _serialize(w)


@router.get("/expiring")
def expiring_warranties(_: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    today = date.today()
    cutoff = today + timedelta(days=30)
    rows = db.execute(
        select(Warranty).where(Warranty.deleted_at.is_(None), Warranty.status == "active",
                                Warranty.end_date >= today, Warranty.end_date <= cutoff)
        .order_by(Warranty.end_date.asc())
    ).scalars().all()
    data = [_serialize(w) for w in rows]
    return {"data": data, "count": len(data)}


@router.get("/{warranty_id}")
def get_warranty(warranty_id: str, _: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _serialize(_get_or_404(db, warranty_id))


@router.patch("/{warranty_id}")
def update_warranty(warranty_id: str, request: Request, payload: dict[str, Any] = Body(...),
                    user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    w = _get_or_404(db, warranty_id)
    if not payload:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    if "status" in payload and payload["status"] not in _ALLOWED_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    start_d = w.start_date if isinstance(w.start_date, date) else date.fromisoformat(str(w.start_date))
    end_d = w.end_date if isinstance(w.end_date, date) else date.fromisoformat(str(w.end_date))

    if "start_date" in payload:
        start_d = _parse_iso_date(payload["start_date"], "start_date")
    if "end_date" in payload:
        end_d = _parse_iso_date(payload["end_date"], "end_date")
    if end_d < start_d:
        raise HTTPException(status_code=422, detail="end_date must be on or after start_date")

    changed = False
    if "description" in payload:
        desc = str(payload["description"] or "").strip()
        if not desc: raise HTTPException(status_code=422, detail="description cannot be blank")
        w.description = desc; changed = True
    if "terms" in payload: w.terms = payload["terms"]; changed = True
    if "status" in payload: w.status = payload["status"]; changed = True
    if "start_date" in payload: w.start_date = start_d; changed = True
    if "end_date" in payload: w.end_date = end_d; changed = True

    if not changed:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    w.updated_at = _now()
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("update_warranty_failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(db=db, tenant_id=tid, user_id=str(user.get("sub") or user.get("user_id") or "system"),
                         action="warranty_updated", entity_type="warranty", entity_id=warranty_id,
                         details={"fields_updated": list(payload.keys())})
    db.commit()
    db.refresh(w)
    return _serialize(w)


@router.delete("/{warranty_id}", status_code=204)
def delete_warranty(warranty_id: str, request: Request, user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    w = _get_or_404(db, warranty_id)
    now = _now()
    w.deleted_at = now
    w.updated_at = now
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("delete_warranty_failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(db=db, tenant_id=tid, user_id=str(user.get("sub") or user.get("user_id") or "system"),
                         action="warranty_deleted", entity_type="warranty", entity_id=warranty_id,
                         details={"warranty_id": warranty_id})
    db.commit()
    return Response(status_code=204)


@router.post("/{warranty_id}/claim")
def file_warranty_claim(warranty_id: str, request: Request, payload: dict[str, Any] = Body(default_factory=dict),
                        user: dict[str, Any] = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    w = _get_or_404(db, warranty_id)
    now = _now()
    try:
        w.claim_count = (w.claim_count or 0) + 1
        w.status = "claimed"
        w.last_claim_at = now
        w.last_claim_notes = payload.get("notes")
        w.updated_at = now
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("file_warranty_claim_failed")
        raise HTTPException(status_code=500, detail="A database error occurred") from None

    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    log_audit_event_sync(db=db, tenant_id=tid, user_id=str(user.get("sub") or user.get("user_id") or "system"),
                         action="warranty_claim_filed", entity_type="warranty", entity_id=warranty_id,
                         details={"warranty_id": warranty_id, "notes": payload.get("notes")})
    db.commit()
    db.refresh(w)
    return _serialize(w)
