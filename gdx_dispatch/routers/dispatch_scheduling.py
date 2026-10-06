"""Dispatch scheduling endpoints — traffic-aware scheduling, capacity.

Routes:
  GET  /api/dispatch/schedule-with-traffic — optimized schedule with drive times
  GET  /api/dispatch/check-capacity — overbooking prevention
  GET  /api/dispatch/late-open — open jobs whose scheduled day has passed
  GET  /api/dispatch/partial-jobs — a day worked, no day booked
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
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
            "       j.customer_id, c.name AS customer_name, j.is_return_visit "
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
                "is_return_visit": bool(r[7]),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Past their date, not closed out (2026-09-27)
# ---------------------------------------------------------------------------
# A job whose scheduled day has passed without a closeout was on no screen at
# all: the board fetches only undated jobs and the dates in view, so the day
# after its visit an open job simply stopped being loaded. On 2026-09-27 prod
# had six of them, the oldest scheduled 2026-05-13 and one a broken spring
# waiting since 2026-06-02 — found by SQL, not by anyone looking at the board. This is the list that makes that
# state visible on the screen dispatch already has open.
#
# "Past" is judged on the shop's calendar day, not UTC and not the instant:
# a job booked for 8am today is not late at 9am, and a 7pm Minnesota job is
# already tomorrow in UTC. Everything before the start of today, shop time.
# Holding area, tech and job type are deliberately NOT filters — a stale
# Ready-to-Schedule stamp is how several of these hid in the first place.

def _board_row(job: Any, customer_name: str | None, ds_map: dict[str, Any]) -> dict[str, Any]:
    """The fields a board card reads, shared by the late-open and partial queues."""
    at = job.scheduled_at
    return {
        "id": str(job.id),
        "job_number": job.job_number,
        "title": job.title,
        "job_type": job.job_type,
        "status": job.status,
        "lifecycle_stage": job.lifecycle_stage,
        # SQLite hands back a naive (UTC) value; say so, or the browser
        # reads it as local time.
        "scheduled_at": (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).isoformat() if at else None,
        "customer_id": str(job.customer_id) if job.customer_id else None,
        "customer_name": customer_name,
        "assigned_to": job.assigned_to,
        "is_return_visit": bool(job.is_return_visit),
        "display_state": ds_map.get(str(job.id)),
    }


@router.get("/api/dispatch/late-open", dependencies=[Depends(require_permission("jobs.read_all"))])
def late_open_jobs(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    from zoneinfo import ZoneInfo

    from sqlalchemy import select as _select

    from gdx_dispatch.core.pay_periods import resolve_zone, shop_day_of, shop_today, shop_tz_name_from_settings
    from gdx_dispatch.models.tenant_models import Customer, Job, Technician
    from gdx_dispatch.services.visit_sync import partial_clause

    tz_name = shop_tz_name_from_settings(db)
    zone: ZoneInfo = resolve_zone(tz_name)
    today = shop_today(tz_name)
    start_of_today = datetime(today.year, today.month, today.day, tzinfo=zone).astimezone(timezone.utc)

    rows = db.execute(
        _select(Job, Customer.name, Technician.name)
        .outerjoin(Customer, Job.customer_id == Customer.id)
        .outerjoin(Technician, Job.assigned_to == Technician.id)
        .where(
            Job.deleted_at.is_(None),
            Job.scheduled_at.is_not(None),
            Job.scheduled_at < start_of_today,
            Job.lifecycle_stage.notin_(["completed", "cancelled"]),
            # A day of it was worked and nothing is booked: that job is in
            # Partial Jobs (below), and one job sits in one queue.
            ~partial_clause(Job.id),
        )
        # Oldest first — the customer who has waited longest.
        .order_by(Job.scheduled_at.asc())
        .limit(200)
    ).all()

    # The board's JobStateChip reads display_state; without it every row
    # renders "unverified — refresh to sync", which no refresh ever fixes.
    # Same helper and same degrade-to-empty contract as the jobs list.
    try:
        from gdx_dispatch.routers.jobs import _display_state_for_jobs

        ds_map = _display_state_for_jobs(db, [(job.id, job.lifecycle_stage) for job, _c, _t in rows])
    except Exception:
        log.exception("late_open_display_state_failed")
        ds_map = {}

    items = []
    for job, customer_name, tech_name in rows:
        day = shop_day_of(job.scheduled_at, tz_name)
        items.append({
            **_board_row(job, customer_name, ds_map),
            "days_late": (today - day).days if day else None,
            "tech_name": tech_name,
        })
    return {"items": items, "today": today.isoformat(), "timezone": tz_name}


# ---------------------------------------------------------------------------
# Partial Jobs — Need to Schedule (multi-day jobs plan §5.3a, D10–D12)
# ---------------------------------------------------------------------------
# A job a tech worked one day of, and stopped, with no next day booked. It
# has a date (the day it was worked), so it is in neither "New Jobs" (no
# date, or no tech) nor on any day ahead; and it is not late, because no one
# was booked to come back. The board shows it here until a drop books the
# next day. Membership is ``partial_clause``, the predicate late-open leaves
# out, so the two queues cannot both hold a job.

@router.get("/api/dispatch/partial-jobs", dependencies=[Depends(require_permission("jobs.read_all"))])
def partial_jobs(
    db: Session = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    _ = user
    from sqlalchemy import select as _select

    from gdx_dispatch.core.pay_periods import shop_day_of, shop_tz_name_from_settings
    from gdx_dispatch.models.tenant_models import Appointment, Customer, Job, Technician
    from gdx_dispatch.services.visit_sync import partial_clause, visit_state

    tz_name = shop_tz_name_from_settings(db)
    rows = db.execute(
        _select(Job, Customer.name)
        .outerjoin(Customer, Job.customer_id == Customer.id)
        .where(
            Job.deleted_at.is_(None),
            Job.lifecycle_stage.notin_(["completed", "cancelled"]),
            partial_clause(Job.id),
        )
        .order_by(Job.scheduled_at.asc().nulls_last())
        .limit(200)
    ).all()
    job_ids = [job.id for job, _c in rows]

    # The days worked and who worked them, from the closed visits.
    worked: dict[str, dict[str, Any]] = {}
    if job_ids:
        visits = db.execute(
            _select(Appointment).where(Appointment.job_id.in_(job_ids), Appointment.deleted_at.is_(None))
        ).scalars().all()
        tech_ids = {v.tech_id for v in visits if v.tech_id}
        names = {
            str(t.id): t.name for t in db.execute(_select(Technician).where(Technician.id.in_(tech_ids))).scalars().all()
        } if tech_ids else {}
        for v in visits:
            if visit_state(v) != "closed":
                continue
            entry = worked.setdefault(str(v.job_id), {"last": None, "techs": {}, "closed": {}})
            day = shop_day_of(v.start_at, tz_name)
            if day is None:
                continue
            # Every (day, tech) with a closed visit: E5 books nothing for a
            # drop on one, so the board refuses it. A visit can close early
            # on a later day than today, so the last day alone is not enough.
            entry["closed"].setdefault(day.isoformat(), set()).add(str(v.tech_id) if v.tech_id else None)
            if entry["last"] is not None and day < entry["last"]:
                continue
            if entry["last"] is None or day > entry["last"]:
                entry["last"], entry["techs"] = day, {}  # worked_by: that day's techs
            if v.tech_id:
                entry["techs"][str(v.tech_id)] = names.get(str(v.tech_id))

    try:
        from gdx_dispatch.routers.jobs import _display_state_for_jobs

        ds_map = _display_state_for_jobs(db, [(job.id, job.lifecycle_stage) for job, _c in rows])
    except Exception:
        log.exception("partial_jobs_display_state_failed")
        ds_map = {}

    items = []
    for job, customer_name in rows:
        entry = worked.get(str(job.id), {"last": None, "techs": {}, "closed": {}})
        items.append({
            **_board_row(job, customer_name, ds_map),
            "holding_area_id": job.holding_area_id,
            "last_worked_day": entry["last"].isoformat() if entry["last"] else None,
            "worked_by": [
                {"tech_id": tid, "name": name} for tid, name in entry["techs"].items()
            ],
            "closed_days": {d: sorted(t for t in techs if t) for d, techs in sorted(entry["closed"].items())},
        })
    return {"items": items, "timezone": tz_name}
