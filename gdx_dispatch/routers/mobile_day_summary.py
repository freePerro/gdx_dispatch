"""Sprint tech_mobile Phase 4.2 (S4-B3) — End-of-day summary for techs.

GET /api/mobile/day-summary?date=YYYY-MM-DD → KPIs for that date for the
calling user (their assigned jobs, their time entries, their parts
requests, their invoices).

Mirrors industry pattern (Housecall "Day at a glance", ServiceTitan
Technician Performance) — informational, no clock-out modal, accessible
both from /mobile/today's wrap card AND a dedicated /mobile/summary
route.
"""
from __future__ import annotations

import logging
from datetime import UTC, date as _date, datetime, timedelta
from typing import Any
from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import text as _text

from gdx_dispatch.core.pii import decrypt_if_ciphertext
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_day_summary_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile-day-summary"],
    dependencies=[Depends(require_module("mobile"))],
)


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "")


@router.get("/day-summary", response_model=None)
def day_summary(
    request: Request,
    date: str | None = Query(default=None, description="YYYY-MM-DD; default = today"),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    user_id = _user_id(user)
    if not user_id:
        return JSONResponse({"detail": "no user"}, 401)

    target = _date.today()
    if date:
        try:
            target = _date.fromisoformat(date)
        except ValueError:
            return JSONResponse({"detail": "invalid date format; expected YYYY-MM-DD"}, 400)

    day_start = datetime.combine(target, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    # Jobs the user finished today (assigned_to OR job_assignment).
    jobs_done = db.execute(
        _text(  # noqa: RAW_ENC — c.address decrypted via decrypt_if_ciphertext below
            """
            SELECT j.id, j.title, j.completed_at,
                   c.name AS customer_name, c.address AS customer_address
            FROM jobs j
            LEFT JOIN customers c ON c.id = j.customer_id
            WHERE j.deleted_at IS NULL
              AND j.completed_at >= :start AND j.completed_at < :end
              AND (
                j.assigned_to = :uid
                OR EXISTS (
                  SELECT 1 FROM job_assignments ja
                  JOIN technicians t ON t.id = ja.tech_id
                  WHERE CAST(ja.job_id AS TEXT) = CAST(j.id AS TEXT) AND CAST(t.user_id AS TEXT) = :uid
                )
              )
            ORDER BY j.completed_at ASC
            """
        ),
        {"uid": user_id, "start": day_start, "end": day_end},
    ).all()

    # Hours from timeclock entries for today.
    # SQLite test path uses julianday(); PG uses EXTRACT EPOCH. Try
    # the dialect-appropriate one based on the SQLAlchemy bind.
    is_sqlite = db.bind is not None and db.bind.dialect.name == "sqlite"
    # Table is `time_entries` — the one that actually carries populated
    # user_id / clock_in / clock_out (mobile.py's _create_time_entry writes
    # it, payroll reads it). The old "timeclock_entries" was a nonexistent
    # table (query raised UndefinedTable, swallowed → 0 hours); `timeclocks`
    # exists but its legacy clock_in/clock_out/user_id columns are never
    # written (0 rows), so it would keep reporting 0. Found + corrected in
    # the 2026-07-15 full-app walk (audit round 2).
    if is_sqlite:
        hours_sql = (
            "SELECT COALESCE(SUM((julianday(clock_out) - julianday(clock_in)) * 24.0), 0) "
            "FROM time_entries WHERE CAST(user_id AS TEXT) = :uid "
            "AND clock_in >= :start AND clock_in < :end AND clock_out IS NOT NULL"
        )
    else:
        hours_sql = (
            "SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (clock_out - clock_in)) / 3600.0), 0) "
            "FROM time_entries WHERE CAST(user_id AS TEXT) = :uid "
            "AND clock_in >= :start AND clock_in < :end AND clock_out IS NOT NULL"
        )
    try:
        labor_hours = float(db.execute(
            _text(hours_sql),
            {"uid": user_id, "start": day_start, "end": day_end},
        ).scalar() or 0)
    except Exception:
        log.exception("day_summary_hours_unavailable user=%s", user_id)
        try: db.rollback()
        except Exception: pass
        labor_hours = 0.0

    # Parts requested today by this user.
    try:
        parts_requested = int(db.execute(
            _text(
                # Table is `job_parts_needed`, attribution column is
                # `requested_by_user_id`, and it has no deleted_at (soft state
                # lives in `status`). The old names raised UndefinedTable so
                # the day-wrap always reported 0 parts. (2026-07-15 walk.)
                """
                SELECT COUNT(*) FROM job_parts_needed
                WHERE requested_by_user_id = :uid
                  AND created_at >= :start AND created_at < :end
                """
            ),
            {"uid": user_id, "start": day_start, "end": day_end},
        ).scalar() or 0)
    except Exception:
        log.exception("day_summary_parts_unavailable user=%s", user_id)
        try: db.rollback()
        except Exception: pass
        parts_requested = 0

    # Invoices generated from this user's mobile actions today.
    try:
        invoices_count = int(db.execute(
            _text(
                """
                SELECT COUNT(*) FROM invoices i
                WHERE i.deleted_at IS NULL
                  AND i.created_at >= :start AND i.created_at < :end
                  AND CAST(i.job_id AS TEXT) IN (
                    SELECT CAST(j.id AS TEXT) FROM jobs j WHERE j.assigned_to = :uid
                    UNION
                    SELECT CAST(ja.job_id AS TEXT) FROM job_assignments ja
                    JOIN technicians t ON t.id = ja.tech_id
                    WHERE CAST(t.user_id AS TEXT) = :uid
                  )
                """
            ),
            {"uid": user_id, "start": day_start, "end": day_end},
        ).scalar() or 0)
    except Exception:
        log.exception("day_summary_invoices_unavailable user=%s", user_id)
        try: db.rollback()
        except Exception: pass
        invoices_count = 0

    revenue_invoiced = 0.0
    try:
        revenue_invoiced = float(db.execute(
            _text(
                """
                SELECT COALESCE(SUM(i.total), 0) FROM invoices i
                WHERE i.deleted_at IS NULL
                  AND i.created_at >= :start AND i.created_at < :end
                  AND CAST(i.job_id AS TEXT) IN (
                    SELECT CAST(j.id AS TEXT) FROM jobs j WHERE j.assigned_to = :uid
                    UNION
                    SELECT CAST(ja.job_id AS TEXT) FROM job_assignments ja
                    JOIN technicians t ON t.id = ja.tech_id
                    WHERE CAST(t.user_id AS TEXT) = :uid
                  )
                """
            ),
            {"uid": user_id, "start": day_start, "end": day_end},
        ).scalar() or 0)
    except Exception:
        log.exception("day_summary_revenue_unavailable user=%s", user_id)
        try: db.rollback()
        except Exception: pass

    # Tomorrow's first stop (peek-ahead — small affordance).
    tomorrow_start = day_start + timedelta(days=1)
    tomorrow_end = tomorrow_start + timedelta(days=1)
    next_first = db.execute(
        _text(  # noqa: RAW_ENC — c.address decrypted via decrypt_if_ciphertext below
            """
            SELECT j.id, j.title, j.scheduled_at,
                   c.name AS customer_name, c.address AS customer_address
            FROM jobs j
            LEFT JOIN customers c ON c.id = j.customer_id
            WHERE j.deleted_at IS NULL
              AND j.scheduled_at >= :start AND j.scheduled_at < :end
              AND (
                j.assigned_to = :uid
                OR EXISTS (
                  SELECT 1 FROM job_assignments ja
                  JOIN technicians t ON t.id = ja.tech_id
                  WHERE CAST(ja.job_id AS TEXT) = CAST(j.id AS TEXT) AND CAST(t.user_id AS TEXT) = :uid
                )
              )
            ORDER BY j.scheduled_at ASC
            LIMIT 1
            """
        ),
        {"uid": user_id, "start": tomorrow_start, "end": tomorrow_end},
    ).first()

    return JSONResponse({
        "date": target.isoformat(),
        "user_id": user_id,
        "jobs_completed": [
            {
                "id": str(r[0]),
                "title": r[1],
                "completed_at": r[2].isoformat() if r[2] else None,
                "customer_name": r[3],
                "customer_address": decrypt_if_ciphertext(r[4]),
            }
            for r in jobs_done
        ],
        "jobs_completed_count": len(jobs_done),
        "labor_hours": round(labor_hours, 2),
        "parts_requested_count": parts_requested,
        "invoices_count": invoices_count,
        "revenue_invoiced": round(revenue_invoiced, 2),
        "next_first_stop": (
            {
                "id": str(next_first[0]),
                "title": next_first[1],
                "scheduled_at": next_first[2].isoformat() if next_first[2] else None,
                "customer_name": next_first[3],
                "customer_address": decrypt_if_ciphertext(next_first[4]),
            }
            if next_first else None
        ),
    })
