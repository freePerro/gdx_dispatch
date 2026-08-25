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
# M28: the $95 constant is dead. It disagreed with labor.py's $65 and the
# tenant's real wage-plus-burden number in pricing_settings — the same time
# entry could display three different costs. labor.py's _cost_rate_fallback
# (imported in the block above, like ui_compat already does) is THE source.
OVERHEAD_PERCENT = Decimal("8.00")  # applied to labor+parts


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


from gdx_dispatch.models.tenant_models import MarkupRule  # noqa: E402
from gdx_dispatch.routers.labor import _cost_rate_fallback  # noqa: E402

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
        return {"hours": 0.0, "rate": float(_cost_rate_fallback(db)), "total": 0.0}
    except Exception:
        log.exception("time_entries_unexpected_error job_id=%s", job_id)
        db.rollback()
        return {"hours": 0.0, "rate": float(_cost_rate_fallback(db)), "total": 0.0}

    fallback = Decimal(str(_cost_rate_fallback(db)))
    total_minutes = Decimal("0")
    weighted_total = Decimal("0")
    for r in rows:
        minutes = Decimal(str(r[0] or 0))
        # M28: `r[1] or fallback` re-rated a DELIBERATE $0 (warranty/comp) to
        # $95/h — a 3-hour warranty entry cost $0 on labor.py's endpoint and
        # $285 here, and the profitability report ranked the job a loser.
        # labor.py fixed this exact trap and said so; the fallback applies
        # ONLY when no rate was stored.
        rate = fallback if r[1] is None else Decimal(str(r[1]))
        total_minutes += minutes
        weighted_total += (minutes / Decimal("60")) * rate
    hours = total_minutes / Decimal("60") if total_minutes > 0 else Decimal("0")
    avg_rate = (weighted_total / hours) if hours > 0 else fallback
    return {
        "hours": float(hours.quantize(Decimal("0.01"))),
        "rate": float(avg_rate.quantize(Decimal("0.01"))),
        "total": float(weighted_total.quantize(Decimal("0.01"))),
    }


