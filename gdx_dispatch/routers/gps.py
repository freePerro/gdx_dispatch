"""
GPS router — real-time technician location tracking.

Technicians post location pings from their mobile apps (every 30-60s). Dispatchers
see a live view of all active techs within the last 5 minutes, and can replay the
route a given technician drove on any given UTC calendar date. A stub route
optimization endpoint is exposed so the frontend can wire up the "optimize route"
button ahead of a real routing algorithm.

Pattern mirrors gdx_dispatch/routers/appointments.py (inline model + tenant-scoped queries
+ audit on mutation). Gated behind the "jobs" module.

Note on audit logging: location pings arrive every ~30s per technician. Auditing
every ping would flood the audit table without adding compliance value — the raw
pings ARE the audit trail for location. We therefore skip audit on the ping
endpoint but DO audit the history cleanup endpoint (destructive admin action).
"""
from __future__ import annotations

import logging
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, Numeric, String, Uuid, delete, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from gdx_dispatch.core.audit import TenantBase, log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["gps"],
    dependencies=[Depends(require_module("jobs"))],
)


class TechnicianLocation(TenantBase):
    __tablename__ = "technician_locations"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    company_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tech_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lat: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    lng: Mapped[Decimal] = mapped_column(Numeric(10, 7), nullable=False)
    accuracy_meters: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=True)
    speed_mph: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=True)
    heading_deg: Mapped[int] = mapped_column(Integer, nullable=True)
    battery_percent: Mapped[int] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    timestamp: Mapped[datetime] = mapped_column("timestamp", DateTime(timezone=True), nullable=True)
    accuracy: Mapped[float] = mapped_column(Float, nullable=True)
    battery_pct: Mapped[int] = mapped_column(Integer, nullable=True)
    heading: Mapped[float] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=True)
    technician_id: Mapped[str] = mapped_column(String(36), nullable=True)


class LocationPingIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_meters: float | None = Field(default=None, ge=0, le=100000)
    speed_mph: float | None = Field(default=None, ge=0, le=300)
    heading_deg: int | None = Field(default=None, ge=0, le=360)
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    recorded_at: datetime | None = None


class RouteOptimizeIn(BaseModel):
    tech_id: str = Field(min_length=1, max_length=64)
    job_ids: list[str] = Field(min_length=1, max_length=50)


def _tenant_id(request: Request) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if not isinstance(user, dict):
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _serialize(loc: TechnicianLocation) -> dict[str, Any]:
    return {
        "id": str(loc.id),
        "company_id": loc.company_id,
        "tech_id": loc.tech_id,
        "lat": float(loc.lat) if loc.lat is not None else None,
        "lng": float(loc.lng) if loc.lng is not None else None,
        "accuracy_meters": float(loc.accuracy_meters) if loc.accuracy_meters is not None else None,
        "speed_mph": float(loc.speed_mph) if loc.speed_mph is not None else None,
        "heading_deg": loc.heading_deg,
        "battery_percent": loc.battery_percent,
        "recorded_at": loc.recorded_at.isoformat() if loc.recorded_at else None,
        "created_at": loc.created_at.isoformat() if loc.created_at else None,
    }


def _parse_date(raw: str, field: str) -> date_cls:
    try:
        return date_cls.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid {field} (YYYY-MM-DD required)") from exc


