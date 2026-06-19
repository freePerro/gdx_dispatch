"""
Job Costing router — per-job cost breakdown, markup rules, price calculator.

Profitability tooling:
- GET /api/costing/jobs/{job_id} — labor + parts + overhead + margin breakdown
- CRUD /api/costing/markup-rules — per-category markup/minimum-margin rules
- POST /api/costing/calculate-price — suggested price with markup + margin floor
- GET /api/costing/profitability — aggregate per-job profitability over N days
- GET /api/costing/catalog-pricing — list all markup rules (for settings UI)

Reads labor from `time_entries`, parts from `job_parts` / `inventory_items`,
and invoiced totals from `invoices`. Missing tables degrade to zeros. No
database-specific SQL (Python-computed ids/timestamps, bound parameters).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import (
    select,
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["job_costing"],
    dependencies=[Depends(require_module("jobs")), Depends(require_role("admin", "owner", "superadmin"))],
)

# Defaults when a tenant has no markup rule for a category.
DEFAULT_MARKUP_PERCENT = Decimal("35.00")
DEFAULT_LABOR_RATE = Decimal("95.00")  # $/hour fallback
OVERHEAD_PERCENT = Decimal("8.00")  # applied to labor+parts


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import MarkupRule  # noqa: E402

# ---------------------------------------------------------------------------
# Pydantic schemas (bounded)
# ---------------------------------------------------------------------------


class MarkupRuleIn(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    markup_percent: float = Field(ge=0, le=1000)
    minimum_margin_percent: float = Field(default=0, ge=0, le=99)
    active: bool = True


class MarkupRulePatch(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=100)
    markup_percent: float | None = Field(default=None, ge=0, le=1000)
    minimum_margin_percent: float | None = Field(default=None, ge=0, le=99)
    active: bool | None = None


class PriceCalcIn(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    cost: float = Field(ge=0, le=10_000_000)


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


def _serialize_rule(r: MarkupRule) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "company_id": r.company_id,
        "category": r.category,
        "markup_percent": float(r.markup_percent or 0),
        "minimum_margin_percent": float(r.minimum_margin_percent or 0),
        "active": bool(r.active),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _get_scoped_rule(db: Session, rule_id: UUID, tenant_id: str) -> MarkupRule:
    row = db.execute(
        select(MarkupRule).where(
            MarkupRule.id == rule_id,
            MarkupRule.company_id == tenant_id,
            MarkupRule.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Markup rule not found")
    return row


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
            entity_type="markup_rule",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("job_costing_audit_failed action=%s entity_id=%s", action, entity_id)
        db.rollback()


def _apply_markup(
    cost: Decimal, markup_percent: Decimal, min_margin_percent: Decimal
) -> Decimal:
    """Apply markup, then enforce min margin floor.

    margin_percent = (price - cost) / price * 100
    => price_needed_for_min_margin = cost / (1 - min_margin/100)
    """
    if cost <= 0:
        return Decimal("0")
    markup_price = cost * (Decimal("1") + markup_percent / Decimal("100"))
    if min_margin_percent and min_margin_percent < Decimal("100"):
        floor_divisor = Decimal("1") - (min_margin_percent / Decimal("100"))
        if floor_divisor > 0:
            min_price = cost / floor_divisor
            if min_price > markup_price:
                return min_price.quantize(Decimal("0.01"))
    return markup_price.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Cost analysis: GET /api/costing/jobs/{job_id}
# ---------------------------------------------------------------------------


def _labor_for_job(db: Session, job_id: UUID) -> dict[str, Any]:
    """Sum time_entries minutes and rate for job. Best-effort: tables may not exist."""
    try:
        from sqlalchemy import select as _sel

        from gdx_dispatch.models.tenant_models import TimeEntry
        rows = db.execute(
            _sel(TimeEntry.duration_minutes, TimeEntry.hourly_rate)
            .where(TimeEntry.job_id == job_id, TimeEntry.deleted_at.is_(None))
        ).fetchall()
    except OperationalError:
        log.exception("time_entries_query_failed job_id=%s", job_id)
        db.rollback()
        return {"hours": 0.0, "rate": float(DEFAULT_LABOR_RATE), "total": 0.0}
    except Exception:
        log.exception("time_entries_unexpected_error job_id=%s", job_id)
        db.rollback()
        return {"hours": 0.0, "rate": float(DEFAULT_LABOR_RATE), "total": 0.0}

    total_minutes = Decimal("0")
    weighted_total = Decimal("0")
    for r in rows:
        minutes = Decimal(str(r[0] or 0))
        rate = Decimal(str(r[1] or DEFAULT_LABOR_RATE))
        total_minutes += minutes
        weighted_total += (minutes / Decimal("60")) * rate
    hours = total_minutes / Decimal("60") if total_minutes > 0 else Decimal("0")
    avg_rate = (weighted_total / hours) if hours > 0 else DEFAULT_LABOR_RATE
    return {
        "hours": float(hours.quantize(Decimal("0.01"))),
        "rate": float(avg_rate.quantize(Decimal("0.01"))),
        "total": float(weighted_total.quantize(Decimal("0.01"))),
    }


def _parts_for_job(db: Session, job_id: UUID) -> dict[str, Any]:
    """Sum job_parts rows for job. Columns vary across schemas — best-effort."""
    items: list[dict[str, Any]] = []
    total = Decimal("0")
    try:
        rows = db.execute(
            text(
                "SELECT COALESCE(part_name, description, 'Part') AS name, "
                "COALESCE(quantity, 1) AS qty, "
                "COALESCE(unit_cost, unit_price, 0) AS unit_cost "
                "FROM job_parts "
                "WHERE job_id = :jid AND deleted_at IS NULL"
            ),
            {"jid": str(job_id)},
        ).fetchall()
    except OperationalError:
        log.exception("job_parts_query_failed job_id=%s", job_id)
        db.rollback()
        return {"items": [], "total": 0.0}
    except Exception:
        log.exception("job_parts_unexpected_error job_id=%s", job_id)
        db.rollback()
        return {"items": [], "total": 0.0}

    for r in rows:
        name = str(r[0] or "Part")
        qty = Decimal(str(r[1] or 0))
        unit_cost = Decimal(str(r[2] or 0))
        subtotal = (qty * unit_cost).quantize(Decimal("0.01"))
        total += subtotal
        items.append(
            {
                "name": name,
                "qty": float(qty),
                "unit_cost": float(unit_cost),
                "subtotal": float(subtotal),
            }
        )
    return {"items": items, "total": float(total.quantize(Decimal("0.01")))}


def _invoiced_for_job(db: Session, job_id: UUID, tenant_id: str) -> Decimal:
    try:
        from sqlalchemy import func as _func
        from sqlalchemy import select as _sel

        from gdx_dispatch.models.tenant_models import Invoice
        row = db.execute(
            _sel(_func.coalesce(_func.sum(Invoice.total), 0))
            .where(Invoice.job_id == job_id, Invoice.company_id == tenant_id, Invoice.deleted_at.is_(None))
        ).fetchone()
    except OperationalError:
        log.exception("invoices_query_failed job_id=%s", job_id)
        db.rollback()
        return Decimal("0")
    except Exception:
        log.exception("invoices_unexpected_error job_id=%s", job_id)
        db.rollback()
        return Decimal("0")
    if not row:
        return Decimal("0")
    return Decimal(str(row[0] or 0))


@router.get("/api/costing/jobs/{job_id}", response_model=None)
def get_job_costing(
    job_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)

    labor = _labor_for_job(db, job_id)
    parts = _parts_for_job(db, job_id)
    labor_total = Decimal(str(labor["total"]))
    parts_total = Decimal(str(parts["total"]))
    base = labor_total + parts_total
    overhead_total = (base * OVERHEAD_PERCENT / Decimal("100")).quantize(Decimal("0.01"))
    total_cost = (base + overhead_total).quantize(Decimal("0.01"))
    invoiced = _invoiced_for_job(db, job_id, tenant_id)
    profit = (invoiced - total_cost).quantize(Decimal("0.01"))
    margin_percent = (
        float((profit / invoiced * Decimal("100")).quantize(Decimal("0.01")))
        if invoiced > 0
        else 0.0
    )

    return {
        "job_id": str(job_id),
        "labor": labor,
        "parts": parts,
        "overhead": {
            "percent": float(OVERHEAD_PERCENT),
            "total": float(overhead_total),
        },
        "total_cost": float(total_cost),
        "invoiced_amount": float(invoiced),
        "profit": float(profit),
        "margin_percent": margin_percent,
    }


# ---------------------------------------------------------------------------
# Markup rules CRUD
# ---------------------------------------------------------------------------


@router.get("/api/costing/markup-rules", response_model=None)
def list_markup_rules(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    active_only: bool = True,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    stmt = select(MarkupRule).where(
        MarkupRule.deleted_at.is_(None),
    )
    if active_only:
        stmt = stmt.where(MarkupRule.active.is_(True))
    rows = db.execute(stmt.order_by(MarkupRule.category.asc())).scalars().all()
    return [_serialize_rule(r) for r in rows]


@router.post("/api/costing/markup-rules", response_model=None, status_code=201)
def create_markup_rule(
    payload: MarkupRuleIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    category = payload.category.strip().lower()

    # Duplicate check (active, not soft-deleted) — gives 409 instead of 500 IntegrityError.
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    existing = db.execute(
        select(MarkupRule).where(
            MarkupRule.category == category,
            MarkupRule.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Markup rule already exists for category '{category}'",
        )

    rule = MarkupRule(
        id=uuid4(),
        company_id=tenant_id,
        category=category,
        markup_percent=Decimal(str(payload.markup_percent)),
        minimum_margin_percent=Decimal(str(payload.minimum_margin_percent)),
        active=payload.active,
    )
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        log.exception("markup_rule_unique_violation tenant=%s cat=%s", tenant_id, category)
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Markup rule already exists for category '{category}'",
        ) from None
    db.refresh(rule)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="markup_rule_created",
        entity_id=str(rule.id),
        details={"category": category, "markup_percent": float(rule.markup_percent)},
        request=request,
    )
    return _serialize_rule(rule)


@router.patch("/api/costing/markup-rules/{rule_id}", response_model=None)
def update_markup_rule(
    rule_id: UUID,
    payload: MarkupRulePatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    data = payload.model_dump(exclude_unset=True)

    if "category" in data and data["category"] is not None:
        new_cat = str(data["category"]).strip().lower()
        if new_cat != rule.category:
            # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
            clash = db.execute(
                select(MarkupRule).where(
                    MarkupRule.category == new_cat,
                    MarkupRule.deleted_at.is_(None),
                    MarkupRule.id != rule.id,
                )
            ).scalar_one_or_none()
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail=f"Markup rule already exists for category '{new_cat}'",
                )
            rule.category = new_cat
    if "markup_percent" in data and data["markup_percent"] is not None:
        rule.markup_percent = Decimal(str(data["markup_percent"]))
    if "minimum_margin_percent" in data and data["minimum_margin_percent"] is not None:
        rule.minimum_margin_percent = Decimal(str(data["minimum_margin_percent"]))
    if "active" in data and data["active"] is not None:
        rule.active = bool(data["active"])

    try:
        db.commit()
    except IntegrityError:
        log.exception("markup_rule_update_integrity_error rule_id=%s", rule_id)
        db.rollback()
        raise HTTPException(status_code=409, detail="Markup rule conflict") from None
    db.refresh(rule)
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="markup_rule_updated",
        entity_id=str(rule.id),
        details={"fields": list(data.keys())},
        request=request,
    )
    return _serialize_rule(rule)


@router.delete("/api/costing/markup-rules/{rule_id}", response_model=None, status_code=204)
def delete_markup_rule(
    rule_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_id = _tenant_id(request)
    rule = _get_scoped_rule(db, rule_id, tenant_id)
    rule.deleted_at = utcnow()
    rule.active = False
    db.commit()
    _audit(
        db,
        tenant_id=tenant_id,
        user=user,
        action="markup_rule_deleted",
        entity_id=str(rule_id),
        request=request,
    )
    return None


# ---------------------------------------------------------------------------
# Price calculator
# ---------------------------------------------------------------------------


@router.post("/api/costing/calculate-price", response_model=None)
def calculate_price(
    payload: PriceCalcIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    category = payload.category.strip().lower()
    cost = Decimal(str(payload.cost))

    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rule = db.execute(
        select(MarkupRule).where(
            MarkupRule.category == category,
            MarkupRule.deleted_at.is_(None),
            MarkupRule.active.is_(True),
        )
    ).scalar_one_or_none()

    if rule:
        markup_percent = Decimal(str(rule.markup_percent or 0))
        min_margin = Decimal(str(rule.minimum_margin_percent or 0))
        rule_id = str(rule.id)
    else:
        markup_percent = DEFAULT_MARKUP_PERCENT
        min_margin = Decimal("0")
        rule_id = None

    suggested = _apply_markup(cost, markup_percent, min_margin)
    # "min_price": the floor implied by min_margin (or markup price if no min).
    if min_margin > 0 and min_margin < Decimal("100"):
        divisor = Decimal("1") - (min_margin / Decimal("100"))
        min_price = (cost / divisor).quantize(Decimal("0.01")) if divisor > 0 else suggested
    else:
        min_price = suggested

    return {
        "cost": float(cost),
        "category": category,
        "markup_percent": float(markup_percent),
        "minimum_margin_percent": float(min_margin),
        "suggested_price": float(suggested),
        "min_price": float(min_price),
        "rule_id": rule_id,
    }


# ---------------------------------------------------------------------------
# Profitability report
# ---------------------------------------------------------------------------


@router.get("/api/costing/profitability", response_model=None)
def profitability_report(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int = 30,
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    days = max(1, min(int(days or 30), 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    try:
        from sqlalchemy import func as _func
        from sqlalchemy import select as _sel

        from gdx_dispatch.models.tenant_models import Invoice
        rows = db.execute(
            _sel(Invoice.job_id, _func.coalesce(_func.sum(Invoice.total), 0).label("invoice_total"))
            .where(Invoice.company_id == tenant_id, Invoice.deleted_at.is_(None), Invoice.created_at >= cutoff)
            .group_by(Invoice.job_id)
        ).fetchall()
    except OperationalError:
        log.exception("profitability_invoices_query_failed tenant=%s", tenant_id)
        db.rollback()
        raise
    except Exception:
        log.exception("profitability_unexpected_error tenant=%s", tenant_id)
        db.rollback()
        raise RuntimeError("Failed to fetch profitability data due to database error") from None

    # Pull job titles + customer names for the rows we're about to return,
    # so the UI never renders a raw UUID. Single batched query.
    job_uuids: list[UUID] = []
    for r in rows:
        raw = r[0]
        if raw is None:
            continue
        try:
            job_uuids.append(raw if isinstance(raw, UUID) else UUID(str(raw)))
        except (ValueError, TypeError):
            continue

    name_map: dict[str, dict[str, Any]] = {}
    if job_uuids:
        try:
            from sqlalchemy import select as _sel

            from gdx_dispatch.models.tenant_models import Customer, Job
            label_rows = db.execute(
                _sel(Job.id, Job.title, Job.job_number, Customer.name)
                .join(Customer, Customer.id == Job.customer_id, isouter=True)
                .where(Job.id.in_(job_uuids))
            ).all()
            for jid, title, job_number, customer_name in label_rows:
                name_map[str(jid)] = {
                    "job_title": title,
                    "job_number": job_number,
                    "customer_name": customer_name,
                }
        except Exception:
            log.exception("profitability_label_lookup_failed tenant=%s", tenant_id)

    out: list[dict[str, Any]] = []
    for r in rows:
        raw_job_id = r[0]
        if raw_job_id is None:
            continue
        try:
            job_uuid = raw_job_id if isinstance(raw_job_id, UUID) else UUID(str(raw_job_id))
        except (ValueError, TypeError):
            log.exception("profitability_report_failed")
            continue
        invoice_total = Decimal(str(r[1] or 0))
        labor = _labor_for_job(db, job_uuid)
        parts = _parts_for_job(db, job_uuid)
        base = Decimal(str(labor["total"])) + Decimal(str(parts["total"]))
        overhead = (base * OVERHEAD_PERCENT / Decimal("100"))
        cost_estimate = (base + overhead).quantize(Decimal("0.01"))
        profit = (invoice_total - cost_estimate).quantize(Decimal("0.01"))
        margin = (
            float((profit / invoice_total * Decimal("100")).quantize(Decimal("0.01")))
            if invoice_total > 0
            else 0.0
        )
        labels = name_map.get(str(job_uuid), {})
        out.append(
            {
                "job_id": str(job_uuid),
                "job_title": labels.get("job_title"),
                "job_number": labels.get("job_number"),
                "customer_name": labels.get("customer_name"),
                "invoice_total": float(invoice_total),
                "cost_estimate": float(cost_estimate),
                "profit": float(profit),
                "margin_percent": margin,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Catalog pricing (read-only projection of markup rules)
# ---------------------------------------------------------------------------


@router.get("/api/costing/catalog-pricing", response_model=None)
def catalog_pricing(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    rows = db.execute(
        select(MarkupRule)
        .where(
            MarkupRule.deleted_at.is_(None),
        )
        .order_by(MarkupRule.category.asc())
    ).scalars().all()
    return [
        {
            "category": r.category,
            "markup_percent": float(r.markup_percent or 0),
            "minimum_margin_percent": float(r.minimum_margin_percent or 0),
            "active": bool(r.active),
        }
        for r in rows
    ]
