"""Overhead obligations router (ADR-016).

A first-class, owned register of recurring fixed/overhead cash obligations
(loans, insurance, rent, subscriptions, payroll …) plus a forward, month-by-
month projection so the owner can see overhead step *down* as loans pay off.

This is deliberately NOT a fusion of qb_pnl_monthly + the forecasting
RecurringStream (that was rejected — see ADR-016). The register is the source of
truth; the bank feed is used elsewhere only to flag completeness/drift.

Role model:
- Read:  ``accounting.read``
- Write: ``accounting.write``
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_permission
from gdx_dispatch.models.tenant_models import OverheadObligation
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.services.overhead_projection import project

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/overhead", tags=["overhead"])

# Validation sets. Cadence is a DB enum (must match the model); category and
# cost_type are app-validated so the catalog can grow without a migration.
CADENCES = frozenset({"weekly", "biweekly", "monthly", "quarterly", "semiannual", "annual"})
COST_TYPES = frozenset({"fixed", "variable"})
# Suggested categories for the UI; not enforced (any <=40 char string is allowed).
CATEGORIES = [
    "loan", "insurance", "rent", "lease", "utilities",
    "subscription", "payroll", "tax", "other",
]
MAX_HORIZON_MONTHS = 36


def _tenant_id(request: Request) -> str:
    t = getattr(request.state, "tenant", None) or {}
    tid = t.get("id") if isinstance(t, dict) else None
    if not tid:
        raise HTTPException(400, "tenant context not resolved")
    return str(tid)


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


# ---------- pydantic schemas ----------


class ScheduledChange(BaseModel):
    effective_date: date
    amount: Decimal = Field(ge=0, le=Decimal("1000000000"))


class ObligationIn(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    category: str = Field(default="other", min_length=1, max_length=40)
    vendor_id: UUID | None = None
    amount: Decimal = Field(ge=0, le=Decimal("1000000000"))
    cadence: str = Field(default="monthly")
    start_date: date
    end_date: date | None = None
    term_total_occurrences: int | None = Field(default=None, ge=1, le=600)
    scheduled_changes: list[ScheduledChange] | None = None
    cost_type: str = Field(default="fixed")
    is_estimate: bool = False
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: str) -> str:
        if v not in CADENCES:
            raise ValueError(f"cadence must be one of {sorted(CADENCES)}")
        return v

    @field_validator("cost_type")
    @classmethod
    def _check_cost_type(cls, v: str) -> str:
        if v not in COST_TYPES:
            raise ValueError(f"cost_type must be one of {sorted(COST_TYPES)}")
        return v


class ObligationPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, min_length=1, max_length=40)
    vendor_id: UUID | None = None
    amount: Decimal | None = Field(default=None, ge=0, le=Decimal("1000000000"))
    cadence: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    term_total_occurrences: int | None = Field(default=None, ge=1, le=600)
    scheduled_changes: list[ScheduledChange] | None = None
    cost_type: str | None = None
    is_estimate: bool | None = None
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("cadence")
    @classmethod
    def _check_cadence(cls, v: str | None) -> str | None:
        if v is not None and v not in CADENCES:
            raise ValueError(f"cadence must be one of {sorted(CADENCES)}")
        return v

    @field_validator("cost_type")
    @classmethod
    def _check_cost_type(cls, v: str | None) -> str | None:
        if v is not None and v not in COST_TYPES:
            raise ValueError(f"cost_type must be one of {sorted(COST_TYPES)}")
        return v


# ---------- serialization ----------


def _changes_to_json(changes: list[ScheduledChange] | None) -> list[dict] | None:
    if not changes:
        return None
    return [
        {"effective_date": c.effective_date.isoformat(), "amount": str(c.amount)}
        for c in sorted(changes, key=lambda c: c.effective_date)
    ]


def _to_iso(d) -> str | None:
    if d is None:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _obligation_to_dict(ob: OverheadObligation) -> dict[str, Any]:
    return {
        "id": str(ob.id),
        "label": ob.label,
        "category": ob.category,
        "vendor_id": str(ob.vendor_id) if ob.vendor_id else None,
        "amount": str(ob.amount if ob.amount is not None else Decimal("0")),
        "cadence": ob.cadence,
        "start_date": _to_iso(ob.start_date),
        "end_date": _to_iso(ob.end_date),
        "term_total_occurrences": ob.term_total_occurrences,
        "scheduled_changes": ob.scheduled_changes or [],
        "cost_type": ob.cost_type,
        "is_estimate": bool(ob.is_estimate),
        "source": ob.source,
        "active": bool(ob.active),
        "notes": ob.notes,
        "created_at": _to_iso(ob.created_at),
        "updated_at": _to_iso(ob.updated_at),
    }


def _active_obligations(db: Session, tenant_id: str) -> list[OverheadObligation]:
    return list(
        db.execute(
            select(OverheadObligation).where(
                OverheadObligation.company_id == tenant_id,
                OverheadObligation.active.is_(True),
                OverheadObligation.deleted_at.is_(None),
            )
        ).scalars().all()
    )


def _get_or_404(db: Session, tenant_id: str, obligation_id: UUID) -> OverheadObligation:
    ob = db.execute(
        select(OverheadObligation).where(
            OverheadObligation.id == obligation_id,
            OverheadObligation.company_id == tenant_id,
            OverheadObligation.deleted_at.is_(None),
        )
    ).scalars().first()
    if ob is None:
        raise HTTPException(404, "overhead obligation not found")
    return ob


# ---------- routes ----------


@router.get("", dependencies=[Depends(require_permission("accounting.read"))])
def list_obligations(
    request: Request,
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List overhead obligations plus the current monthly-equivalent total."""
    tenant_id = _tenant_id(request)
    stmt = select(OverheadObligation).where(
        OverheadObligation.company_id == tenant_id,
        OverheadObligation.deleted_at.is_(None),
    )
    if not include_inactive:
        stmt = stmt.where(OverheadObligation.active.is_(True))
    rows = list(db.execute(stmt.order_by(OverheadObligation.label)).scalars().all())

    today = datetime.now(UTC).date()
    summary = project(
        [r for r in rows if r.active],
        anchor_year=today.year,
        anchor_month=today.month,
        horizon_months=1,
    )
    return {
        "obligations": [_obligation_to_dict(r) for r in rows],
        "current_monthly_total": str(summary["current_monthly_total"]),
        "categories": CATEGORIES,
        "cadences": sorted(CADENCES),
        "cost_types": sorted(COST_TYPES),
    }


