"""Sprint 5 / S5-C — GPS breadcrumb router.

POST /api/mobile/location — tech app pushes a breadcrumb. Refused if no
open clock-in entry (privacy boundary, S5-C4).

GET /api/dispatch/locations — dispatch reads latest position per tech for
the live truck map (S5-C2).

GET /api/jobs/{job_id}/arrival-check — auto-arrival evaluation
(S5-C3); returns whether the latest tech location has been within the
configured radius of the job's address for the configured dwell time.
"""
from __future__ import annotations

import logging
import math
import uuid as _uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text as _text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting
from gdx_dispatch.models.tenant_models import CustomerLocation, Job, TechLocation, TimeclockEntry
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["tech-locations"])


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)
    speed_mps: float | None = Field(default=None, ge=0)
    heading_deg: float | None = Field(default=None, ge=0, le=360)
    job_id: str | None = None
    recorded_at: datetime | None = None


class LocationOut(BaseModel):
    id: str
    user_id: str
    technician_id: str | None
    job_id: str | None
    lat: float
    lng: float
    accuracy_m: float | None
    recorded_at: str


def _user_id(current_user: Any) -> str:
    user = current_user or {}
    return str(user.get("user_id") or user.get("sub") or "")


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id") or "")


def _technician_id(current_user: Any) -> str | None:
    user = current_user or {}
    return user.get("technician_id") or user.get("tech_id")