@router.post(
    "/api/gps/technicians/{tech_id}/location",
    response_model=None,
    status_code=201,
)
def post_location_ping(
    tech_id: str,
    payload: LocationPingIn,
    request: Request,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Tech posts a location ping. Called every 30-60s from the mobile app.

    Note: audit logging is intentionally skipped here — the raw pings are
    themselves the authoritative location audit trail. Logging every ping
    would flood the audit table (~2,880 events/tech/day) without compliance
    benefit.
    """
    tenant_id = _tenant_id(request)
    if not tech_id or len(tech_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid tech_id")

    recorded = payload.recorded_at or utcnow()
    if recorded.tzinfo is None:
        recorded = recorded.replace(tzinfo=timezone.utc)

    loc = TechnicianLocation(
        id=uuid4(),
        company_id=tenant_id,
        tech_id=tech_id,
        lat=Decimal(str(payload.lat)),
        lng=Decimal(str(payload.lng)),
        accuracy_meters=Decimal(str(payload.accuracy_meters)) if payload.accuracy_meters is not None else None,
        speed_mph=Decimal(str(payload.speed_mph)) if payload.speed_mph is not None else None,
        heading_deg=payload.heading_deg,
        battery_percent=payload.battery_percent,
        recorded_at=recorded,
        created_at=utcnow(),
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return _serialize(loc)


@router.get("/api/gps/technicians/live", response_model=None)
def live_technicians(
    request: Request,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Dispatcher view: latest ping per active technician within the last 5 minutes."""
    tenant_id = _tenant_id(request)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)

    stmt = (
        select(TechnicianLocation)
        .where(
            TechnicianLocation.company_id == tenant_id,
            TechnicianLocation.recorded_at >= cutoff,
        )
        .order_by(TechnicianLocation.recorded_at.desc())
    )
    rows = db.execute(stmt).scalars().all()

    # Collapse to the most recent ping per tech_id.
    latest: dict[str, TechnicianLocation] = {}
    for r in rows:
        if r.tech_id not in latest:
            latest[r.tech_id] = r

    result: list[dict[str, Any]] = []
    for tech_id, r in latest.items():
        rec = r.recorded_at
        if rec and rec.tzinfo is None:
            rec = rec.replace(tzinfo=timezone.utc)
        age = int((now - rec).total_seconds()) if rec else None
        result.append(
            {
                "tech_id": tech_id,
                "lat": float(r.lat) if r.lat is not None else None,
                "lng": float(r.lng) if r.lng is not None else None,
                "recorded_at": r.recorded_at.isoformat() if r.recorded_at else None,
                "accuracy_meters": float(r.accuracy_meters) if r.accuracy_meters is not None else None,
                "speed_mph": float(r.speed_mph) if r.speed_mph is not None else None,
                "heading_deg": r.heading_deg,
                "age_seconds": age,
            }
        )
    # Stable order: most recently seen first.
    result.sort(key=lambda x: x["age_seconds"] if x["age_seconds"] is not None else 10**9)
    return result


@router.get("/api/gps/technicians/{tech_id}/history", response_model=None)
def tech_history(
    tech_id: str,
    request: Request,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    date: str = Query(..., description="UTC calendar date YYYY-MM-DD"),
) -> dict[str, Any]:
    """All pings for a tech on a given UTC calendar date, chronological. Max 5000 points."""
    tenant_id = _tenant_id(request)
    if not tech_id or len(tech_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid tech_id")
    day = _parse_date(date, "date")

    start_dt = datetime.combine(day, time.min, tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)

    stmt = (
        select(TechnicianLocation)
        .where(
            TechnicianLocation.company_id == tenant_id,
            TechnicianLocation.tech_id == tech_id,
            TechnicianLocation.recorded_at >= start_dt,
            TechnicianLocation.recorded_at < end_dt,
        )
        .order_by(TechnicianLocation.recorded_at.asc())
        .limit(5000)
    )
    rows = db.execute(stmt).scalars().all()
    return {
        "tech_id": tech_id,
        "date": day.isoformat(),
        "count": len(rows),
        "points": [_serialize(r) for r in rows],
    }


@router.post("/api/gps/route-optimize", response_model=None)
def route_optimize(
    payload: RouteOptimizeIn,
    request: Request,
    _user: dict = Depends(get_current_user),
    _db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Stub — returns input order unchanged. Real routing algorithm TBD."""
    _tenant_id(request)  # enforce tenant context even for stubs
    log.debug("route optimization is a stub; returning input order")
    return {
        "tech_id": payload.tech_id,
        "optimized_order": list(payload.job_ids),
    }


@router.delete("/api/gps/technicians/{tech_id}/history", response_model=None)
def cleanup_history(
    tech_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    before: str = Query(..., description="Delete pings recorded before this UTC date YYYY-MM-DD"),
) -> dict[str, Any]:
    """Admin cleanup: delete all pings for tech recorded strictly before `before` date."""
    tenant_id = _tenant_id(request)
    if not tech_id or len(tech_id) > 64:
        raise HTTPException(status_code=422, detail="Invalid tech_id")
    cutoff_date = _parse_date(before, "before")
    cutoff_dt = datetime.combine(cutoff_date, time.min, tzinfo=timezone.utc)

    stmt = delete(TechnicianLocation).where(
        TechnicianLocation.company_id == tenant_id,
        TechnicianLocation.tech_id == tech_id,
        TechnicianLocation.recorded_at < cutoff_dt,
    )
    result = db.execute(stmt)
    deleted_count = int(result.rowcount or 0)
    db.commit()

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action="gps_history_cleanup",
            entity_type="technician_location",
            entity_id=tech_id,
            details={"before": cutoff_date.isoformat(), "deleted_count": deleted_count},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception(
            "gps_history_cleanup_audit_failed tech_id=%s before=%s", tech_id, before
        )
        db.rollback()

    return {"deleted_count": deleted_count}
