"""Dispatch scheduling endpoints — traffic-aware scheduling, on-my-way, capacity.

Routes:
  GET  /api/dispatch/schedule-with-traffic — optimized schedule with drive times
  POST /api/jobs/{job_id}/on-my-way — send ETA to customer
  GET  /api/dispatch/check-capacity — overbooking prevention
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["dispatch-scheduling"],
    dependencies=[Depends(require_module("dispatch"))],
)


def _tenant_id(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


# ---------------------------------------------------------------------------
# Traffic-Aware Scheduling (#174)
# ---------------------------------------------------------------------------

@router.get("/api/dispatch/schedule-with-traffic")
def schedule_with_traffic(
    tech_id: str,
    date: str,
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Get tech's schedule with drive time estimates between jobs."""
    tenant_id = _tenant_id(request)
    try:
        from datetime import date as _date_type

        from sqlalchemy import select as _select

        from gdx_dispatch.models.tenant_models import Customer, Job
        target = _date_type.fromisoformat(date) if isinstance(date, str) else date
        day_start = datetime(target.year, target.month, target.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        rows = db.execute(
            _select(Job, Customer.name.label("customer_name"), Customer.address.label("customer_address"))
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .where(Job.company_id == tenant_id, Job.scheduled_at >= day_start, Job.scheduled_at < day_end, Job.deleted_at.is_(None))
            .order_by(Job.scheduled_at)
        ).all()
        jobs = [{"id": str(j.id), "title": j.title, "scheduled_at": str(j.scheduled_at) if j.scheduled_at else None,
                 "customer_name": cname, "customer_address": caddr} for j, cname, caddr in rows]

        schedule = []
        for i, job in enumerate(jobs):
            entry = {
                "job_id": str(job["id"]),
                "title": job["title"],
                "customer_name": job["customer_name"],
                "address": job["customer_address"],
                "scheduled_at": str(job["scheduled_at"]) if job["scheduled_at"] else None,
                "drive_time_minutes": None,
                "order": i + 1,
            }

            # Try Google Maps for drive time
            if i > 0 and job["customer_address"] and jobs[i - 1]["customer_address"]:
                try:
                    import googlemaps
                    gmaps = googlemaps.Client(key=os.getenv("GOOGLE_MAPS_API_KEY", ""))
                    result = gmaps.distance_matrix(
                        origins=[jobs[i - 1]["customer_address"]],
                        destinations=[job["customer_address"]],
                        mode="driving",
                        departure_time=datetime.now(timezone.utc),
                    )
                    if result["rows"][0]["elements"][0]["status"] == "OK":
                        entry["drive_time_minutes"] = result["rows"][0]["elements"][0]["duration"]["value"] // 60
                except Exception:
                    log.exception("google_maps_drive_time_failed")

            schedule.append(entry)

        return {"date": date, "tech_id": tech_id, "jobs": schedule, "total_jobs": len(schedule)}

    except Exception:
        log.exception("schedule_with_traffic_failed")
        raise HTTPException(status_code=500, detail="Failed to get schedule") from None


# ---------------------------------------------------------------------------
# On My Way (#177)
# ---------------------------------------------------------------------------

class OnMyWayIn(BaseModel):
    tech_lat: float | None = None
    tech_lng: float | None = None


@router.post("/api/jobs/{job_id}/on-my-way")
def on_my_way(
    job_id: str,
    payload: OnMyWayIn | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """Notify customer that tech is on the way with ETA."""
    tenant_id = _tenant_id(request)
    try:
        from sqlalchemy import select as _select

        from gdx_dispatch.models.tenant_models import Customer, Job
        row = db.execute(
            _select(Job, Customer.name.label("customer_name"), Customer.phone.label("customer_phone"),
                    Customer.address.label("customer_address"))
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .where(Job.id == job_id, Job.company_id == tenant_id)
        ).first()

        if not row:
            raise HTTPException(status_code=404, detail="Job not found")
        j, cname, cphone, caddr = row
        job = {"id": str(j.id), "title": j.title, "customer_name": cname, "customer_phone": cphone, "customer_address": caddr}

        eta_minutes = None
        map_link = None

        # Calculate ETA via Google Maps if coordinates provided
        if payload and payload.tech_lat and payload.tech_lng and job["customer_address"]:
            try:
                import googlemaps
                gmaps = googlemaps.Client(key=os.getenv("GOOGLE_MAPS_API_KEY", ""))
                result = gmaps.distance_matrix(
                    origins=[f"{payload.tech_lat},{payload.tech_lng}"],
                    destinations=[job["customer_address"]],
                    mode="driving",
                    departure_time=datetime.now(timezone.utc),
                )
                if result["rows"][0]["elements"][0]["status"] == "OK":
                    eta_minutes = result["rows"][0]["elements"][0]["duration"]["value"] // 60
                    map_link = f"https://www.google.com/maps/dir/{payload.tech_lat},{payload.tech_lng}/{job['customer_address'].replace(' ', '+')}"
            except Exception:
                log.exception("on_my_way_maps_failed")

        # Send SMS to customer
        sms_sent = False
        if job["customer_phone"]:
            try:
                from gdx_dispatch.core import sms as sms_service
                eta_text = f" ETA: ~{eta_minutes} minutes." if eta_minutes else ""
                msg = f"Your technician is on the way!{eta_text}"
                if map_link:
                    msg += f" Track: {map_link}"
                import os as _os
                from_phone = _os.getenv("TWILIO_PHONE_NUMBER", "").strip()
                sms_service.send_sms(
                    to_phone=job["customer_phone"],
                    body=msg,
                    from_phone=from_phone,
                    tenant_id=tenant_id,
                )
                sms_sent = True
            except Exception:
                log.exception("on_my_way_sms_failed")

        log_audit_event_sync(
            db=db, tenant_id=tenant_id,
            user_id=str(user.get("sub") or user.get("user_id") or "system"),
            action="on_my_way_sent", entity_type="job", entity_id=job_id,
            details={"eta_minutes": eta_minutes, "sms_sent": sms_sent},
            request=request,
        )
        db.commit()

        return {
            "job_id": job_id,
            "eta_minutes": eta_minutes,
            "map_link": map_link,
            "sms_sent": sms_sent,
            "customer_name": job["customer_name"],
        }

    except HTTPException:
        raise
    except Exception:
        log.exception("on_my_way_failed")
        raise HTTPException(status_code=500, detail="Failed to send on-my-way notification") from None


# ---------------------------------------------------------------------------
# Scheduled — Not Assigned lane (2026-05-01)
# ---------------------------------------------------------------------------
# Surfaces upcoming jobs (after today) that have a scheduled_at but no tech.
# Today's scheduled-no-tech jobs already appear in the per-day "Unassigned"
# column on the Dispatch board; this is the forward-looking view so they
# don't slip past the dispatcher's eyes.

@router.get("/api/dispatch/scheduled-unassigned")
def scheduled_unassigned(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user, request
    from sqlalchemy import text as _text
    rows = db.execute(
        _text(
            # 2026-05-01 (round 2) — surface ALL scheduled-no-tech jobs,
            # not just future. A salesperson can pencil in an asap job
            # without knowing the dispatcher's day; the lane is where
            # those land so they don't get missed.
            "SELECT j.id, j.job_number, j.title, j.scheduled_at, j.priority, "
            "       j.customer_id, c.name AS customer_name "
            "FROM jobs j "
            "LEFT JOIN customers c ON c.id = j.customer_id "
            "WHERE j.scheduled_at IS NOT NULL "
            "  AND j.assigned_to IS NULL "
            "  AND j.holding_area_id IS NULL "
            "  AND COALESCE(j.lifecycle_stage::text, '') NOT IN ('cancelled', 'completed') "
            "ORDER BY j.scheduled_at ASC "
            "LIMIT 500"
        )
    ).all()
    return {
        "items": [
            {
                "id": str(r[0]),
                "job_number": r[1],
                "title": r[2],
                "scheduled_at": r[3].isoformat() if r[3] else None,
                "priority": r[4],
                "customer_id": str(r[5]) if r[5] else None,
                "customer_name": r[6],
            }
            for r in rows
        ]
    }
