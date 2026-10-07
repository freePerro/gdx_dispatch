"""Tech efficiency report — daily + weekly leaderboards.

Sprint dispatch-capacity (2026-05-20). Surfaces "how much each tech beat
their scheduled time" so dispatch can plan around real velocity and the
shop can build a bonus structure on top.

Efficiency ratio = sum(scheduled_duration_hours) / sum(time on site) over
completed jobs (job_closeouts row exists) in the window. Time on site is the
closeout's hours_worked plus the job's earlier days (multi-day jobs plan
§5.4a; ``_actual_hours_by_job``). Higher = the
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
from datetime import UTC, datetime, timedelta
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
                jc.created_at AS closeout_created_at,
                j.scheduled_duration_hours,
                j.assigned_to
            FROM job_closeouts jc
            JOIN jobs j ON j.id = jc.job_id
            -- Supersede model (plan §12): only the job's CURRENT closeout
            -- counts. Without this filter a re-closed-out job contributes
            -- every restatement to the SUMs — hours double-count and the
            -- efficiency ratio silently rots. Same filter as
            -- core/closeouts.get_current_closeout.
            WHERE jc.superseded_at IS NULL
              AND jc.deleted_at IS NULL
              AND j.deleted_at IS NULL
              AND jc.closed_at >= :start
              AND jc.closed_at <  :end
              AND j.scheduled_duration_hours IS NOT NULL
              -- No hours filter here (multi-day plan §5.4a): a job whose final
              -- day attests 0 h can still have earlier days. The "> 0" test
              -- runs on the whole denominator, in _actual_hours_by_job.
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
            c.job_id                                                        AS job_id,
            COALESCE(lfj.tech_id, c.assigned_to)                            AS tech_id,
            t.name                                                          AS tech_name,
            c.scheduled_duration_hours                                      AS scheduled_hours,
            c.hours_worked                                                  AS hours_worked,
            c.closeout_created_at                                           AS closeout_created_at
        FROM closed_in_window c
        LEFT JOIN lead_for_job lfj ON CAST(lfj.job_id AS TEXT) = CAST(c.job_id AS TEXT)
        LEFT JOIN technicians t
               ON CAST(t.id AS TEXT) = CAST(COALESCE(lfj.tech_id, c.assigned_to) AS TEXT)
              AND t.deleted_at IS NULL
        WHERE COALESCE(lfj.tech_id, c.assigned_to) IS NOT NULL
        """
    )
    rows = db.execute(sql, {"start": window_start, "end": window_end}).mappings().all()
    return _aggregate(db, rows)


def _actual_hours_by_job(db: Session, jobs: list[dict[str, Any]]) -> dict[str, Decimal]:
    """The ratio's denominator per job: time on site, wall-clock.

    Multi-day jobs plan §5.4a ("Tech efficiency"): the closeout's
    ``hours_worked`` plus, per shop day, the job's LONGEST day row. The
    closeout's own shop day (``job_closeouts.created_at``, shop-local) counts
    ``max(hours_worked, longest day row that day)`` instead of adding
    ``hours_worked`` on top, because ``hours_worked`` is wall-clock for that
    day too: a helper who left early (a 4 h row beside an 8 h closeout) reads
    as 8 h, not 12; a crew "No" of 6 h then a 0 h "Yes" the same day reads as
    6 h, not 0. A job with no day rows is exactly ``hours_worked``.

    ``jobs`` items carry ``job_id``, ``hours_worked`` and
    ``closeout_created_at``. Keys are ``str(UUID)``.
    """
    from gdx_dispatch.core.closeout_billing import (  # noqa: PLC0415
        _job_uuid,
        day_row_entries,
        longest_day_row_minutes,
    )
    from gdx_dispatch.core.pay_periods import shop_day_of, shop_tz_name_from_settings  # noqa: PLC0415

    entries = day_row_entries(db, [j["job_id"] for j in jobs])
    tz_name = shop_tz_name_from_settings(db) if entries else None
    out: dict[str, Decimal] = {}
    for j in jobs:
        jid = str(_job_uuid(j["job_id"]))
        hours_worked = Decimal(str(j.get("hours_worked") or 0))
        rows = entries.get(jid)
        if not rows:
            out[jid] = hours_worked
            continue
        per_day = longest_day_row_minutes(rows, tz_name)
        closeout_day = shop_day_of(j.get("closeout_created_at"), tz_name)
        total = Decimal("0")
        for day, minutes in per_day.items():
            if day != closeout_day:
                total += Decimal(minutes) / 60
        on_closeout_day = Decimal(per_day.get(closeout_day, 0)) / 60
        total += max(hours_worked, on_closeout_day)
        out[jid] = total
    return out


def _aggregate(db: Session, rows) -> list[dict[str, Any]]:
    """Per-job rows (one per closed-out job, with its credited tech) → the
    leaderboard: one row per tech, ratio descending, no-ratio rows last."""
    jobs = [dict(r) for r in rows]
    actual_by_job = _actual_hours_by_job(db, jobs)
    from gdx_dispatch.core.closeout_billing import _job_uuid  # noqa: PLC0415

    by_tech: dict[str, dict[str, Any]] = {}
    for j in jobs:
        actual = actual_by_job.get(str(_job_uuid(j["job_id"])), Decimal("0"))
        # The old hours-worked-above-zero SQL filter, moved onto the whole
        # denominator: zero time on site means no ratio to compute.
        if actual <= 0:
            continue
        tid = str(j.get("tech_id"))
        agg = by_tech.setdefault(tid, {
            "tech_id": tid,
            "tech_name": j.get("tech_name") or "Unassigned",
            "scheduled": Decimal("0"),
            "actual": Decimal("0"),
            "job_count": 0,
        })
        agg["scheduled"] += Decimal(str(j.get("scheduled_hours") or 0))
        agg["actual"] += actual
        agg["job_count"] += 1
    out: list[dict[str, Any]] = []
    for agg in by_tech.values():
        sched, actual = agg["scheduled"], agg["actual"]
        ratio = float(sched / actual) if actual > 0 else None
        out.append({
            "tech_id": agg["tech_id"],
            "tech_name": agg["tech_name"],
            "scheduled_hours": float(sched),
            "actual_hours": float(actual),
            "job_count": agg["job_count"],
            "efficiency_ratio": round(ratio, 2) if ratio is not None else None,
        })
    out.sort(key=lambda r: (r["efficiency_ratio"] is None, -(r["efficiency_ratio"] or 0)))
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
