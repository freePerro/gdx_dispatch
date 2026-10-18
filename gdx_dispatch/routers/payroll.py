"""
Payroll router — commission rate history, and payroll's `time_entries` hours
reader.

Gated behind the "timeclock" module. The summary, per-tech and CSV export
routes (/api/payroll/summary, /tech/{tech_id}, /export) were deleted in
GDXA-1560 (Doug, 2026-10-18): no UI called them, every call was a 503 behind
the broken commission revenue query (money audit M27), and plugins read the
shared database through plugin_api rather than core REST routes, so a future
payroll plugin had no use for them either. Their overtime, commission and
revenue helpers went with them.

Pattern mirrors gdx_dispatch/routers/change_orders.py (tenant scoping, audit, pydantic bounds).
Inline model follows gdx_dispatch/routers/collections.py pattern.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import contained_read, get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.pay_periods import shop_day_of, shop_tz_name_from_settings
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

# "timeclock" is present in AVAILABLE_MODULES (gdx_dispatch/core/modules.py).
router = APIRouter(
    tags=["payroll"],
    dependencies=[Depends(require_module("timeclock")), Depends(require_permission("payroll.read"))],
)


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
# Data aggregation
# ---------------------------------------------------------------------------
def _fetch_tech_hours(
    db: Session, *, tenant_id: str, start: date, end: date, tech_id: str | None = None
) -> dict[str, dict[date, float]]:
    """Return {tech_id: {date: hours}} from time_entries table.

    Returns empty dict if the table is missing (graceful degrade).

    No route calls this since GDXA-1560 deleted the payroll summary. It stays
    because the labor-trail guards (closeout, day close, mobile timer, stale
    timer repair, the #478 soft-delete test) assert through it that a
    `time_entries` row never reaches pay without a user_id, a clock_out and no
    deleted_at. Whether those guards should read the timesheet authority
    (core/timesheet_hours.py, which reads `timeclock_entries`) instead is not
    decided here.
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
    #
    # The work day is the shop's, not UTC's: `DATE(clock_in)` filed an
    # evening shift under tomorrow and, on the last day, out of the range
    # (GDXA-510). The UTC dates below are a day wider each side as a
    # prefilter; shop_day_of decides each row.
    sql = """
        SELECT user_id AS tech_id,
               clock_in,
               COALESCE(duration_minutes, 0) / 60.0 AS hours
          FROM time_entries
         WHERE company_id = :tenant_id
           AND DATE(clock_in) >= :start
           AND DATE(clock_in) <= :end
           AND clock_out IS NOT NULL
           AND deleted_at IS NULL
    """
    params: dict[str, Any] = {
        "tenant_id": tenant_id,
        "start": (start - timedelta(days=1)).isoformat(),
        "end": (end + timedelta(days=1)).isoformat(),
    }
    if tech_id:
        sql += " AND user_id = :tech_id"
        params["tech_id"] = tech_id

    result: dict[str, dict[date, float]] = {}
    try:
        # SAVEPOINT: the "graceful degrade" this docstring promises is only true
        # on SQLite. On Postgres a missing `time_entries` aborts the caller's
        # transaction, so the empty dict would be followed by every later read
        # on the caller's session failing as well.
        with contained_read(db):
            rows = db.execute(text(sql), params).all()
    except OperationalError:
        log.exception("payroll_time_entries_missing")
        return result
    except SQLAlchemyError:
        log.exception("payroll_time_entries_query_failed")
        return result

    tz_name = shop_tz_name_from_settings(db) if rows else None
    for row in rows:
        tid = str(row[0]) if row[0] is not None else ""
        if not tid:
            continue
        day = shop_day_of(row[1], tz_name)
        if day is None:
            log.warning("_fetch_tech_hours_unparseable_clock_in tech_id=%s", tid)
            continue
        if not start <= day <= end:
            continue
        by_day = result.setdefault(tid, {})
        by_day[day] = by_day.get(day, 0.0) + float(row[2] or 0)
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
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