def _parts_for_job(db: Session, job_id: UUID) -> dict[str, Any]:
    """Cost the parts a job consumed. Bill if we have one, catalog if we don't.

    Owner's rules (2026-08-25):
      * only parts a tech confirmed USED carry cost — a request is a wish, and
        68 of this tenant's 73 rows are requests;
      * "the catalog is the estimated cost but the bill from the vendor is what
        counts";
      * "we should be able to see the diff in case we need to change pricing".

    Resolution, in order, with NO inference anywhere:

      actual   — a `vendor_invoice_lines` row whose `job_part_needed_id` points
                 at this part. That link is set by the office when it confirms
                 the line (`fulfils_part_id`), never guessed: a bill line
                 carries no SKU, only the vendor's free text, so which part it
                 paid for is not derivable. Matching it by name is what
                 AUDIT-R1 forbids (`core/part_pricing.py`: "Matching is exact
                 SKU only … that ruling stands").
      estimate — the catalog the estimator prices from, matched on EXACT SKU.
                 Covers 100% of the rows that count on this tenant: closeout
                 capture has SKU autocomplete, so all 4 `used` rows carry a SKU
                 and all 4 resolve. The poorly-SKU'd rows are requests, which
                 this function ignores by rule.
      unknown  — no link and no exact SKU match. Listed, contributes nothing.

    `catalog_variance` is actual − estimated for parts that have both, measured
    on the BILL LINE's own quantity so split bills accumulate rather than
    overwrite. Positive means the supplier charged more than the catalog says.

    Two earlier implementations were pulled before merge, both for pricing rows
    without first deciding which rows count: the first double-counted because
    `confirm.py` mints a new per-event row rather than linking the tech's, the
    second reached for name matching to paper over that. The fix was never in
    this query — it was giving the office a way to state the link.
    """
    from sqlalchemy import select as _sel
    from sqlalchemy import text as _text

    from gdx_dispatch.modules.inventory.models import JobPart, Part
    from gdx_dispatch.modules.vendor_invoices.models import (
        KIND_ITEM,
        LINE_CONFIRMED,
        VendorInvoice,
        VendorInvoiceLine,
    )

    items: list[dict[str, Any]] = []
    actual_total = Decimal("0")
    estimated_total = Decimal("0")
    variance_total = Decimal("0")
    unknown = 0

    # ── Actual: confirmed item lines, and the part each one paid for ─────────
    # kind='item' because `line.job_id` is set outside confirm.py's KIND_ITEM
    # guard, so freight and tax rows also carry a job_id; they are costs but not
    # PARTS cost. Soft-deleted invoices are excluded — invariant #2 applies to
    # reads too, or a voided bill keeps charging the job.
    billed_part_ids: set[str] = set()
    billed_qty: dict[str, Decimal] = {}
    billed_subtotal: dict[str, Decimal] = {}
    try:
        # ORM, not raw SQL with a stringified UUID. `vendor_invoice_lines.job_id`
        # is a SQLAlchemy `Uuid`, which SQLite stores as 32 hex chars with NO
        # dashes while `str(job_id)` produces the dashed form — a `text()` query
        # binding the string matches ZERO rows there, silently, so "actual" cost
        # would never appear in dev while looking fine on Postgres. Letting the
        # type coerce the bind is the only version that is right on both.
        rows = db.execute(
            _sel(
                VendorInvoiceLine.description,
                VendorInvoiceLine.quantity,
                VendorInvoiceLine.unit_cost,
                VendorInvoiceLine.line_total,
                VendorInvoiceLine.job_part_needed_id,
            )
            .join(VendorInvoice, VendorInvoice.id == VendorInvoiceLine.vendor_invoice_id)
            .where(
                VendorInvoiceLine.job_id == job_id,
                VendorInvoiceLine.kind == KIND_ITEM,
                VendorInvoiceLine.status == LINE_CONFIRMED,
                VendorInvoice.deleted_at.is_(None),
            )
        ).all()
    except Exception:
        log.exception("vendor_line_parts_query_failed job_id=%s", job_id)
        db.rollback()
        rows = []

    unlinked_bill_lines = 0
    for description, quantity, unit_cost, line_total, jpn_id in rows:
        subtotal = Decimal(str(line_total or 0)).quantize(Decimal("0.01"))
        actual_total += subtotal
        if not jpn_id:
            unlinked_bill_lines += 1
        if jpn_id:
            pid = str(jpn_id)
            billed_part_ids.add(pid)
            # ACCUMULATE, never overwrite: one part can be billed across several
            # lines, and overwriting made the variance nonsense in an earlier
            # attempt.
            billed_qty[pid] = billed_qty.get(pid, Decimal("0")) + Decimal(str(quantity or 0))
            billed_subtotal[pid] = billed_subtotal.get(pid, Decimal("0")) + subtotal
        items.append(
            {
                "name": str(description or "Part"),
                "qty": float(Decimal(str(quantity or 0))),
                "unit_cost": float(unit_cost or 0),
                "subtotal": float(subtotal),
                "source": "vendor_bill",
                "cost_known": True,
                "is_estimate": False,
            }
        )

    # ── Actual: inventory consumption (job_parts) ────────────────────────────
    # The canonical cost table wherever a `parts` catalog exists. Empty on this
    # tenant (it FKs to `parts`, which is also empty) but kept and guarded:
    # dropping it is how a prod bug once made every job's parts cost read $0,
    # and `test_parts_for_job_sums_via_parts_join` exists to catch exactly that.
    # Uses `unit_cost_at_time` — costing must not retroactively re-price.
    try:
        inv_rows = db.execute(
            _sel(Part.name, JobPart.qty_used, JobPart.unit_cost_at_time)
            .join(Part, Part.id == JobPart.part_id)
            .where(JobPart.job_id == job_id)
        ).fetchall()
    except OperationalError:
        log.exception("job_parts_query_failed job_id=%s", job_id)
        db.rollback()
        inv_rows = []
    except Exception:
        log.exception("job_parts_unexpected_error job_id=%s", job_id)
        db.rollback()
        inv_rows = []

    for r in inv_rows:
        qty = Decimal(str(r[1] or 0))
        unit_cost = Decimal(str(r[2] or 0))
        subtotal = (qty * unit_cost).quantize(Decimal("0.01"))
        actual_total += subtotal
        items.append(
            {
                "name": str(r[0] or "Part"),
                "qty": float(qty),
                "unit_cost": float(unit_cost),
                "subtotal": float(subtotal),
                "source": "inventory",
                "cost_known": True,
                "is_estimate": False,
            }
        )

    # ── Estimate: used parts with no bill line pointing at them ──────────────
    # EXACT SKU ONLY (AUDIT-R1). `ORDER BY updated_at DESC` so a duplicate SKU
    # resolves to the most recently maintained row rather than arbitrarily.
    try:
        rows = db.execute(
            _text(
                """
                SELECT pn.id, pn.part_name, pn.quantity,
                       COALESCE(
                         (SELECT c.cost FROM custom_catalog_items c
                           WHERE LOWER(TRIM(c.sku)) = LOWER(TRIM(pn.sku)) AND c.cost > 0
                             AND c.deleted_at IS NULL AND c.active
                           ORDER BY c.updated_at DESC LIMIT 1),
                         (SELECT k.cost FROM chi_parts_catalog k
                           WHERE LOWER(TRIM(k.sku)) = LOWER(TRIM(pn.sku)) AND k.cost > 0
                             AND k.is_active
                           ORDER BY k.imported_at DESC LIMIT 1)
                       ) AS catalog_cost
                FROM job_parts_needed pn
                WHERE pn.job_id = :job_id
                  AND pn.status = 'used'
                  AND pn.sku IS NOT NULL AND TRIM(pn.sku) <> ''
                """
            ),
            {"job_id": str(job_id)},
        ).mappings().all()
    except Exception:
        log.exception("job_parts_needed_query_failed job_id=%s", job_id)
        db.rollback()
        rows = []

    for r in rows:
        pid = str(r["id"])
        qty = Decimal(str(r["quantity"] or 1))
        cat = r["catalog_cost"]
        cat_unit = Decimal(str(cat)) if cat is not None else None

        if pid in billed_part_ids:
            # A bill paid for this part — it is already in actual_total.
            if cat_unit is not None:
                # Compare like with like: what the supplier charged against what
                # the catalog says THAT SAME quantity costs.
                est_at_billed_qty = (billed_qty[pid] * cat_unit).quantize(Decimal("0.01"))
                variance_total += billed_subtotal[pid] - est_at_billed_qty

            # A PARTIAL bill must not swallow the rest. Used 4, billed 1 leaves
            # 3 units genuinely consumed and genuinely uncosted; treating the
            # part as "done" because one line mentions it is an undercount, and
            # an unflagged one — the same direction of error (margin overstated)
            # as the silent $0 this whole function replaced.
            shortfall = qty - billed_qty[pid]
            if shortfall > 0:
                if cat_unit is not None:
                    sub = (shortfall * cat_unit).quantize(Decimal("0.01"))
                    estimated_total += sub
                    items.append(
                        {
                            "name": f"{r['part_name'] or 'Part'} (unbilled remainder)",
                            "qty": float(shortfall),
                            "unit_cost": float(cat_unit),
                            "subtotal": float(sub),
                            "source": "catalog_estimate",
                            "cost_known": True,
                            "is_estimate": True,
                        }
                    )
                else:
                    unknown += 1
            continue

        if cat_unit is None:
            unknown += 1
            items.append(
                {
                    "name": str(r["part_name"] or "Part"),
                    "qty": float(qty),
                    "unit_cost": None,
                    "subtotal": None,
                    "source": "parts_needed",
                    "cost_known": False,
                    "is_estimate": False,
                }
            )
            continue

        subtotal = (qty * cat_unit).quantize(Decimal("0.01"))
        estimated_total += subtotal
        items.append(
            {
                "name": str(r["part_name"] or "Part"),
                "qty": float(qty),
                "unit_cost": float(cat_unit),
                "subtotal": float(subtotal),
                "source": "catalog_estimate",
                "cost_known": True,
                "is_estimate": True,
            }
        )

    # Used parts with NO sku cannot be priced at all — count them so the job
    # reads as incomplete rather than silently cheap.
    try:
        no_sku = db.execute(
            _text(
                """
                SELECT id, part_name, quantity FROM job_parts_needed
                WHERE job_id = :job_id AND status = 'used'
                  AND (sku IS NULL OR TRIM(sku) = '')
                """
            ),
            {"job_id": str(job_id)},
        ).mappings().all()
    except Exception:
        log.exception("job_parts_needed_nosku_query_failed job_id=%s", job_id)
        db.rollback()
        no_sku = []

    for r in no_sku:
        if str(r["id"]) in billed_part_ids:
            continue
        unknown += 1
        items.append(
            {
                "name": str(r["part_name"] or "Part"),
                "qty": float(Decimal(str(r["quantity"] or 1))),
                "unit_cost": None,
                "subtotal": None,
                "source": "parts_needed",
                "cost_known": False,
                "is_estimate": False,
            }
        )

    # A bill line attributed to the job but NOT linked to a part means we do not
    # know WHICH part it paid for — and it may well be one of the parts we just
    # estimated from the catalog. Adding both is a guaranteed overcount of one
    # physical part, roughly doubling it.
    #
    # An earlier revision of this function did exactly that and shipped a test
    # asserting it was correct. It is not: it replaced a known $0 with an
    # unknown ~2x, which is the worse error because it looks like a number.
    #
    # So when anything is unattributed, `total` carries only what we can
    # evidence — the bills — and the estimates are reported alongside, marked
    # ambiguous, without being summed into the figure that feeds margin. The job
    # reads INCOMPLETE, which is the truth: we know money was spent and parts
    # were used, and we cannot say whether they are the same money.
    ambiguous = unlinked_bill_lines > 0 and estimated_total > 0
    if ambiguous:
        for it in items:
            if it.get("is_estimate"):
                it["ambiguous"] = True
        total = actual_total
    else:
        total = actual_total + estimated_total

    return {
        "items": items,
        "total": float(total.quantize(Decimal("0.01"))),
        "unlinked_bill_lines": unlinked_bill_lines,
        "estimates_ambiguous": ambiguous,
        "actual_cost_total": float(actual_total.quantize(Decimal("0.01"))),
        "estimated_cost_total": float(estimated_total.quantize(Decimal("0.01"))),
        "catalog_variance": float(variance_total.quantize(Decimal("0.01"))),
        "unknown_cost_count": unknown,
    }


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
        # Promoted so a caller reading only the top level cannot miss them.
        # "Incomplete" means the cost is NOT SETTLED — parts nobody costed, OR
        # bills we could not attribute, OR a figure resting on catalog guesses.
        # A previous revision set this False whenever every part happened to
        # resolve to an estimate, i.e. "complete" meant "we guessed everything
        # successfully". That is the opposite of what a reader needs.
        "cost_incomplete": bool(
            parts.get("unknown_cost_count", 0)
            or parts.get("unlinked_bill_lines", 0)
            or parts.get("estimated_cost_total", 0.0)
        ),
        "unknown_cost_parts": int(parts.get("unknown_cost_count", 0)),
        "unlinked_bill_lines": int(parts.get("unlinked_bill_lines", 0)),
        "estimates_ambiguous": bool(parts.get("estimates_ambiguous", False)),
        "estimated_parts_cost": float(parts.get("estimated_cost_total", 0.0)),
        "actual_parts_cost": float(parts.get("actual_cost_total", 0.0)),
        # actual − catalog on the billed quantity. Positive = the supplier
        # charged more than the catalog says, so anything priced off that
        # catalog is under-recovering. This is the number to act on.
        "catalog_variance": float(parts.get("catalog_variance", 0.0)),
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
                # The same caveats the per-job endpoint carries. This is the
                # report an owner reads to judge margin, so stripping them here
                # would be the worst place to hide them.
                "cost_incomplete": bool(
                    parts.get("unknown_cost_count", 0)
                    or parts.get("unlinked_bill_lines", 0)
                    or parts.get("estimated_cost_total", 0.0)
                ),
                "estimates_ambiguous": bool(parts.get("estimates_ambiguous", False)),
                "estimated_parts_cost": float(parts.get("estimated_cost_total", 0.0)),
                "actual_parts_cost": float(parts.get("actual_cost_total", 0.0)),
                "catalog_variance": float(parts.get("catalog_variance", 0.0)),
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
