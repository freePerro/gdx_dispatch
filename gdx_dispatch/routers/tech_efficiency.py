"""Tech efficiency report — daily + weekly leaderboards.

Sprint dispatch-capacity (2026-05-20). Surfaces "how much each tech beat
their scheduled time" so dispatch can plan around real velocity and the
shop can build a bonus structure on top.

Efficiency ratio = sum(scheduled_duration_hours) / sum(closeout.hours_worked)
over completed jobs (job_closeouts row exists) in the window. Higher = the
tech finished faster than the scheduler's estimate.

Credit is assigned to the LEAD tech on each job; if no lead is marked,
the first assigned tech receives the credit. Jobs without
``scheduled_duration_hours`` are excluded (no estimate to beat) so the
ratio stops being inflated by zero-baseline rows.

Two windows: ``daily`` (today UTC) and ``weekly`` (current ISO week,
Mon–Sun, UTC). Timezone-correct boundaries are a follow-up — v1 chooses
the smaller-blast-radius path.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import text as _text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission

log = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/reports/tech-efficiency",
    tags=["reports"],
    dependencies=[Depends(require_module("dispatch"))],
)


def _today_window_utc() -> tuple[datetime, datetime]:
    today = datetime.now(UTC).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


def _iso_week_window_utc() -> tuple[datetime, datetime]:
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())  # Monday
    start = datetime.combine(monday, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=7)


def _query_efficiency(
    db: Session, window_start: datetime, window_end: datetime,
) -> list[dict[str, Any]]:
    """Aggregate (lead-tech) scheduled vs actual hours over the window.

    Lead resolution: prefer ``job_assignments.is_lead = TRUE``; fall back
    to the first assignment row (oldest ``assigned_at``); ultimately the
    legacy ``jobs.assigned_to`` if no assignments exist. Jobs with no
    ``scheduled_duration_hours`` are filtered out so the ratio reflects
    only jobs the scheduler actually estimated.
    """
    sql = _text(
        """
        WITH closed_in_window AS (
            SELECT
                jc.job_id,
                jc.hours_worked,
                j.scheduled_duration_hours,
                j.assigned_to
            FROM job_closeouts jc
            JOIN jobs j ON j.id = jc.job_id
            WHERE jc.deleted_at IS NULL
              AND j.deleted_at IS NULL
              AND jc.closed_at >= :start
              AND jc.closed_at <  :end
              AND j.scheduled_duration_hours IS NOT NULL
              AND jc.hours_worked > 0
        ),
        lead_for_job AS (
            SELECT DISTINCT ON (ja.job_id)
                ja.job_id,
                ja.tech_id
            FROM job_assignments ja
            -- job_assignments.job_id is VARCHAR while job_closeouts/jobs use
            -- UUID, so this join must cast both sides to TEXT (same as the
            -- technicians join below) or Postgres errors with
            -- "operator does not exist: uuid = character varying".
            JOIN closed_in_window c ON CAST(c.job_id AS TEXT) = CAST(ja.job_id AS TEXT)
            WHERE ja.deleted_at IS NULL
            ORDER BY ja.job_id, ja.is_lead DESC, ja.assigned_at ASC
        )
        SELECT
            COALESCE(lfj.tech_id, c.assigned_to)                            AS tech_id,
            t.name                                                          AS tech_name,
            SUM(c.scheduled_duration_hours)                                 AS scheduled_hours,
            SUM(c.hours_worked)                                             AS actual_hours,
            COUNT(*)                                                        AS job_count
        FROM closed_in_window c
        LEFT JOIN lead_for_job lfj ON CAST(lfj.job_id AS TEXT) = CAST(c.job_id AS TEXT)
        LEFT JOIN technicians t
               ON CAST(t.id AS TEXT) = CAST(COALESCE(lfj.tech_id, c.assigned_to) AS TEXT)
              AND t.deleted_at IS NULL
        WHERE COALESCE(lfj.tech_id, c.assigned_to) IS NOT NULL
        GROUP BY COALESCE(lfj.tech_id, c.assigned_to), t.name
        ORDER BY (SUM(c.scheduled_duration_hours) / NULLIF(SUM(c.hours_worked), 0)) DESC NULLS LAST
        """
    )
    rows = db.execute(sql, {"start": window_start, "end": window_end}).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        sched = Decimal(str(r.get("scheduled_hours") or 0))
        actual = Decimal(str(r.get("actual_hours") or 0))
        ratio = float(sched / actual) if actual > 0 else None
        out.append({
            "tech_id": str(r.get("tech_id")) if r.get("tech_id") else None,
            "tech_name": r.get("tech_name") or "Unassigned",
            "scheduled_hours": float(sched),
            "actual_hours": float(actual),
            "job_count": int(r.get("job_count") or 0),
            "efficiency_ratio": round(ratio, 2) if ratio is not None else None,
        })
    return out


@router.get(
    "",
    response_model=None,
    dependencies=[Depends(require_permission("dispatch.read"))],
)
def tech_efficiency(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ = request
    try:
        d_start, d_end = _today_window_utc()
        w_start, w_end = _iso_week_window_utc()
        daily = _query_efficiency(db, d_start, d_end)
        weekly = _query_efficiency(db, w_start, w_end)
    except ProgrammingError:
        # Tenant DB hasn't run the sprint dispatch-capacity migration yet
        # (scheduled_duration_hours column missing). Return an empty
        # report with a clear shape; client renders "no data yet".
        log.exception("tech_efficiency_schema_missing")
        return JSONResponse(jsonable_encoder({
            "daily": {"rows": [], "window": None},
            "weekly": {"rows": [], "window": None},
            "schema_pending": True,
        }))
    except SQLAlchemyError:
        log.exception("tech_efficiency_sql_failed")
        return JSONResponse({"detail": "tech efficiency report failed"}, status_code=500)
    return JSONResponse(jsonable_encoder({
        "daily": {
            "window": {"start": d_start.isoformat(), "end": d_end.isoformat()},
            "rows": daily,
        },
        "weekly": {
            "window": {"start": w_start.isoformat(), "end": w_end.isoformat()},
            "rows": weekly,
        },
    }))
