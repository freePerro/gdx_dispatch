"""Performance — user performance stats aggregated from existing tables.

A stat the server could not compute is ``None`` with a reason in the user's
``unavailable`` map, never ``0``. A zero here is a count that ran; "we could not read it" and "there is no record of it" are said in
words, because on this page a made-up nought reads as "this person did no
work" (maintainer ruling on GDXA-174, 2026-10-04).

Nothing here may feed pay or billing. Hours are clock-worked hours for a
performance read-out; billed labour comes from attested hours only.
"""
from __future__ import annotations

import calendar
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.pay_periods import PayPeriod, resolve_zone, shop_tz_name_from_settings
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.core.timesheet_hours import build_timesheet
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.reports import _revenue_amount_sql, _revenue_where_sql

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/performance",
    tags=["performance"],
    # `nav.office` is exactly who the sidebar shows this page to
    # (constants/modules.js). Before GDXA-174 the router had no permission
    # gate at all, which mattered little while every hours figure was a
    # broken 0; now that they are real clock hours for the whole crew, a
    # technician must not be able to read everyone else's. `nav.office` is
    # still wider than the timesheet's audience (sales, accounting and viewer
    # hold it), so the crew's hours are additionally withheld, by name, from
    # anyone `is_dispatch_manager` refuses — see `_hours_by_tech`.
    dependencies=[Depends(require_module("jobs")), Depends(require_permission("nav.office"))],
)

#: Why a stat is ``None``. The view turns each into a sentence.
REASON_READ_FAILED = "read_failed"   # the source could not be read this time
REASON_NO_DATA = "no_data"           # read fine; nothing recorded for this person
REASON_PERIOD_REQUIRED = "period_required"  # hours are only computed for a month
REASON_NOT_RECORDED = "not_recorded"  # this system does not record the fact at all
REASON_SHIFT_FLAGGED = "shift_flagged"  # a shift the timesheet flags for a human look
REASON_IN_PROGRESS = "in_progress"   # clocked in now; no shift finished yet this month
REASON_RESTRICTED = "restricted"     # the caller may not read the crew's hours

_PERIOD_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
# Both endpoints take the same month; a malformed one is a 422, not a silent
# whole-history read.
_PERIOD_QUERY = Query(None, description="YYYY-MM format", pattern=_PERIOD_PATTERN)


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


def _id_key(value: Any) -> str:
    """A user id as the timeclock stores it and as `User.id` renders it, made
    comparable: `technician_id` is TEXT written from the token's `sub`, and
    `User.id` is a Uuid that SQLite keeps as dashless hex."""
    return str(value or "").replace("-", "").lower()


def _month_period(period: str | None) -> PayPeriod | None:
    """`YYYY-MM` as a closed range of shop-local days, or None."""
    if not period:
        return None
    year, month = int(period[:4]), int(period[5:7])
    return PayPeriod(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]))


def _month_bounds_utc(db: Session, period: str) -> tuple[str, str]:
    """`YYYY-MM` as ``[start, end)`` UTC instants: the shop's local midnights.

    The same month the hours authority buckets by (`build_timesheet`, shop
    calendar day). Bare `'YYYY-MM-01'` strings made the other counts UTC
    months, so a job finished at 8pm Central on the 31st landed in the next
    month while that evening's shift stayed in this one.

    Spelled ``YYYY-MM-DD HH:MM:SS+00:00``: Postgres reads the offset, and on
    SQLite — which stores the ORM's timestamps as ``YYYY-MM-DD HH:MM:SS.ffffff``
    text — the string compares in the right order, boundary second included.
    """
    month = _month_period(period)
    if month is None:
        raise ValueError("a month window needs a period")
    zone = resolve_zone(shop_tz_name_from_settings(db))

    def utc_midnight(day: date) -> str:
        local = datetime.combine(day, time.min, tzinfo=zone)
        return local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S+00:00")

    return utc_midnight(month.start), utc_midnight(month.end + timedelta(days=1))


HoursByTech = dict[str, tuple[float | None, str | None]]


def _card_hours(card: Any) -> tuple[float | None, str | None]:
    """Finished-shift worked hours, or ``(None, reason)`` when 0.0 would lie.

    `Shift.worked_minutes` counts a shift with no known length as 0. Payroll
    can, because it shows the authority's flag beside it; this page has no
    flag column, so it reads the same flag (`Shift.flag`, never its own test
    of `minutes`) and says so instead:

    - any shift the authority flags (`Timecard.flagged`: open past a possible
      shift, no duration, or implausibly long — prod holds 72h..1584h rows):
      the month's total is not known. The authority's own set, not a
      hand-picked list, so a new flag cannot slip a lying total past here.
    - open but unflagged is a shift in progress — normal on any workday.
      Finished shifts still count; only a month with nothing finished yet
      would otherwise read 0.0 for someone who is at work right now.
    """
    if card.flagged:
        return None, REASON_SHIFT_FLAGGED
    worked = [s for s in card.shifts if not s.is_time_off]
    if worked and all(s.clock_out is None for s in worked):
        return None, REASON_IN_PROGRESS
    return card.worked_hours, None


