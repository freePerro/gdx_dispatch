"""
Payroll router — technician hours + revenue + commissions + CSV export.

Gated behind the "timeclock" module. Calculates weekly overtime (40h base +
1.5x rate), commission by rate type (percent/flat/hourly), and exports CSV.

Pattern mirrors gdx_dispatch/routers/proposals.py (tenant scoping, audit, pydantic bounds).
Inline model follows gdx_dispatch/routers/collections.py pattern.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# "timeclock" is present in AVAILABLE_MODULES (gdx_dispatch/core/modules.py).
router = APIRouter(
    tags=["payroll"],
    dependencies=[Depends(require_module("timeclock")), Depends(require_permission("payroll.read"))],
)


RATE_TYPES = ("percent", "flat", "hourly")
OT_THRESHOLD_HOURS = 40.0
OT_MULTIPLIER = 1.5


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
from gdx_dispatch.models.tenant_models import TechCommissionRate  # noqa: E402


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class CommissionRateIn(BaseModel):
    tech_id: str = Field(min_length=1, max_length=64)
    rate_type: str = Field(default="percent", pattern=r"^(percent|flat|hourly)$")
    rate_value: float = Field(ge=0, le=1_000_000)
    effective_from: datetime | None = None
    active: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid date: {value!r} (expect YYYY-MM-DD)") from None


def _default_range() -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    return start, today


def _serialize_rate(r: TechCommissionRate) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "company_id": r.company_id,
        "tech_id": r.tech_id,
        "rate_type": r.rate_type,
        "rate_value": float(r.rate_value or 0),
        "effective_from": r.effective_from.isoformat() if r.effective_from else None,
        "effective_until": r.effective_until.isoformat() if r.effective_until else None,
        "active": bool(r.active),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _audit(
    db: Session,
    *,
    tenant_id: str,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=_user_id(user),
            action=action,
            entity_type="commission_rate",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("payroll_audit_failed action=%s entity_id=%s", action, entity_id)
        try:
            db.rollback()
        except Exception:
            log.exception("payroll_audit_rollback_failed")


# ---------------------------------------------------------------------------
# Overtime + commission math (pure functions — unit-testable)
# ---------------------------------------------------------------------------
def calculate_weekly_overtime(
    daily_hours: dict[date, float],
) -> tuple[float, float]:
    """Return (regular_hours, overtime_hours) summed across ISO calendar weeks.

    Hours within a week over 40 are overtime.
    """
    by_week: dict[tuple[int, int], float] = {}
    for day, hours in daily_hours.items():
        if hours <= 0:
            continue
        iso = day.isocalendar()
        key = (iso[0], iso[1])
        by_week[key] = by_week.get(key, 0.0) + float(hours)

    regular = 0.0
    overtime = 0.0
    for _, total in by_week.items():
        if total <= OT_THRESHOLD_HOURS:
            regular += total
        else:
            regular += OT_THRESHOLD_HOURS
            overtime += total - OT_THRESHOLD_HOURS
    return regular, overtime


def calculate_commission(
    *,
    rate_type: str,
    rate_value: float,
    revenue: float,
    jobs_completed: int,
    hours_worked: float,
) -> float:
    """Commission by rate_type: percent of revenue, flat per job, or hourly bonus."""
    rv = float(rate_value or 0)
    if rate_type == "percent":
        return float(revenue or 0) * (rv / 100.0)
    if rate_type == "flat":
        return rv * int(jobs_completed or 0)
    if rate_type == "hourly":
        return rv * float(hours_worked or 0)
    return 0.0


def calculate_gross_pay(
    *,
    regular_hours: float,
    overtime_hours: float,
    base_rate: float,
    commission: float,
) -> float:
    """Gross = (regular * base) + (overtime * base * 1.5) + commission."""
    base = float(base_rate or 0)
    return (
        float(regular_hours) * base
        + float(overtime_hours) * base * OT_MULTIPLIER
        + float(commission or 0)
    )


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------
def _fetch_active_rate(
    db: Session, *, tenant_id: str, tech_id: str
) -> TechCommissionRate | None:
    stmt = (
        select(TechCommissionRate)
        .where(
            TechCommissionRate.company_id == tenant_id,
            TechCommissionRate.tech_id == tech_id,
            TechCommissionRate.deleted_at.is_(None),
            TechCommissionRate.active == True,  # noqa: E712
            TechCommissionRate.effective_until.is_(None),
        )
        .order_by(TechCommissionRate.effective_from.desc())
    )
    try:
        return db.execute(stmt).scalars().first()
    except SQLAlchemyError:
        log.exception("payroll_fetch_active_rate_failed tech_id=%s", tech_id)
        return None


def _fetch_tech_hours(
    db: Session, *, tenant_id: str, start: date, end: date, tech_id: str | None = None
) -> dict[str, dict[date, float]]:
    """Return {tech_id: {date: hours}} from time_entries table.

    Returns empty dict if the table is missing (graceful degrade).
    """
    # Prod time_entries schema uses clock_in/clock_out + a pre-computed
    # duration_minutes column. Historical code computed hours via
    # julianday(end_time) - julianday(start_time), which referenced columns
    # that do not exist on the live schema AND used a SQLite-only function
    # (Build Rule #1: no database-specific functions). Using duration_minutes
    # directly is both correct and portable.
    #
    # Live data stores the technician identifier in user_id (11/11 rows
    # populated on prod gdx tenant); tech_id/technician_id/tech_name exist
    # on the table schema but are not populated. Aliasing user_id as tech_id
    # in the result keeps the external return shape compatible.
    sql = """
        SELECT user_id AS tech_id,
               DATE(clock_in) AS work_day,
               SUM(COALESCE(duration_minutes, 0)) / 60.0 AS hours
          FROM time_entries
         WHERE company_id = :tenant_id
           AND DATE(clock_in) >= :start
           AND DATE(clock_in) <= :end
           AND clock_out IS NOT NULL
    """
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if tech_id:
        sql += " AND user_id = :tech_id"
        params["tech_id"] = tech_id
    sql += " GROUP BY user_id, DATE(clock_in)"

    result: dict[str, dict[date, float]] = {}
    try:
        rows = db.execute(text(sql), params).all()
    except OperationalError:
        log.exception("payroll_time_entries_missing")
        return result
    except SQLAlchemyError:
        log.exception("payroll_time_entries_query_failed")
        return result

    for row in rows:
        tid = str(row[0]) if row[0] is not None else ""
        if not tid:
            continue
        day_raw = row[1]
        if isinstance(day_raw, str):
            try:
                day = datetime.strptime(day_raw[:10], "%Y-%m-%d").date()
            except ValueError:
                log.exception("_fetch_tech_hours_failed")
                continue
        elif isinstance(day_raw, datetime):
            day = day_raw.date()
        elif isinstance(day_raw, date):
            day = day_raw
        else:
            continue
        hours = float(row[2] or 0)
        result.setdefault(tid, {})[day] = hours
    return result


def _fetch_tech_revenue(
    db: Session, *, tenant_id: str, start: date, end: date, tech_id: str | None = None
) -> dict[str, tuple[int, float]]:
    """Return {tech_id: (jobs_completed, revenue)} joined from jobs+invoices.

    Graceful degrade if tables missing.
    """
    sql = """
        SELECT j.assigned_tech_id AS tech_id,
               COUNT(DISTINCT j.id) AS jobs_completed,
               COALESCE(SUM(i.total), 0) AS revenue
          FROM jobs j
          LEFT JOIN invoices i
                 ON i.job_id = j.id
                AND i.company_id = :tenant_id
         WHERE j.company_id = :tenant_id
           AND j.status = 'completed'
           AND DATE(j.completed_at) >= :start
           AND DATE(j.completed_at) <= :end
           AND j.assigned_tech_id IS NOT NULL
    """
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    if tech_id:
        sql += " AND j.assigned_tech_id = :tech_id"
        params["tech_id"] = tech_id
    sql += " GROUP BY j.assigned_tech_id"

    result: dict[str, tuple[int, float]] = {}
    try:
        rows = db.execute(text(sql), params).all()
    except OperationalError:
        log.exception("payroll_jobs_or_invoices_missing")
        return result
    except SQLAlchemyError:
        log.exception("payroll_jobs_query_failed")
        return result
    for row in rows:
        tid = str(row[0]) if row[0] is not None else ""
        if not tid:
            continue
        result[tid] = (int(row[1] or 0), float(row[2] or 0))
    return result


def _fetch_tech_names(
    db: Session, *, tenant_id: str, tech_ids: list[str]
) -> dict[str, str]:
    if not tech_ids:
        return {}
    sql = """
        SELECT id, COALESCE(name, email, id) AS display
          FROM users
         WHERE company_id = :tenant_id
           AND id IN :ids
    """
    try:
        db.execute(
            text(sql).bindparams(),
            {"tenant_id": tenant_id},
        )
        # Fallback simple lookup per-id (IN-binding across dialects is tricky)
    except Exception:
        log.exception("_fetch_tech_names_failed extra_context=%s", "unknown")
        pass

    names: dict[str, str] = {}
    for tid in tech_ids:
        try:
            row = db.execute(
                text(
                    "SELECT COALESCE(name, email, id) FROM users "
                    "WHERE company_id = :tenant_id AND id = :tid LIMIT 1"
                ),
                {"tenant_id": tenant_id, "tid": tid},
            ).first()
            if row and row[0]:
                names[tid] = str(row[0])
        except OperationalError:
            log.exception("payroll_users_table_missing")
            return names
        except SQLAlchemyError:
            log.exception("payroll_user_name_lookup_failed tid=%s", tid)
    return names


def _build_summary_rows(
    db: Session,
    *,
    tenant_id: str,
    start: date,
    end: date,
    tech_id: str | None = None,
) -> list[dict[str, Any]]:
    hours_by_tech = _fetch_tech_hours(
        db, tenant_id=tenant_id, start=start, end=end, tech_id=tech_id
    )
    revenue_by_tech = _fetch_tech_revenue(
        db, tenant_id=tenant_id, start=start, end=end, tech_id=tech_id
    )

    all_techs = set(hours_by_tech.keys()) | set(revenue_by_tech.keys())
    if tech_id:
        all_techs.add(tech_id)
    names = _fetch_tech_names(db, tenant_id=tenant_id, tech_ids=sorted(all_techs))

    out: list[dict[str, Any]] = []
    for tid in sorted(all_techs):
        daily = hours_by_tech.get(tid, {})
        total_hours = sum(daily.values())
        regular, overtime = calculate_weekly_overtime(daily)
        jobs_done, revenue = revenue_by_tech.get(tid, (0, 0.0))

        rate = _fetch_active_rate(db, tenant_id=tenant_id, tech_id=tid)
        if rate is not None:
            commission = calculate_commission(
                rate_type=rate.rate_type,
                rate_value=float(rate.rate_value or 0),
                revenue=revenue,
                jobs_completed=jobs_done,
                hours_worked=total_hours,
            )
        else:
            commission = 0.0

        # Gross pay: without a base hourly wage field on the rate, treat gross
        # as commission only. Frontend can layer base wage separately.
        gross = commission

        out.append(
            {
                "tech_id": tid,
                "tech_name": names.get(tid, tid),
                "hours_worked": round(total_hours, 2),
                "regular_hours": round(regular, 2),
                "overtime_hours": round(overtime, 2),
                "jobs_completed": jobs_done,
                "revenue": round(revenue, 2),
                "commission": round(commission, 2),
                "gross_pay": round(gross, 2),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/api/payroll/summary", response_model=None)
def payroll_summary(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    default_start, default_end = _default_range()
    s = _parse_date(start, default_start)
    e = _parse_date(end, default_end)
    if e < s:
        raise HTTPException(status_code=422, detail="end must be >= start")
    return _build_summary_rows(db, tenant_id=tenant_id, start=s, end=e)


@router.get("/api/payroll/tech/{tech_id}", response_model=None)
def payroll_tech_detail(
    tech_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    default_start, default_end = _default_range()
    s = _parse_date(start, default_start)
    e = _parse_date(end, default_end)
    if e < s:
        raise HTTPException(status_code=422, detail="end must be >= start")

    rows = _build_summary_rows(db, tenant_id=tenant_id, start=s, end=e, tech_id=tech_id)
    row = rows[0] if rows else {
        "tech_id": tech_id,
        "tech_name": tech_id,
        "hours_worked": 0.0,
        "regular_hours": 0.0,
        "overtime_hours": 0.0,
        "jobs_completed": 0,
        "revenue": 0.0,
        "commission": 0.0,
        "gross_pay": 0.0,
    }
    daily_hours = _fetch_tech_hours(
        db, tenant_id=tenant_id, start=s, end=e, tech_id=tech_id
    ).get(tech_id, {})
    row["daily"] = [
        {"date": d.isoformat(), "hours": round(h, 2)}
        for d, h in sorted(daily_hours.items())
    ]
    return row


@router.get("/api/payroll/commission-rates", response_model=None)
def list_commission_rates(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    tech_id: str | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(TechCommissionRate).where(
        TechCommissionRate.deleted_at.is_(None),
    )
    if tech_id:
        stmt = stmt.where(TechCommissionRate.tech_id == tech_id)
    if active_only:
        stmt = stmt.where(
            TechCommissionRate.active == True,  # noqa: E712
            TechCommissionRate.effective_until.is_(None),
        )
    stmt = stmt.order_by(TechCommissionRate.effective_from.desc())
    rows = db.execute(stmt).scalars().all()
    return [_serialize_rate(r) for r in rows]


@router.post("/api/payroll/commission-rates", response_model=None, status_code=201)
def create_commission_rate(
    payload: CommissionRateIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    now = utcnow()

    # Expire any currently-active rate for this tech (rate history pattern).
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    prior_stmt = select(TechCommissionRate).where(
        TechCommissionRate.tech_id == payload.tech_id,
        TechCommissionRate.deleted_at.is_(None),
        TechCommissionRate.active == True,  # noqa: E712
        TechCommissionRate.effective_until.is_(None),
    )
    for prior in db.execute(prior_stmt).scalars().all():
        prior.effective_until = now
        prior.active = False
        prior.updated_at = now
        _audit(
            db,
            tenant_id=tenant_id,
            user=user,
            action="commission_rate_expired",
            entity_id=str(prior.id),
            details={"tech_id": prior.tech_id, "expired_at": now.isoformat()},
            request=request,
        )

    rate = TechCommissionRate(
        company_id=tenant_id,
        tech_id=payload.tech_id,
        rate_type=payload.rate_type,
        rate_value=Decimal(str(payload.rate_value)),
        effective_from=payload.effective_from or now,
        effective_until=None,
        active=bool(payload.active),
        created_at=now,
        updated_at=now,
    )
    db.add(rate)
    db.commit()
    db.refresh(rate)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="commission_rate_created",
        entity_id=str(rate.id),
        details={
            "tech_id": rate.tech_id,
            "rate_type": rate.rate_type,
            "rate_value": float(rate.rate_value or 0),
        },
        request=request,
    )
    return _serialize_rate(rate)


@router.get("/api/payroll/export", response_model=None)
def export_payroll_csv(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    start: str | None = None,
    end: str | None = None,
) -> Response:
    tenant_id = _tenant_id(request)
    default_start, default_end = _default_range()
    s = _parse_date(start, default_start)
    e = _parse_date(end, default_end)
    if e < s:
        raise HTTPException(status_code=422, detail="end must be >= start")

    rows = _build_summary_rows(db, tenant_id=tenant_id, start=s, end=e)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "tech_id",
            "tech_name",
            "regular_hours",
            "overtime_hours",
            "total_hours",
            "revenue",
            "commission",
            "gross_pay",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r.get("tech_id", ""),
                r.get("tech_name", ""),
                r.get("regular_hours", 0),
                r.get("overtime_hours", 0),
                r.get("hours_worked", 0),
                r.get("revenue", 0),
                r.get("commission", 0),
                r.get("gross_pay", 0),
            ]
        )

    filename = f"payroll_{s.isoformat()}_{e.isoformat()}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