@router.get("/projection", dependencies=[Depends(require_permission("accounting.read"))])
def get_projection(
    request: Request,
    horizon_months: int = Query(default=12, ge=1, le=MAX_HORIZON_MONTHS),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Forward month-by-month overhead projection from the current month."""
    tenant_id = _tenant_id(request)
    obligations = _active_obligations(db, tenant_id)
    today = datetime.now(UTC).date()
    result = project(
        obligations,
        anchor_year=today.year,
        anchor_month=today.month,
        horizon_months=horizon_months,
    )
    # Decimals -> strings for JSON precision.
    result["current_monthly_total"] = str(result["current_monthly_total"])
    result["horizon_total"] = str(result["horizon_total"])
    for mo in result["months"]:
        mo["total"] = str(mo["total"])
        mo["by_category"] = {k: str(v) for k, v in mo["by_category"].items()}
    for sd in result["step_downs"]:
        sd["drop"] = str(sd["drop"])
    result["disclaimer"] = (
        "Outflow only — this is overhead you must pay, not runway. "
        "Completeness depends on what's entered here; variable costs are "
        "projected flat in v1."
    )
    return result


@router.post("", status_code=201, dependencies=[Depends(require_permission("accounting.write"))])
def create_obligation(
    payload: ObligationIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    if payload.end_date and payload.end_date < payload.start_date:
        raise HTTPException(400, "end_date cannot be before start_date")
    ob = OverheadObligation(
        id=uuid4(),
        company_id=tenant_id,
        label=payload.label,
        category=payload.category,
        vendor_id=payload.vendor_id,
        amount=payload.amount,
        cadence=payload.cadence,
        start_date=payload.start_date,
        end_date=payload.end_date,
        term_total_occurrences=payload.term_total_occurrences,
        scheduled_changes=_changes_to_json(payload.scheduled_changes),
        cost_type=payload.cost_type,
        is_estimate=payload.is_estimate,
        source="manual",
        notes=payload.notes,
    )
    db.add(ob)
    db.commit()
    db.refresh(ob)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(user),
        action="overhead_obligation_created",
        entity_type="overhead_obligation",
        entity_id=str(ob.id),
        details={"label": ob.label, "amount": str(ob.amount), "cadence": ob.cadence},
    )
    db.commit()
    return _obligation_to_dict(ob)


@router.patch("/{obligation_id}", dependencies=[Depends(require_permission("accounting.write"))])
def update_obligation(
    obligation_id: UUID,
    payload: ObligationPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    ob = _get_or_404(db, tenant_id, obligation_id)
    data = payload.model_dump(exclude_unset=True)
    if "scheduled_changes" in data:
        ob.scheduled_changes = _changes_to_json(payload.scheduled_changes)
        data.pop("scheduled_changes")
    for field, value in data.items():
        setattr(ob, field, value)
    new_start = ob.start_date
    if ob.end_date and new_start and ob.end_date < new_start:
        raise HTTPException(400, "end_date cannot be before start_date")
    db.commit()
    db.refresh(ob)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(user),
        action="overhead_obligation_updated",
        entity_type="overhead_obligation",
        entity_id=str(ob.id),
        details={"fields": sorted(data.keys())},
    )
    db.commit()
    return _obligation_to_dict(ob)


@router.delete("/{obligation_id}", status_code=204, dependencies=[Depends(require_permission("accounting.write"))])
def delete_obligation(
    obligation_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    tenant_id = _tenant_id(request)
    ob = _get_or_404(db, tenant_id, obligation_id)
    ob.deleted_at = datetime.now(UTC)
    ob.active = False
    db.commit()
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(user),
        action="overhead_obligation_deleted",
        entity_type="overhead_obligation",
        entity_id=str(ob.id),
        details={"label": ob.label},
    )
    db.commit()
