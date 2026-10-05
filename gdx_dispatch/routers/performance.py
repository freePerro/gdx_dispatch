"""Performance — user performance stats aggregated from existing tables."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/performance",
    tags=["performance"],
    dependencies=[Depends(require_module("jobs"))],
)


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _safe_int(val: Any) -> int:
    try:
        return int(val or 0)
    except (TypeError, ValueError):  # silent failure for type conversion utility
        logging.getLogger(__name__).exception("_safe_int caught exception")
        return 0


def _safe_float(val: Any) -> float:
    try:
        return round(float(val or 0), 2)
    except (TypeError, ValueError):  # silent failure for invalid numeric input
        logging.getLogger(__name__).exception("_safe_float caught exception")
        return 0.0


def _build_user_stats(db: Session, tid: str, user_id: str, period: str | None) -> dict[str, Any]:
    """Aggregate stats for a single user from existing tables.

    Every read below is deliberately swallowed — a tenant that has never used
    the planner should not 500 the performance page over a missing
    `planner_tasks`. Each is wrapped in `contained_read` (GDXA-164) because on
    Postgres that swallow is otherwise a lie: a failed statement aborts the
    whole transaction, so read #1 failing makes reads #2..#7 fail too and the
    user comes back with every stat at zero — no error, just a page of noughts.
    `all_users_performance` runs this once per user on one session, so the blast
    radius is the whole crew, not one row.

    This is the one call site in the tree where that mechanism demonstrably
    fires, every request, rather than being insurance — and be precise about how
    far that goes, because three drafts of this note got it wrong in three
    different ways. TWO of the seven reads below can never succeed, not one:

    - #3, estimates, filters on `estimates.created_by` — a column in no model,
      no migration, no fixture, and 0 on prod. Raises `UndefinedColumn` (42703).
    - #4, hours, selects `FROM timeclock_entries` — a table that exists nowhere
      either. Raises `UndefinedTable` (42P01). See its own comment.

    So pre-GDXA-164 the first user lost reads #3..#7, and because
    `all_users_performance` loops this function over ONE session, every user
    after the first lost #1..#7 — `jobs_completed` and `revenue` included.
    Measured on real PG, two seeded users, one job and one `done` task each:
    pre-fix `{jobs:1, tasks:0}` then `{jobs:0, tasks:0}`; post-fix both
    `{jobs:1, tasks:1}`. Containment fixes the collateral damage. It does NOT
    fix #3 or #4, which stay 0 forever.

    It is still NOT a user-visible repair, and do not sell it as one. No human
    has ever read these numbers: `PerformanceView.vue` does
    `Array.isArray(r) ? r : r?.items || []` against a `{"users": [...]}` payload,
    so the table is unconditionally empty, and the columns it renders
    (`efficiency_score`, `on_time_pct`, ...) are served by no endpoint in the
    repo. The page is an orphan in both directions and it is the only caller.
    So: this fix corrects an endpoint nothing currently displays. `hours_worked`
    stays 0 regardless — a separate defect, named below. Both are raised on
    GDXA-172 rather than fixed in a containment sweep.

    On cost, because this change was briefly deferred over it and the deferral
    was wrong: the savepoint pair triples the statement count here (7 -> 21 per
    user, measured). What that actually costs was then measured too — 0.159 ms
    per read against Postgres over a docker bridge, so a 30-person page pays
    about 33 ms. That is noise next to the 210 real queries this endpoint
    already issues, and it does not buy back a page of silent zeros. The N+1
    shape is the real performance problem here and it is older than this change.

    Contained, not re-raised: the degraded answer stays the answer. The savepoint
    only stops one unreadable table from silently zeroing the six that are fine.
    """
    stats: dict[str, Any] = {
        "jobs_completed": 0,
        "revenue": 0.0,
        "avg_job_value": 0.0,
        "estimates_created": 0,
        "estimates_accepted": 0,
        "hours_worked": 0.0,
        "tasks_completed": 0,
        "commission_earned": 0.0,
        "safety_checklists": 0,
    }

    period_filter = ""
    params: dict[str, Any] = {"tid": tid, "user_id": user_id}
    if period:
        period_filter = " AND created_at >= :period_start AND created_at < :period_end"
        params["period_start"] = f"{period}-01"
        # Approximate end: add 32 days, truncate
        try:
            year, month = int(period[:4]), int(period[5:7])
            end = f"{year + 1}-01-01" if month == 12 else f"{year}-{month + 1:02d}-01"
            params["period_end"] = end
        except (ValueError, IndexError):
            logging.getLogger(__name__).exception("_build_user_stats caught exception")
            period_filter = ""

    # Jobs completed
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM jobs
                    WHERE company_id = :tid AND assigned_to = :user_id
                      AND status IN ('Complete', 'Completed', 'complete', 'completed')
                      {period_filter}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        stats["jobs_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("jobs table query failed for user stats")

    # Revenue from invoices
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COALESCE(SUM(i.total), 0) AS revenue
                    FROM invoices i
                    JOIN jobs j ON i.job_id = j.id
                    WHERE j.company_id = :tid AND j.assigned_to = :user_id
                      {period_filter.replace('created_at', 'i.created_at')}
                """),  # noqa: S608 — period_filter is a literal clause (column renamed by a literal replace); dates are bound
                params,
            ).mappings().first()
        revenue = _safe_float(row["revenue"]) if row else 0.0
        stats["revenue"] = revenue
        if stats["jobs_completed"] > 0:
            stats["avg_job_value"] = round(revenue / stats["jobs_completed"], 2)
    except Exception:
        log.debug("invoices table query failed for user stats")

    # Estimates created / accepted
    #
    # BROKEN the same way the hours read below is, and found the same way — by an
    # audit, after two earlier passes over this function missed it. `created_by`
    # is not a column on `estimates`: not on the ORM model (30 columns, none of
    # them this), not added by any migration, absent from
    # `tests/fixtures/structure.sql`, and 0 on prod (checked live 2026-09-27,
    # `information_schema.columns`). So this raises `UndefinedColumn` (42703) on
    # every request and `estimates_created`/`estimates_accepted` have always been
    # 0. Not repaired here, and a rename will not do it: `estimates` records no
    # author at all. Its only two author-ish columns are `signed_by` (the
    # CUSTOMER's signature, not staff) and `created_at` (a timestamp) — checked
    # against the model, 30 columns. So "which user created this estimate" is
    # not answerable from this table today, which makes it a product/schema
    # question rather than a containment one. Contained so it stops zeroing the
    # four reads that follow it. See GDXA-172.
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status IN ('accepted', 'approved') THEN 1 ELSE 0 END) AS accepted
                    FROM estimates
                    WHERE company_id = :tid AND created_by = :user_id
                      {period_filter}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        if row:
            stats["estimates_created"] = _safe_int(row["total"])
            stats["estimates_accepted"] = _safe_int(row["accepted"])
    except Exception:
        log.debug("estimates table query failed for user stats")

    # Timeclock hours
    #
    # BROKEN, and containment does not fix it — flagged for a ruling, not
    # repaired here (GDXA-164 is a containment sweep; this is a wrong-query
    # defect and changing what the office's hours number MEANS is not a
    # containment change). `timeclock_entries` exists nowhere: not in any
    # migration, not in the ORM (`timeclock_entries_router`, `timeclock_breaks_
    # router`, `timeclocks`), not in `tests/fixtures/structure.sql`, and not on
    # prod — checked live 2026-09-27, PG 16.13, which has `time_entries`,
    # `timeclock_entries_router`, `timeclock_breaks_router`, `timeclocks`.
    # So this raises `UndefinedTable` on every request and `hours_worked` has
    # always been 0.0 on this page, which reads as "this tech did no work"
    # rather than "this number is broken". A rename is NOT the fix: no table
    # has an `hours_worked` column either (`timeclock_entries_router` has
    # `minutes`, `timeclocks` has `labor_minutes`, `time_entries` has
    # `duration_minutes`, and `hours_worked` belongs to `JobCloseout`), and
    # which of those is the office's intended "hours" is a product question.
    # `tests/test_raw_sql_table_names.py` should have caught this and cannot.
    # Its `_TEXT_RX` tries `"([^"]*)"` before the triple-quote branch, and that
    # matches the EMPTY string at the start of any `\"\"\"` literal — so EVERY
    # triple-double-quoted `text(...)` is invisible, not just the f-string form
    # (the `f` is irrelevant; a first draft of this comment said otherwise).
    # Measured: 72 empty captures across 23 files under `routers/` against 99
    # real ones. Fixing only the f-string case would leave 58 SQL strings
    # unscanned. See GDXA-172.
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COALESCE(SUM(hours_worked), 0) AS total_hours
                    FROM timeclock_entries
                    WHERE company_id = :tid AND user_id = :user_id
                      {period_filter.replace('created_at', 'clock_in')}
                """),  # noqa: S608 — period_filter is a literal clause (column renamed by a literal replace); dates are bound
                params,
            ).mappings().first()
        stats["hours_worked"] = _safe_float(row["total_hours"]) if row else 0.0
    except Exception:
        log.debug("timeclock_entries table query failed for user stats")

    # Tasks completed
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM planner_tasks
                    WHERE company_id = :tid AND assigned_to = :user_id
                      AND status = 'done'
                      {period_filter}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        stats["tasks_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("planner_tasks table query failed for user stats")

    # Commission earned
    try:
        dict(params)
        if period:
            with contained_read(db):
                row = db.execute(
                    text("""
                        SELECT COALESCE(SUM(total), 0) AS earned FROM commission_entries
                        WHERE company_id = :tid AND user_id = :user_id AND period = :period
                    """),
                    {"tid": tid, "user_id": user_id, "period": period},
                ).mappings().first()
        else:
            with contained_read(db):
                row = db.execute(
                    text("""
                        SELECT COALESCE(SUM(total), 0) AS earned FROM commission_entries
                        WHERE company_id = :tid AND user_id = :user_id
                    """),
                    {"tid": tid, "user_id": user_id},
                ).mappings().first()
        stats["commission_earned"] = _safe_float(row["earned"]) if row else 0.0
    except Exception:
        log.debug("commission_entries table query failed for user stats")

    # Safety checklists completed
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM safety_checklists
                    WHERE company_id = :tid AND technician_id = :user_id
                      AND completed = true AND deleted_at IS NULL
                      {period_filter}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        stats["safety_checklists"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("safety_checklists table query failed for user stats")

    return stats


@router.get("/users")
def all_users_performance(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM format"),
) -> dict[str, Any]:
    """All users with stats for a period."""
    tid = _tid(request)

    # Get all users for this tenant — ORM
    users: list[dict[str, Any]] = []
    try:
        user_rows = db.execute(
            select(
                User.id,
                User.name,
                User.full_name,
                User.email,
                User.role,
            )
            .where(
                User.company_id == tid,
                User.deleted_at.is_(None),
            )
            .order_by(User.name)
        ).mappings().all()
        users = [dict(r) for r in user_rows]
    except Exception:
        # Counted by the GDXA-164 sweep, deliberately NOT contained: `db` is the
        # route's own (`Depends(get_db)`, which only closes) and on failure
        # `users` stays empty, so the loop below never runs and no later
        # statement touches this session. Containment would change no outcome.
        log.debug("users ORM query failed")

    result = []
    for u in users:
        uid = str(u["id"])
        stats = _build_user_stats(db, tid, uid, period)
        result.append({
            "id": uid,
            "name": u.get("name") or u.get("full_name") or "",
            "email": u.get("email") or "",
            "role": u.get("role") or "",
            "stats": stats,
        })

    return {"users": result, "period": period}


@router.get("/users/{user_id}")
def user_performance(
    user_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = Query(None, description="YYYY-MM format"),
) -> dict[str, Any]:
    """Single user detail with stats."""
    tid = _tid(request)

    # Verify user belongs to tenant — ORM
    u = None
    try:
        u = db.execute(
            select(User.id, User.name, User.full_name, User.email, User.role)
            .where(User.id == user_id, User.company_id == tid)
        ).mappings().first()
    except Exception:
        # Counted by the GDXA-164 sweep, deliberately NOT contained: on failure
        # `u` stays None and the next line raises 404, so nothing further reads
        # this session. (The 404 is then misleading — it says "not found" for
        # what was really an unreadable table — but that is true with or without
        # a savepoint, so it is not this sweep's to fix.)
        log.debug("user lookup failed")

    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    stats = _build_user_stats(db, tid, user_id, period)
    return {
        "id": str(u["id"]),
        "name": u.get("name") or u.get("full_name") or "",
        "email": u.get("email") or "",
        "role": u.get("role") or "",
        "stats": stats,
        "period": period,
    }
