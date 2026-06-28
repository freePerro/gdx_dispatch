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

# Keyword → category, checked in order (first match wins). Only a starting guess
# for a stream-seeded suggestion; the user can change it before saving.
_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("payroll", ("payroll", "gusto", "adp", "paychex", "wage", "salary")),
    ("insurance", ("insurance", "insur", "policy", "geico", "allstate", "statefarm", "state farm", "liberty mutual", "progressive")),
    ("loan", ("loan", "lienholder", "ally financ", "capital one auto", "lending", "note payment")),
    ("tax", ("tax", "irs", "franchise tax", "dept of revenue", "department of revenue")),
    ("utilities", ("electric", "utility", "utilit", "water", "sewer", "energy", "pg&e", "edison", "power co")),
    ("rent", ("rent", "landlord", "property mgmt", "property management", "leasing office")),
    ("lease", ("lease", "leasing")),
    ("subscription", ("subscription", "software", "saas", "adobe", "microsoft", "google", "zoom", "quickbooks", "verizon", "at&t", "comcast", "phone.com", "internet")),
]


def _guess_category(*texts: str | None) -> str:
    """Best-effort category from a payee/label string. Defaults to 'other'."""
    blob = " ".join(t for t in texts if t).lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(kw in blob for kw in keywords):
            return category
    return "other"


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
    # Slice 2: set when the obligation is confirmed from a bank-detected
    # RecurringStream suggestion. Flips source to 'seeded_from_stream'.
    source_stream_id: UUID | None = None

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
        "source_stream_id": str(ob.source_stream_id) if ob.source_stream_id else None,
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


@router.get("/suggestions", dependencies=[Depends(require_permission("accounting.read"))])
def list_suggestions(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Bank-detected recurring outflows (forecasting RecurringStream) that aren't
    yet tracked as overhead — surfaced as DRAFT suggestions to confirm.

    A hint only (ADR-016 Slice 2): amount is the midpoint of the detector's
    tolerance window and the category is a keyword guess; the user confirms and
    sets the payoff/end date (which the detector can't know). Streams already
    confirmed into the register are excluded via source_stream_id.
    """
    tenant_id = _tenant_id(request)
    # Lazy import keeps this router decoupled from the experimental forecasting
    # module at import time (we only reach into it for this read).
    from gdx_dispatch.modules.forecasting.models import (
        STREAM_STATUS_ACTIVE,
        STREAM_STATUS_SUGGESTED,
        RecurringStream,
    )

    linked = {
        sid
        for sid in db.execute(
            select(OverheadObligation.source_stream_id).where(
                OverheadObligation.company_id == tenant_id,
                OverheadObligation.source_stream_id.isnot(None),
                OverheadObligation.deleted_at.is_(None),
            )
        ).scalars().all()
    }

    streams = db.execute(
        select(RecurringStream).where(
            RecurringStream.status.in_([STREAM_STATUS_SUGGESTED, STREAM_STATUS_ACTIVE]),
            RecurringStream.deleted_at.is_(None),
        ).order_by(RecurringStream.payee_pattern)
    ).scalars().all()

    suggestions: list[dict[str, Any]] = []
    for s in streams:
        if s.id in linked:
            continue
        mid = (Decimal(str(s.amount_min)) + Decimal(str(s.amount_max))) / 2
        suggestions.append({
            "stream_id": str(s.id),
            "label": s.label,
            "payee_pattern": s.payee_pattern,
            "suggested_amount": str(mid.quantize(Decimal("0.01"))),
            "amount_min": str(s.amount_min),
            "amount_max": str(s.amount_max),
            "cadence": s.cadence,
            "suggested_category": _guess_category(s.label, s.payee_pattern, s.account_name),
            "status": s.status,
            "occurrences_seen": s.occurrences_seen,
            # Carry the timeline so a confirmed obligation starts when the stream
            # actually started (not "today") — otherwise a termed loan seen N
            # times would project N already-paid occurrences into the future.
            "start_date": _to_iso(s.start_date or s.last_observed_date),
            "next_expected_date": _to_iso(s.next_expected_date),
            "last_observed_date": _to_iso(s.last_observed_date),
            "term_end_date": _to_iso(s.term_end_date),
            "term_total_occurrences": s.term_total_occurrences,
        })

    return {"suggestions": suggestions, "count": len(suggestions)}


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
    if payload.source_stream_id is not None:
        already = db.execute(
            select(OverheadObligation.id).where(
                OverheadObligation.company_id == tenant_id,
                OverheadObligation.source_stream_id == payload.source_stream_id,
                OverheadObligation.deleted_at.is_(None),
            )
        ).first()
        if already:
            raise HTTPException(409, "this recurring payment is already tracked as overhead")
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
        source="seeded_from_stream" if payload.source_stream_id else "manual",
        source_stream_id=payload.source_stream_id,
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