def _hours_by_tech(
    db: Session, tid: str, period: str | None, user: dict[str, Any]
) -> tuple[HoursByTech | None, str | None]:
    """``({id_key: (hours, reason)}, None)``, or ``(None, reason)`` when unknown.

    Read through `core/timesheet_hours.build_timesheet` — the one hours
    authority — and NOT hand-summed here. It nets breaks off gross `minutes`
    (totalling `minutes` pays out every lunch), excludes soft-deleted entries
    and time off, and buckets each shift by the shop's calendar day. The raw
    `text()` this replaces read `SUM(hours_worked) FROM timeclock_entries
    WHERE company_id/user_id/clock_in`: a table and four columns that exist
    nowhere, so it raised on every request and the page reported 0.0.

    Clock-worked hours by decision, not by accident: whether this should be
    attested hours (`job_closeouts.hours_worked`) instead is open with the
    maintainer on GDXA-172, and swapping the source is a change to this one
    function. Either way it is a performance read-out — never an input to pay
    or billing.

    Hours need a bounded window. With no month there is no defensible range
    (`build_timesheet` caps its read, and a capped all-time sum would be a
    silent undercount), so the answer is "unavailable", not a number.
    """
    # The same audience as the timesheet's crew reads (routers/timeclock.py).
    if not is_dispatch_manager(user):
        return None, REASON_RESTRICTED
    pay_period = _month_period(period)
    if pay_period is None:
        return None, REASON_PERIOD_REQUIRED
    try:
        # SAVEPOINT: the stats reads after this share the session; a failed
        # hours read must not abort the transaction under them.
        with contained_read(db):
            sheet = build_timesheet(
                db,
                tenant_id=tid,
                period=pay_period,
                tz_name=shop_tz_name_from_settings(db),
            )
    except SQLAlchemyError:
        log.exception("performance_hours_read_failed", extra={"tenant_id": tid})
        return None, REASON_READ_FAILED
    return {_id_key(card.tech_id): _card_hours(card) for card in sheet.timecards}, None