def _open_clock_in(db: Session, user_id: str) -> TimeclockEntry | None:
    """Return user's currently open clock-in entry, if any."""
    if not user_id:
        return None
    return db.execute(
        select(TimeclockEntry)
        .where(
            TimeclockEntry.technician_id == user_id,
            TimeclockEntry.clock_out_at.is_(None),
            TimeclockEntry.deleted_at.is_(None),
        )
        .order_by(TimeclockEntry.clock_in_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _to_response(loc: TechLocation) -> LocationOut:
    return LocationOut(
        id=str(loc.id),
        user_id=loc.user_id,
        technician_id=loc.technician_id,
        job_id=str(loc.job_id) if loc.job_id else None,
        lat=float(loc.lat),
        lng=float(loc.lng),
        accuracy_m=float(loc.accuracy_m) if loc.accuracy_m is not None else None,
        recorded_at=loc.recorded_at.isoformat() if loc.recorded_at else "",
    )


@router.post("/api/mobile/location", response_model=LocationOut, status_code=201)
def post_location(
    payload: LocationIn,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LocationOut:
    _ = _tenant_id(request)
    user_id = _user_id(current_user)
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")

    # Feature gate: master switch.
    if not bool(get_tenant_mobile_setting(db, "tech_mobile.gps_breadcrumb_enabled", request=request)):
        raise HTTPException(status_code=403, detail="GPS breadcrumb disabled for tenant")

    # Privacy boundary (S5-C4): only sample while clocked in.
    if not _open_clock_in(db, user_id):
        raise HTTPException(status_code=403, detail="Not clocked in")

    job_uuid: _uuid.UUID | None = None
    if payload.job_id:
        try:
            job_uuid = _uuid.UUID(payload.job_id)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid job_id") from None

    recorded_at = payload.recorded_at or datetime.now(UTC)
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)

    try:
        loc = TechLocation(
            id=uuid4(),
            user_id=user_id,
            technician_id=_technician_id(current_user),
            job_id=job_uuid,
            lat=Decimal(str(payload.lat)),
            lng=Decimal(str(payload.lng)),
            accuracy_m=Decimal(str(payload.accuracy_m)) if payload.accuracy_m is not None else None,
            speed_mps=Decimal(str(payload.speed_mps)) if payload.speed_mps is not None else None,
            heading_deg=Decimal(str(payload.heading_deg)) if payload.heading_deg is not None else None,
            recorded_at=recorded_at,
        )
        db.add(loc)
        db.commit()
        db.refresh(loc)
        return _to_response(loc)
    except SQLAlchemyError:
        db.rollback()
        log.exception("location_post_failed", extra={"user_id": user_id})
        raise HTTPException(status_code=500, detail="Failed to record location") from None


@router.get("/api/dispatch/locations", response_model=list[LocationOut])
def latest_locations(
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    minutes: int = 30,
) -> list[LocationOut]:
    """Return the most recent breadcrumb per tech within the last `minutes`."""
    _ = current_user
    _ = request
    cutoff = datetime.now(UTC) - timedelta(minutes=max(1, min(minutes, 1440)))

    try:
        # Portable "latest row per user_id" via correlated subquery on max
        # recorded_at. Avoids DISTINCT ON (PG-only) and ROW_NUMBER (SQLite
        # window-function quirks) so the same query runs in pytest's
        # SQLite tenant_db fixture.
        rows = db.execute(
            _text(
                "SELECT tl.id, tl.user_id, tl.technician_id, tl.job_id, "
                "       tl.lat, tl.lng, tl.accuracy_m, tl.recorded_at "
                "FROM tech_locations tl "
                "INNER JOIN ("
                "  SELECT user_id, MAX(recorded_at) AS max_at "
                "  FROM tech_locations "
                "  WHERE recorded_at >= :cutoff "
                "  GROUP BY user_id"
                ") latest "
                "  ON latest.user_id = tl.user_id "
                "  AND latest.max_at = tl.recorded_at "
                "WHERE tl.recorded_at >= :cutoff "
                "ORDER BY tl.recorded_at DESC"
            ),
            {"cutoff": cutoff},
        ).mappings().all()
    except SQLAlchemyError:
        log.exception("latest_locations_failed")
        raise HTTPException(status_code=500, detail="Failed to read locations") from None

    out: list[LocationOut] = []
    for r in rows:
        out.append(
            LocationOut(
                id=str(r["id"]),
                user_id=str(r["user_id"]),
                technician_id=r["technician_id"],
                job_id=str(r["job_id"]) if r["job_id"] else None,
                lat=float(r["lat"]),
                lng=float(r["lng"]),
                accuracy_m=float(r["accuracy_m"]) if r["accuracy_m"] is not None else None,
                recorded_at=r["recorded_at"].isoformat() if r["recorded_at"] else "",
            )
        )
    return out


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distance in meters between two lat/lng points."""
    r = 6_371_000.0
    a = math.radians(lat1)
    b = math.radians(lat2)
    da = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(da / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class ArrivalCheckOut(BaseModel):
    should_prompt: bool
    distance_m: float | None
    dwell_seconds: int | None
    threshold_m: int
    dwell_required_seconds: int
    reason: str | None = None


@router.get("/api/jobs/{job_id}/arrival-check", response_model=ArrivalCheckOut)
def arrival_check(
    job_id: str,
    request: Request,
    current_user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ArrivalCheckOut:
    """Check whether the calling user is within the arrival radius of the
    job's customer address for the dwell time. Frontend polls this on a
    timer; if `should_prompt` flips true, surface the 'Mark arrived?' prompt.
    """
    _ = _tenant_id(request)
    user_id = _user_id(current_user)
    try:
        job_uuid = _uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Job not found") from None

    threshold_m = int(get_tenant_mobile_setting(db, "tech_mobile.gps_arrival_distance_m", request=request))
    dwell_required = int(get_tenant_mobile_setting(db, "tech_mobile.gps_arrival_dwell_seconds", request=request))

    job = db.execute(
        select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Resolve customer geocode via the primary CustomerLocation row.
    customer_lat = None
    customer_lng = None
    if job.customer_id:
        loc_row = db.execute(
            select(CustomerLocation)
            .where(
                CustomerLocation.customer_id == str(job.customer_id),
                CustomerLocation.deleted_at.is_(None),
            )
            .order_by(CustomerLocation.is_primary.desc().nullslast(), CustomerLocation.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        if loc_row and loc_row.lat is not None and loc_row.lng is not None:
            customer_lat = float(loc_row.lat)
            customer_lng = float(loc_row.lng)
    if customer_lat is None or customer_lng is None:
        return ArrivalCheckOut(
            should_prompt=False,
            distance_m=None,
            dwell_seconds=None,
            threshold_m=threshold_m,
            dwell_required_seconds=dwell_required,
            reason="customer_address_not_geocoded",
        )

    cutoff = datetime.now(UTC) - timedelta(seconds=dwell_required)
    rows = db.execute(
        select(TechLocation)
        .where(
            TechLocation.user_id == user_id,
            TechLocation.recorded_at >= cutoff,
        )
        .order_by(TechLocation.recorded_at.desc())
    ).scalars().all()

    if not rows:
        return ArrivalCheckOut(
            should_prompt=False,
            distance_m=None,
            dwell_seconds=None,
            threshold_m=threshold_m,
            dwell_required_seconds=dwell_required,
            reason="no_recent_breadcrumbs",
        )

    inside_throughout = True
    latest = rows[0]
    earliest_inside = latest.recorded_at
    for loc in rows:
        d = _haversine_m(float(loc.lat), float(loc.lng), float(customer_lat), float(customer_lng))
        if d > threshold_m:
            inside_throughout = False
            break
        earliest_inside = loc.recorded_at

    latest_d = _haversine_m(
        float(latest.lat), float(latest.lng), float(customer_lat), float(customer_lng)
    )
    dwell_seconds = int((latest.recorded_at - earliest_inside).total_seconds())

    return ArrivalCheckOut(
        should_prompt=bool(inside_throughout and dwell_seconds >= dwell_required),
        distance_m=latest_d,
        dwell_seconds=dwell_seconds,
        threshold_m=threshold_m,
        dwell_required_seconds=dwell_required,
        reason=None if inside_throughout else "left_radius_during_window",
    )