def _build_user_stats(
    db: Session,
    tid: str,
    user_id: str,
    period: str | None,
    hours_by_tech: HoursByTech | None,
    hours_reason: str | None = None,
    pay_visible: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    """``(stats, unavailable)`` for a single user from existing tables.

    Every stat starts as ``None`` and is set only by a read that succeeded; a
    stat left ``None`` is named in ``unavailable`` with the reason. Before
    GDXA-174 every stat started at ``0`` and a failed read left it there,
    which is how a table that does not exist produced a plausible 0.0.

    Every read below is deliberately swallowed — a tenant that has never used
    the planner should not 500 the performance page over a missing
    `planner_tasks`. Each is wrapped in `contained_read` (GDXA-164) because on
    Postgres that swallow is otherwise a lie: a failed statement aborts the
    whole transaction, so read #1 failing makes reads #2..#7 fail too and the
    user comes back with every stat at zero — no error, just a page of noughts.
    `all_users_performance` runs this once per user on one session, so the blast
    radius is the whole crew, not one row.

    Two of the original seven reads could never succeed, and both are the same
    defect — a raw `text()` whose identifiers were never checked against this
    schema (GDXA-172):

    - estimates, filtering on `estimates.created_by` — a column in no model,
      no migration, no fixture, and 0 on prod. No longer run; see its comment.
    - hours, `FROM timeclock_entries` — a table that exists nowhere. Now read
      through the hours authority; see `_hours_by_tech`.

    Pre-GDXA-164 the first failure also zeroed every read after it, for every
    user after the first, because `all_users_performance` loops this function
    over ONE session. Containment fixed that collateral damage; it could not
    fix the two reads themselves.

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
        "jobs_completed": None,
        "revenue": None,
        "avg_job_value": None,
        "estimates_created": None,
        "estimates_accepted": None,
        "hours_worked": None,
        "tasks_completed": None,
        "commission_earned": None,
        "safety_checklists": None,
    }
    unavailable: dict[str, str] = {}

    period_filter = ""
    params: dict[str, Any] = {"tid": tid, "user_id": user_id}
    if period:
        period_filter = " AND created_at >= :period_start AND created_at < :period_end"
        params["period_start"], params["period_end"] = _month_bounds_utc(db, period)

    # Each count lands in the month the thing happened, not the month its row
    # was created: an installation booked on 25 Feb and finished and billed on
    # 3 Mar is March's work. `created_at` is only the fallback for legacy rows
    # that never stamped a completion. (`period_filter` is spelled on a bare
    # `created_at`; each read swaps in its own column with a literal replace.)
    job_done_at = "COALESCE(j.completed_at, j.created_at)"

    # Jobs completed. `jobs.assigned_to` holds a *technician* id in the common
    # case (core/job_access.py), a user id only on legacy rows — so match both,
    # through `technicians.user_id`, exactly as `job_belongs_to_user` does.
    # Matching the user id alone counted 0 for every normally assigned job.
    # One spelling of "this person's completed jobs in the window", shared by
    # the job count and the revenue read so Avg Job divides like by like.
    completed_jobs = f"""
        j.company_id = :tid AND j.deleted_at IS NULL
          AND (j.assigned_to = :user_id OR t.user_id = :user_id)
          AND j.status IN ('Complete', 'Completed', 'complete', 'completed')
          {period_filter.replace('created_at', job_done_at)}
    """
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM jobs j
                    LEFT JOIN technicians t ON t.id = j.assigned_to
                    WHERE {completed_jobs}
                """),  # noqa: S608 — period_filter is a literal clause (column renamed by a literal replace); dates are bound
                params,
            ).mappings().first()
        stats["jobs_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("jobs table query failed for user stats")
        unavailable["jobs_completed"] = REASON_READ_FAILED

    # Revenue from invoices — the shop's one revenue rule, imported from
    # `routers/reports.py` rather than spelled a third time: live and billed
    # (sent, paid, overdue), so a voided-and-reissued invoice does not count
    # twice and a closeout autodraft is not money earned. Deposits COUNT: the
    # final invoice already nets the deposit with a negative line, so dropping
    # the deposit would subtract it twice (see the M8 note in reports.py).
    #
    # And only on the jobs `jobs_completed` counts — completed in the same
    # window (`job_done_at`) — because `avg_job_value` divides one by the
    # other: a $5000 progress invoice on an unfinished job made one $100 job
    # average $5100. Invoiced, not necessarily paid.
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COALESCE(SUM({_revenue_amount_sql("i")}), 0) AS revenue
                    FROM invoices i
                    JOIN jobs j ON i.job_id = j.id
                    LEFT JOIN technicians t ON t.id = j.assigned_to
                    WHERE {completed_jobs}
                      AND {_revenue_where_sql("i")}
                """),  # noqa: S608 — period_filter is a literal clause (column renamed by a literal replace) and the revenue fragments are reports.py constants; dates are bound
                params,
            ).mappings().first()
        revenue = _safe_float(row["revenue"]) if row else 0.0
        stats["revenue"] = revenue
    except Exception:
        log.debug("invoices table query failed for user stats")
        unavailable["revenue"] = REASON_READ_FAILED
    # An average over no jobs is not $0 — it does not exist.
    if stats["jobs_completed"] and stats["revenue"] is not None:
        stats["avg_job_value"] = round(stats["revenue"] / stats["jobs_completed"], 2)
    elif "jobs_completed" in unavailable or "revenue" in unavailable:
        unavailable["avg_job_value"] = REASON_READ_FAILED
    else:
        unavailable["avg_job_value"] = REASON_NO_DATA

    # Estimates created / accepted — NOT READ, because the question cannot be
    # asked of this schema. The query that used to sit here filtered on
    # `estimates.created_by`, which is not a column: not on the ORM model, not
    # added by any migration, absent from `tests/fixtures/structure.sql`, and 0
    # on prod (checked live 2026-09-27, `information_schema.columns`). It raised
    # `UndefinedColumn` on every request. A rename will not fix it: `estimates`
    # records no staff author at all (`signed_by` is the CUSTOMER's signature).
    # So the honest answer is "not recorded", said in words, until the schema
    # records who wrote an estimate — a product call, raised on GDXA-172.
    unavailable["estimates_created"] = REASON_NOT_RECORDED
    unavailable["estimates_accepted"] = REASON_NOT_RECORDED

    # Clock hours — read through the hours authority, not here. See
    # `_hours_by_tech`: the raw `text()` that used to sit here named a table,
    # a column and two filters that exist nowhere (GDXA-172/GDXA-174).
    if hours_by_tech is None:
        unavailable["hours_worked"] = hours_reason or REASON_READ_FAILED
    else:
        hours, reason = hours_by_tech.get(_id_key(user_id), (None, REASON_NO_DATA))
        if reason:
            unavailable["hours_worked"] = reason
        else:
            stats["hours_worked"] = hours

    # Tasks completed
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM planner_tasks
                    WHERE company_id = :tid AND assigned_to = :user_id
                      AND status = 'done'
                      {period_filter.replace('created_at', 'COALESCE(completed_at, created_at)')}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        stats["tasks_completed"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("planner_tasks table query failed for user stats")
        unavailable["tasks_completed"] = REASON_READ_FAILED

    # Commission earned — pay-adjacent, so the same audience as the hours: a
    # role that may not read the crew's hours does not get the crew's pay
    # either. (The page does not render it; the JSON is what is being gated.)
    if not pay_visible:
        unavailable["commission_earned"] = REASON_RESTRICTED
    else:
        commission_sql = (
            "SELECT COALESCE(SUM(total), 0) AS earned FROM commission_entries"
            " WHERE company_id = :tid AND user_id = :user_id"
        )
        commission_params: dict[str, Any] = {"tid": tid, "user_id": user_id}
        if period:
            commission_sql += " AND period = :period"
            commission_params["period"] = period
        try:
            with contained_read(db):
                row = db.execute(text(commission_sql), commission_params).mappings().first()
            stats["commission_earned"] = _safe_float(row["earned"]) if row else 0.0
        except Exception:
            log.debug("commission_entries table query failed for user stats")
            unavailable["commission_earned"] = REASON_READ_FAILED

    # Safety checklists completed
    try:
        with contained_read(db):
            row = db.execute(
                text(f"""
                    SELECT COUNT(*) AS cnt FROM safety_checklists
                    WHERE company_id = :tid AND technician_id = :user_id
                      AND completed = true AND deleted_at IS NULL
                      {period_filter.replace('created_at', 'COALESCE(signed_at, created_at)')}
                """),  # noqa: S608 — period_filter is a literal clause; dates are bound
                params,
            ).mappings().first()
        stats["safety_checklists"] = _safe_int(row["cnt"]) if row else 0
    except Exception:
        log.debug("safety_checklists table query failed for user stats")
        unavailable["safety_checklists"] = REASON_READ_FAILED

    return stats, unavailable


def _user_payload(u: Any, stats: dict[str, Any], unavailable: dict[str, str]) -> dict[str, Any]:
    """One person's row: identity, the stats, and why any stat is null."""
    return {
        "id": str(u["id"]),
        "name": u.get("name") or u.get("full_name") or "",
        "email": u.get("email") or "",
        "role": u.get("role") or "",
        "stats": stats,
        "unavailable": unavailable,
    }


@router.get("/users")
def all_users_performance(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = _PERIOD_QUERY,
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
    pay_visible = is_dispatch_manager(user)
    # One timesheet for the whole crew, not one per user. Only when there is
    # someone to report on: a failed users read above leaves the session
    # aborted, and the hours read would then fail for that reason instead.
    hours_by_tech, hours_reason = _hours_by_tech(db, tid, period, user) if users else (None, None)
    for u in users:
        uid = str(u["id"])
        stats, unavailable = _build_user_stats(
            db, tid, uid, period, hours_by_tech, hours_reason, pay_visible
        )
        result.append(_user_payload(u, stats, unavailable))

    return {"users": result, "period": period}


@router.get("/users/{user_id}")
def user_performance(
    user_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    period: str | None = _PERIOD_QUERY,
) -> dict[str, Any]:
    """Single user detail with stats."""
    tid = _tid(request)

    # `User.id` is a Uuid column: bound as a plain str it raises in the bind
    # processor (measured on SQLite), the `except` below swallowed that, and
    # every detail request answered "User not found" for a user who exists.
    try:
        user_key = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="User not found") from None

    # Verify user belongs to tenant — ORM
    u = None
    try:
        u = db.execute(
            select(User.id, User.name, User.full_name, User.email, User.role)
            .where(User.id == user_key, User.company_id == tid)
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

    hours_by_tech, hours_reason = _hours_by_tech(db, tid, period, user)
    pay_visible = is_dispatch_manager(user)
    # The canonical spelling, not the path's: a dashless or upper-case id
    # matched no `jobs.assigned_to` and answered a believable 0.
    stats, unavailable = _build_user_stats(
        db, tid, str(user_key), period, hours_by_tech, hours_reason, pay_visible
    )
    return {**_user_payload(u, stats, unavailable), "period": period}
