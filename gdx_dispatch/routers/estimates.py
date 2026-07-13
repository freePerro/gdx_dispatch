from __future__ import annotations

import logging
import secrets
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

import os
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text as _text
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.models.tenant_models import AppSettings, Customer, Document, Job, JobPartNeeded
from gdx_dispatch.modules.estimates_features import require_line_margin_override_allowed
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/estimates", tags=["estimates"], dependencies=[Depends(require_module("estimates"))])


def _money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _to_float(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return float(value or 0)


def _derive_margin_pct(cost: Decimal | float | None, unit_price: Decimal | float | None) -> Decimal | None:
    """Back-derive a margin_pct_snapshot from cost + unit_price.

    Returns None when either value is missing/<=0 (signals genuine free-form/manual line).
    Otherwise returns (unit_price - cost) / unit_price as a Decimal. Used at line creation
    when the client sent a cost (e.g. CHI / typed-catalog door) without going through the
    engine path, and at PATCH time to heal pre-existing lines that were born with cost
    but NULL margin.
    """
    if cost is None or unit_price is None:
        return None
    c = Decimal(str(cost))
    u = Decimal(str(unit_price))
    if u <= 0 or c < 0:
        return None
    return ((u - c) / u).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _next_estimate_number(db: Session) -> str:
    # Take the max numeric suffix of canonical EST-NNNNNN numbers (across BOTH live and
    # soft-deleted rows — the unique constraint covers everything), +1. Legacy formats
    # like EST-JOB-2026-0018 are ignored. count(*)+1 collides whenever rows are deleted
    # or numbers are skipped (prod incident 2026-05-07: EST-000027 already taken).
    rows = db.execute(select(Estimate.estimate_number)).scalars().all()
    highest = 0
    for n in rows:
        if not n or len(n) != 10 or not n.startswith("EST-"):
            continue
        suffix = n[4:]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"EST-{highest + 1:06d}"


def _serialize_line(line: EstimateLine) -> dict[str, object]:
    # Sprint 1.0.5 — surface snapshot fields so the estimate side panel can
    # compute per-line profit and margin without a server round-trip.
    return {
        "id": str(line.id),
        "estimate_id": str(line.estimate_id),
        "description": line.description,
        "category": getattr(line, "category", None),
        "quantity": line.quantity,
        "unit_price": _to_float(line.unit_price),
        "line_total": _to_float(line.line_total),
        "sort_order": line.sort_order,
        "created_at": line.created_at.isoformat() if line.created_at else None,
        # Snapshot fields — null on legacy lines created before the engine
        "cost_snapshot": _to_float(line.cost_snapshot) if line.cost_snapshot is not None else None,
        "margin_pct_snapshot": _to_float(line.margin_pct_snapshot) if line.margin_pct_snapshot is not None else None,
        "margin_pct_override": _to_float(line.margin_pct_override) if line.margin_pct_override is not None else None,
        "pricing_source": line.pricing_source,
        # S97 slice 4 — labor matrix link + man-hours snapshot.
        "labor_price_item_id": str(line.labor_price_item_id) if line.labor_price_item_id else None,
        "estimated_man_hours": _to_float(line.estimated_man_hours) if line.estimated_man_hours is not None else None,
        # Plugin integration (ADR-013) — captured source spec, null on ordinary lines.
        "line_metadata": getattr(line, "line_metadata", None),
    }


def _serialize_estimate(estimate: Estimate, include_lines: bool = False) -> dict[str, object]:
    payload = {
        "id": str(estimate.id),
        "job_id": str(estimate.job_id) if estimate.job_id else None,
        "customer_id": str(estimate.customer_id) if estimate.customer_id else None,
        "estimate_number": estimate.estimate_number,
        "label": estimate.label,
        "jobsite_address": estimate.jobsite_address,
        "description": estimate.description,
        "notes": estimate.notes,
        "tax_rate": _to_float(estimate.tax_rate) if estimate.tax_rate is not None else None,
        "discount": _to_float(estimate.discount) if estimate.discount is not None else None,
        # Tri-state override: null = inherit tenant default; true/false = explicit.
        # getattr-guarded: always present on a real ORM Estimate, but this helper
        # is also handed lightweight non-ORM stubs in tests — matches the getattr
        # style _serialize_line already uses for optional fields.
        "hide_line_prices": getattr(estimate, "hide_line_prices", None),
        "status": estimate.status,
        "total": _to_float(estimate.total),
        "sent_at": estimate.sent_at.isoformat() if estimate.sent_at else None,
        "accepted_at": estimate.accepted_at.isoformat() if estimate.accepted_at else None,
        "declined_at": estimate.declined_at.isoformat() if estimate.declined_at else None,
        "declined_reason": estimate.declined_reason,
        "created_at": estimate.created_at.isoformat() if estimate.created_at else None,
        "updated_at": estimate.updated_at.isoformat() if estimate.updated_at else None,
        "deleted_at": estimate.deleted_at.isoformat() if estimate.deleted_at else None,
    }
    if include_lines:
        lines = sorted(estimate.lines, key=lambda ln: (ln.sort_order, ln.created_at, ln.id))
        payload["lines"] = [_serialize_line(line) for line in lines]
    return payload


def _get_estimate_or_404(estimate_id: UUID, db: Session, include_lines: bool = False) -> Estimate:
    q = select(Estimate).where(Estimate.id == estimate_id, Estimate.deleted_at.is_(None))
    if include_lines:
        q = q.options(selectinload(Estimate.lines))
    estimate = db.execute(q).scalar_one_or_none()
    if not estimate:
        raise HTTPException(status_code=404, detail="Estimate not found")
    return estimate


def _ensure_editable(estimate: Estimate) -> None:
    if estimate.status in {"accepted", "declined"}:
        raise HTTPException(status_code=409, detail="cannot edit a finalized estimate")


def _recalculate_total(estimate: Estimate, db: Session) -> None:
    total = db.execute(
        select(func.sum(EstimateLine.line_total)).where(EstimateLine.estimate_id == estimate.id)
    ).scalar_one_or_none() or 0
    estimate.total = _money(_to_float(total))
    estimate.updated_at = utcnow()


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


# ── Sprint 1.0.5 — pricing engine integration ────────────────────────────────

def _resolve_customer_for_engine(estimate: Estimate, db: Session):
    """Return CustomerView for the estimate's customer, or anonymous-retail default.

    Anonymous (no customer_id) → retail with no override. This matches what an
    operator sees when pricing a quote before attaching a customer.

    Sprint 1.0.6 — also hydrates `cached_rolling_volume`, refreshing the
    cache opportunistically if it's stale (>1h old). Refresh is best-effort:
    if the customer disappeared mid-flight or the SUM fails, we fall back
    to 0 (no discount) rather than blowing up the estimate.
    """
    from gdx_dispatch.services.pricing_engine import CustomerView
    from gdx_dispatch.services.customer_rolling_volume import get_or_refresh

    if not estimate.customer_id:
        return CustomerView(pricing_class="retail", margin_override_pct=None)
    cust = db.execute(
        select(Customer).where(Customer.id == estimate.customer_id)
    ).scalar_one_or_none()
    if cust is None:
        return CustomerView(pricing_class="retail", margin_override_pct=None)
    pc = cust.pricing_class  # may be None on un-migrated customers
    try:
        rolling_volume = get_or_refresh(cust.id, db)
    except Exception:  # pragma: no cover — defensive
        log.exception("rolling_volume_refresh_failed customer_id=%s", cust.id)
        rolling_volume = Decimal(cust.cached_rolling_volume_paid_12mo or 0)
    return CustomerView(
        pricing_class=pc if pc in ("retail", "contractor", "wholesale") else None,
        margin_override_pct=Decimal(str(cust.margin_override_pct)) if cust.margin_override_pct is not None else None,
        cached_rolling_volume=rolling_volume,
    )


def _resolve_labor_matrix_row(db: Session, labor_price_item_id):
    """Re-read the matrix row at save-time. Client-supplied unit_price /
    cost / hours are not trusted for labor lines — flat_price wins. Returns
    the LaborPriceItem or raises 404 (the FK is ON DELETE SET NULL, so a
    missing row means the operator picked something that was archived after
    they opened the form). Lazy import dodges the test harness load order."""
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    row = db.get(LaborPriceItem, labor_price_item_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="labor_price_item not found (was it archived?)",
        )
    return row


def _labor_line_pricing(
    db: Session,
    *,
    matrix_row,
    quantity: int,
) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    """Single source of truth for labor-line pricing fields. Inputs are the
    matrix row (re-read by `_resolve_labor_matrix_row`) and the line
    quantity. Returns (unit_price, line_total, cost_snapshot, margin, source).

    Per Doug 2026-05-07: flat_price IS the customer-facing sell. Hours
    drives cost-side reporting and scheduling. Tier engine is forbidden on
    labor lines — this function never calls `price_line()`."""
    from gdx_dispatch.models.pricing_engine import PricingSettings

    unit_price = Decimal(str(matrix_row.flat_price)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    qty = Decimal(quantity)
    line_total = (unit_price * qty).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    settings = db.execute(select(PricingSettings)).scalar_one_or_none()
    rate = (
        Decimal(str(settings.loaded_labor_cost_per_hour))
        if settings and settings.loaded_labor_cost_per_hour is not None
        else Decimal("0")
    )
    hours = Decimal(str(matrix_row.assumed_man_hours or 0))
    # Cost is qty-aware so the profit panel (which sums per-line) totals
    # correctly. Hours-on-the-line stays per-unit (matrix authoritative); qty
    # multiplication happens at scheduler/variance read-time too (S6).
    cost_snapshot = (rate * hours * qty).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if line_total > 0:
        margin = (
            (line_total - cost_snapshot) / line_total
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        margin = Decimal("0")
    return unit_price, line_total, cost_snapshot, margin, "labor_matrix"


def _labor_cost_snapshot(
    db: Session,
    *,
    unit_price: Decimal | float,
    estimated_man_hours: Decimal | float | None,
) -> tuple[Decimal, Decimal, str]:
    """Derive (cost_snapshot, margin_pct_snapshot, pricing_source) for a
    labor-matrix-sourced estimate line.

    Reads the tenant-default loaded labor rate from PricingSettings. Always
    returns a non-null cost (0 if rate or hours unknown) so the line shows up
    in the profit panel — silent-null drop is the bug this fixes (Doug
    2026-05-05, EST-000026). Sell is authoritative (flat_price); we fill cost
    backwards, never overwrite sell.
    """
    from gdx_dispatch.models.pricing_engine import PricingSettings

    settings = db.execute(select(PricingSettings)).scalar_one_or_none()
    rate = Decimal(str(settings.loaded_labor_cost_per_hour)) if settings else Decimal("0")
    hours = Decimal(str(estimated_man_hours)) if estimated_man_hours is not None else Decimal("0")
    cost = (rate * hours).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sell = Decimal(str(unit_price or 0))
    if sell > 0:
        margin = ((sell - cost) / sell).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    else:
        margin = Decimal("0")
    return cost, margin, "labor_matrix"


def _engine_price_line(
    db: Session,
    estimate: Estimate,
    cost: float,
    pricing_category: str,
    margin_override: float | None,
):
    """Wrap engine call. Returns LinePrice or raises HTTPException(409) on config error."""
    from gdx_dispatch.services.pricing_engine import (
        PricingConfigError,
        hydrate_settings_from_db,
        price_line,
    )

    try:
        settings = hydrate_settings_from_db(db)
        return price_line(
            cost=Decimal(str(cost)),
            pricing_category=pricing_category,
            customer=_resolve_customer_for_engine(estimate, db),
            settings=settings,
            line_margin_override=Decimal(str(margin_override)) if margin_override is not None else None,
        )
    except PricingConfigError as e:
        log.warning("estimate_engine_price_config_error: %s", e)
        raise HTTPException(status_code=409, detail=f"Pricing config error: {e}") from e


class EstimateLineCreateNested(BaseModel):
    """Same shape as EstimateLineCreateIn but tolerant of the frontend's
    estimate-create payload — the form sends category/quantity/unit_price
    + optional cost/pricing_category, and may include extra ignorable keys."""
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(default=0, ge=0, le=999999.99)
    category: str | None = Field(default=None, max_length=80)
    cost: float | None = Field(default=None, ge=0, le=999999.99)
    pricing_category: str | None = Field(default=None, max_length=40)
    # S97 slice 5 — labor matrix link.
    labor_price_item_id: UUID | None = None
    estimated_man_hours: float | None = Field(default=None, ge=0, le=999.99)
    # PLUGIN INTEGRATION POINT (ADR-013) — DO NOT REMOVE. Full captured source
    # spec a plugin (e.g. CHI pricing) attaches to this line; persisted on the
    # line so it survives estimate→Job and is readable downstream. See
    # EstimateLine.line_metadata.
    line_metadata: dict | None = None


class EstimateCreateIn(BaseModel):
    job_id: UUID | None = None
    customer_id: UUID | None = None
    label: str | None = None
    jobsite_address: str | None = None
    notes: str | None = None
    # Per-estimate overrides — null = use tenant-wide tax rate from
    # /api/tax/config; tax_rate is a decimal (0.0825 = 8.25%).
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    discount: float | None = Field(default=None, ge=0, le=999999.99)
    # Frontend submits the full line-items array on /estimates/new. Pre-fix
    # this field was missing from the schema; Pydantic silently dropped the
    # array and the estimate was persisted with zero lines / total $0.00 —
    # the root cause behind EST-000014/015 = $0.00 totals on prod GDX.
    # Accept the array and create EstimateLine rows in-band.
    line_items: list[EstimateLineCreateNested] = Field(default_factory=list)
    description: str | None = None
    valid_until: str | None = None
    # "Total-only" override at create time. None = inherit tenant default.
    hide_line_prices: bool | None = None


class EstimatePatchIn(BaseModel):
    label: str | None = None
    jobsite_address: str | None = None
    description: str | None = None
    notes: str | None = None
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    discount: float | None = Field(default=None, ge=0, le=999999.99)
    job_id: UUID | None = None
    customer_id: UUID | None = None
    # Tri-state via exclude_unset: field omitted = untouched; explicit null =
    # revert to inherit tenant default; true/false = force hide/show.
    hide_line_prices: bool | None = None


class EstimateLineCreateIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(default=0, ge=0, le=999999.99)
    category: str | None = Field(default=None, max_length=80)
    # Sprint 1.0.5 — engine-driven pricing. If `cost` and `pricing_category`
    # are provided, the pricing engine computes `unit_price` (sell) and the
    # line snapshots cost + resolved margin. Manual `unit_price` still works
    # for ad-hoc line items not tied to a catalog cost (back-compat).
    cost: float | None = Field(default=None, ge=0, le=999999.99)
    pricing_category: str | None = Field(default=None, max_length=40)
    margin_pct_override: float | None = Field(default=None, ge=0, lt=1)
    # S97 slice 5 — labor matrix link snapshotted onto the line at create.
    labor_price_item_id: UUID | None = None
    estimated_man_hours: float | None = Field(default=None, ge=0, le=999.99)
    # PLUGIN INTEGRATION POINT (ADR-013) — DO NOT REMOVE. Full captured source
    # spec a plugin (e.g. CHI pricing) attaches to this line; persisted on the
    # line so it survives estimate→Job and is readable downstream. See
    # EstimateLine.line_metadata.
    line_metadata: dict | None = None

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("description cannot be blank")
        return trimmed


class EstimateLinePatchIn(BaseModel):
    description: str | None = None
    category: str | None = Field(default=None, max_length=80)
    quantity: int | None = Field(default=None, gt=0, le=9999)
    unit_price: float | None = Field(default=None, ge=0, le=999999.99)
    # Sprint 1.0.5 — re-resolve sell from snapshotted margin when cost edits.
    # `margin_pct_override` lets operators bump a single line's margin without
    # touching the underlying tier. `pricing_category` is intentionally
    # IMMUTABLE post-create — changing it would invalidate the snapshot.
    cost: float | None = Field(default=None, ge=0, le=999999.99)
    margin_pct_override: float | None = Field(default=None, ge=0, lt=1)
    # Sentinel to clear an override; set this true to set margin_pct_override
    # back to NULL (Pydantic can't distinguish "set to None" from "not set").
    clear_margin_override: bool = False
    # Reorder support — line position in the estimate. Persisted via the generic
    # setattr path below; read-back sorts by (sort_order, created_at, id).
    sort_order: int | None = Field(default=None, ge=0, le=99999)

    @field_validator("description")
    @classmethod
    def _description_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("description cannot be blank")
        return trimmed


class DeclineIn(BaseModel):
    reason: str | None = None


@router.get("/pipeline-summary", response_model=None)
def estimates_pipeline_summary(
    _: None = Depends(require_role("owner", "admin", "dispatcher", "sales", "accounting", "manager")),
    db: Session = Depends(get_db),
) -> dict:
    """Aggregate count + sell + profit across all non-converted estimates.

    "Non-converted" = job_id IS NULL, deleted_at IS NULL, status IN
    ('draft','sent','accepted'). Cost/profit math mirrors
    EstimateProfitPanel.vue exactly: only engine-priced lines (both
    cost_snapshot and margin_pct_snapshot non-null) contribute to cost,
    and the matching unit_price * quantity contributes to sell. Manually
    priced lines are excluded from both sides — same as the per-estimate
    panel — and we surface a count so the dashboard can warn when the
    blended margin understates because a chunk of the pipeline is manual.
    """
    # S-autosave slice 4: exclude estimates with zero lines from the pipeline.
    # With server-side draft autosave, opening /estimates/new and picking a
    # customer will create a draft row that has not yet had any lines added —
    # those should not pollute the pipeline KPI until the user has expressed
    # real intent (≥1 line). Applies to all statuses for consistency: a
    # zero-line "sent" estimate is also nonsense.
    has_lines = select(EstimateLine.id).where(EstimateLine.estimate_id == Estimate.id).exists()
    estimates = db.execute(
        select(Estimate)
        .where(
            Estimate.deleted_at.is_(None),
            Estimate.job_id.is_(None),
            Estimate.status.in_(("draft", "sent", "accepted")),
            has_lines,
        )
        .options(selectinload(Estimate.lines))
    ).scalars().all()

    count = len(estimates)
    total_cost = Decimal("0")
    total_sell = Decimal("0")
    estimates_with_manual_lines = 0
    for est in estimates:
        has_manual = False
        for line in est.lines:
            if line.cost_snapshot is None or line.margin_pct_snapshot is None:
                has_manual = True
                continue
            qty = Decimal(line.quantity or 0)
            total_cost += (line.cost_snapshot or Decimal(0)) * qty
            total_sell += (line.unit_price or Decimal(0)) * qty
        if has_manual:
            estimates_with_manual_lines += 1

    net_profit = total_sell - total_cost
    blended_margin = float(net_profit / total_sell) if total_sell > 0 else 0.0
    return {
        "count": count,
        "total_sell": float(total_sell),
        "total_cost": float(total_cost),
        "net_profit": float(net_profit),
        "blended_margin": blended_margin,
        "estimates_with_manual_lines": estimates_with_manual_lines,
    }


@router.get("", response_model=None)
def list_estimates(
    job_id: UUID | None = Query(default=None),
    customer_id: UUID | None = Query(default=None),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    q = select(Estimate).where(Estimate.deleted_at.is_(None))
    if job_id:
        q = q.where(Estimate.job_id == job_id)
    if customer_id:
        # Pre-fix this param was silently dropped, so the customer-detail
        # Estimates tab rendered every estimate in the tenant. Mirror the
        # invoices.py shape (Phase D / D-71): match either the direct FK or
        # via Job.customer_id, since legacy QB-imported estimates may have
        # NULL Estimate.customer_id with the linkage on the parent Job.
        from sqlalchemy import or_ as _or
        q = q.where(
            _or(
                Estimate.customer_id == customer_id,
                Estimate.job_id.in_(select(Job.id).where(Job.customer_id == customer_id)),
            )
        )
    rows = db.execute(q.order_by(Estimate.created_at.desc(), Estimate.id.desc())).scalars().all()
    items = [_serialize_estimate(row, include_lines=False) for row in rows]

    # MH-6 (mobile UX audit P1 #8, 2026-05-19): pre-fix every estimate
    # card on /mobile/estimates rendered "—" for customer because the
    # serializer carried `customer_id` but no `customer_name`, and the
    # view fell through `e.customer_name || e.customer?.name || '—'`.
    # Enrich here using the same Estimate.customer_id-first / Job.
    # customer_id-fallback pattern invoices.py:466-489 already uses for
    # QB-imported records with NULL Estimate.customer_id. Graceful
    # degradation if the enrichment query fails — customer names just
    # stay empty rather than 5xx-ing the whole list.
    try:
        # MH-6 audit (round 1): pre-fix the if/elif partition routed an
        # Estimate to the customer-id query OR the job-id query but
        # never both. An Estimate with a stale customer_id (customer
        # soft-deleted / orphaned) but a healthy Job linkage would not
        # fall back — same "—" the audit caught, just from a different
        # cause. Fix: build BOTH lookup maps for EVERY row, then choose
        # the first that produces a name.
        cust_ids: set = set()
        job_ids: set = set()
        for row in rows:
            if row.customer_id is not None:
                cust_ids.add(row.customer_id)
            if row.job_id is not None:
                job_ids.add(row.job_id)
        # Customers reachable via direct Estimate.customer_id.
        name_by_cust: dict = {}
        if cust_ids:
            for cid, name in db.execute(
                select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))
            ).all():
                if name:
                    name_by_cust[str(cid)] = name
        # Customers reachable via Job.customer_id (catches BOTH the QB-
        # null case where Estimate.customer_id was never set AND the
        # stale-customer case where the FK points to a soft-deleted row).
        name_by_job: dict = {}
        if job_ids:
            for jid, name in db.execute(
                select(Job.id, Customer.name)
                .select_from(Job)
                .join(Customer, Customer.id == Job.customer_id)
                .where(Job.id.in_(job_ids))
            ).all():
                if name:
                    name_by_job[str(jid)] = name
        for item in items:
            cid = item.get("customer_id")
            if cid and name_by_cust.get(str(cid)):
                item["customer_name"] = name_by_cust[str(cid)]
                continue
            jid = item.get("job_id")
            if jid and name_by_job.get(str(jid)):
                item["customer_name"] = name_by_job[str(jid)]
                continue
            # Last resort — leave the key absent rather than echoing
            # an empty string; the view's `|| '—'` fallback handles it.
    except Exception:
        # Audit round-1 critique: don't swallow silently — log the row
        # count and the tenant id so a degraded-mode list (every card
        # showing "—") leaves a breadcrumb in the server log.
        import logging
        # No `request` param on this handler, so we can't include the
        # tenant id here. Row count is the next-best breadcrumb — a
        # log of "rows=0" tells you the enrich block ran but the list
        # was empty; "rows=N" with the exception trace tells you N
        # cards rendered as "—" downstream.
        logging.getLogger(__name__).exception(
            "list_estimates customer_name enrich failed: rows=%d",
            len(rows),
        )

    return items


@router.post("", response_model=None, status_code=201)
def create_estimate(
    payload: EstimateCreateIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if not payload.job_id and not payload.customer_id:
        raise HTTPException(status_code=400, detail="job_id or customer_id is required")

    customer_id = payload.customer_id
    if payload.job_id:
        job = db.execute(select(Job).where(Job.id == payload.job_id, Job.deleted_at.is_(None))).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        customer_id = customer_id or job.customer_id

    if customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not customer:
            raise HTTPException(status_code=404, detail="customer not found")

    # Tenant binding — previously relied on company_id being nullable. Now
    # that the model enforces NOT NULL we must pull tenant from the request.
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")

    estimate = Estimate(
        job_id=payload.job_id,
        customer_id=customer_id,
        estimate_number=_next_estimate_number(db),
        label=payload.label.strip() if payload.label else None,
        jobsite_address=payload.jobsite_address.strip() if payload.jobsite_address else None,
        description=payload.description.strip() if payload.description else None,
        notes=payload.notes.strip() if payload.notes else None,
        tax_rate=Decimal(str(payload.tax_rate)) if payload.tax_rate is not None else None,
        discount=Decimal(str(payload.discount)) if payload.discount is not None else None,
        hide_line_prices=payload.hide_line_prices,
        status="draft",
        total=Decimal("0.00"),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=tenant_id,
    )
    db.add(estimate)
    db.flush()

    # Persist nested line_items if the client sent them. The Estimate.total is
    # the sum of (quantity * unit_price) across lines (subtotal — tax/discount
    # live on the surrounding context, not the persisted total). Without this
    # block /estimates/new produced rows with zero lines and $0.00 totals.
    running_total = Decimal("0.00")
    for sort_order, item in enumerate(payload.line_items, start=1):
        qty = item.quantity
        # Labor-matrix lines: matrix row is authoritative for unit_price.
        # Client-supplied cost / unit_price / pricing_category are ignored —
        # flat_price wins. EST-000030 (2026-05-07) shipped at $91k vs $1.4k
        # because the engine path computed unit_price from a fat-fingered
        # cost; this branch removes that path for labor lines entirely.
        if item.labor_price_item_id is not None:
            row = _resolve_labor_matrix_row(db, item.labor_price_item_id)
            unit, line_total, cost_snapshot, margin_pct_snapshot, pricing_source = (
                _labor_line_pricing(db, matrix_row=row, quantity=qty)
            )
            estimated_man_hours_val = Decimal(str(row.assumed_man_hours or 0))
        else:
            estimated_man_hours_val = (
                Decimal(str(item.estimated_man_hours))
                if item.estimated_man_hours is not None else None
            )
            if item.cost is not None and item.pricing_category:
                # Engine path — catalog/imported items with cost + pricing bucket
                # get the tier markup, same as the add-line-to-existing path. This
                # is what makes "add from catalog" on a NEW estimate mark up
                # instead of posting at cost (zero margin).
                result = _engine_price_line(
                    db, estimate, cost=item.cost,
                    pricing_category=item.pricing_category, margin_override=None,
                )
                unit = Decimal(str(result.sell))
                cost_snapshot = Decimal(str(result.cost))
                margin_pct_snapshot = result.margin_pct
                pricing_source = result.source
            else:
                unit = Decimal(str(item.unit_price or 0))
                # Default snapshot = whatever cost the client sent (None for free-form).
                cost_snapshot = Decimal(str(item.cost)) if item.cost is not None else None
                margin_pct_snapshot = None
                pricing_source = None
                # Derive margin_pct_snapshot whenever cost + unit_price are both present
                # (CHI / typed-catalog doors etc. that don't go through the engine path).
                # Without this, the line is born with NULL margin and the PATCH lock-out
                # rule classifies it "manually-priced" forever (prod incident 2026-05-07).
                if cost_snapshot is not None:
                    derived = _derive_margin_pct(cost_snapshot, unit)
                    if derived is not None:
                        margin_pct_snapshot = derived
                        pricing_source = "client_cost"
            line_total = (Decimal(qty) * unit).quantize(Decimal("0.01"))
        db.add(EstimateLine(
            estimate_id=estimate.id,
            description=item.description.strip(),
            category=(item.category.strip() if item.category else None),
            quantity=qty,
            unit_price=unit,
            line_total=line_total,
            sort_order=sort_order,
            cost_snapshot=cost_snapshot,
            margin_pct_snapshot=margin_pct_snapshot,
            pricing_source=pricing_source,
            labor_price_item_id=item.labor_price_item_id,
            estimated_man_hours=estimated_man_hours_val,
            company_id=tenant_id,
            # Plugin integration (ADR-013) — captured source spec, if the line came
            # from a plugin (e.g. CHI pricing). See EstimateLine.line_metadata.
            line_metadata=item.line_metadata,
        ))
        running_total += line_total
    estimate.total = running_total
    db.commit()
    db.refresh(estimate)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_created",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={
            "estimate_number": estimate.estimate_number,
            "status": estimate.status,
            "line_count": len(payload.line_items),
            "total": float(running_total),
        },
    )
    db.commit()
    return _serialize_estimate(estimate, include_lines=True)


@router.get("/{estimate_id}", response_model=None)
def get_estimate(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    return _serialize_estimate(estimate, include_lines=True)


@router.patch("/{estimate_id}", response_model=None)
def patch_estimate(
    estimate_id: UUID,
    payload: EstimatePatchIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    _ensure_editable(estimate)

    updates = payload.model_dump(exclude_unset=True)
    if "job_id" in updates and updates["job_id"]:
        job = db.execute(select(Job).where(Job.id == updates["job_id"], Job.deleted_at.is_(None))).scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="job not found")

    if "customer_id" in updates and updates["customer_id"]:
        customer = db.execute(
            select(Customer).where(Customer.id == updates["customer_id"], Customer.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not customer:
            raise HTTPException(status_code=404, detail="customer not found")

    for key, value in updates.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(estimate, key, value)

    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_estimate",
                entity_type="estimate",
                entity_id=str(estimate_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_estimate_audit_failed')
    return _serialize_estimate(estimate, include_lines=True)


@router.delete("/{estimate_id}", response_model=None)
def delete_estimate(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    estimate = _get_estimate_or_404(estimate_id, db)
    estimate.deleted_at = utcnow()
    estimate.updated_at = utcnow()
    db.commit()
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_estimate",
                entity_type="estimate",
                entity_id=str(estimate_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_estimate_audit_failed')
    return {"deleted": True}


@router.post("/{estimate_id}/lines", response_model=None, status_code=201)
def add_line(
    estimate_id: UUID,
    payload: EstimateLineCreateIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    _ensure_editable(estimate)

    if payload.margin_pct_override is not None:
        _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
        if _tid:
            require_line_margin_override_allowed(_tid)

    sort_order = (max((line.sort_order for line in estimate.lines), default=0) or 0) + 1

    # Sprint 1.0.5 — engine path. If cost + pricing_category provided, resolve
    # sell via the engine and snapshot the result. Otherwise fall back to the
    # legacy manual unit_price path (back-compat for ad-hoc lines).
    cost_snapshot: Decimal | None = None
    margin_pct_snapshot: Decimal | None = None
    margin_pct_override: Decimal | None = None
    pricing_source: str | None = None

    # Labor-matrix lines: matrix row IS the price. Engine path is forbidden
    # for labor — see _labor_line_pricing docstring + EST-000030 retro.
    # This branch must come BEFORE the engine path to lock out the cascade
    # bug where (cost, pricing_category="labor") would otherwise route here.
    if payload.labor_price_item_id is not None:
        row = _resolve_labor_matrix_row(db, payload.labor_price_item_id)
        unit_price, _line_total_unused, cost_snapshot, margin_pct_snapshot, pricing_source = (
            _labor_line_pricing(db, matrix_row=row, quantity=payload.quantity)
        )
        # Authoritative hours come from the matrix row — same reason as price.
        payload_hours_override = Decimal(str(row.assumed_man_hours or 0))
    elif payload.cost is not None and payload.pricing_category:
        result = _engine_price_line(
            db, estimate,
            cost=payload.cost,
            pricing_category=payload.pricing_category,
            margin_override=payload.margin_pct_override,
        )
        unit_price = _money(float(result.sell))
        cost_snapshot = _money(float(result.cost))
        margin_pct_snapshot = result.margin_pct  # already Decimal
        if payload.margin_pct_override is not None:
            margin_pct_override = Decimal(str(payload.margin_pct_override))
        pricing_source = result.source
        payload_hours_override = None
    else:
        unit_price = _money(payload.unit_price)
        payload_hours_override = None
        # Cost without a pricing_category (typed-catalog / CHI fallback path).
        # Snapshot the cost and derive margin so future PATCHes can edit it
        # via the engine instead of getting locked out as "manually-priced".
        if payload.cost is not None:
            cost_snapshot = _money(float(payload.cost))
            derived = _derive_margin_pct(cost_snapshot, unit_price)
            if derived is not None:
                margin_pct_snapshot = derived
                pricing_source = "client_cost"

    line_total = _money(payload.quantity * float(unit_price))
    if payload_hours_override is not None:
        estimated_man_hours_val = payload_hours_override
    elif payload.estimated_man_hours is not None:
        estimated_man_hours_val = Decimal(str(payload.estimated_man_hours))
    else:
        estimated_man_hours_val = None
    line = EstimateLine(
        estimate=estimate,
        company_id=estimate.company_id,
        description=payload.description,
        category=(payload.category.strip() if payload.category else None),
        quantity=payload.quantity,
        unit_price=unit_price,
        line_total=line_total,
        sort_order=sort_order,
        cost_snapshot=cost_snapshot,
        margin_pct_snapshot=margin_pct_snapshot,
        margin_pct_override=margin_pct_override,
        pricing_source=pricing_source,
        labor_price_item_id=payload.labor_price_item_id,
        estimated_man_hours=estimated_man_hours_val,
        # Plugin integration (ADR-013) — carries the captured source spec, if any.
        line_metadata=payload.line_metadata,
    )
    db.add(line)
    db.flush()
    _recalculate_total(estimate, db)
    db.commit()
    db.refresh(line)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="add_line",
                entity_type="estimate_line",
                entity_id=str(estimate_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('add_line_audit_failed')
    return _serialize_line(line)


@router.patch("/{estimate_id}/lines/{line_id}", response_model=None)
def patch_line(
    estimate_id: UUID,
    line_id: UUID,
    payload: EstimateLinePatchIn,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    _ensure_editable(estimate)

    if payload.margin_pct_override is not None:
        _tid = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
        if _tid:
            require_line_margin_override_allowed(_tid)

    line = db.execute(
        select(EstimateLine).where(EstimateLine.id == line_id, EstimateLine.estimate_id == estimate.id)
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail="line not found")

    updates = payload.model_dump(exclude_unset=True)

    # Sprint 1.0.5 — handle engine-managed fields separately so we don't blast
    # them through setattr along with description/quantity. Snapshot is frozen
    # at create; patch can only change cost (re-derives sell from frozen
    # margin_pct_snapshot) or margin_pct_override (re-derives sell from cost).
    new_cost = updates.pop("cost", None)
    new_override = updates.pop("margin_pct_override", None)
    clear_override = updates.pop("clear_margin_override", False)

    for key, value in updates.items():
        if isinstance(value, str):
            value = value.strip() or None
        setattr(line, key, value)

    # Engine-managed recomputation. Order: cost change → override change →
    # nothing (legacy manual path falls through to qty × unit_price).
    is_engine_line = line.margin_pct_snapshot is not None
    wants_engine_fields = new_cost is not None or new_override is not None or clear_override
    if wants_engine_fields and not is_engine_line:
        # Heal pre-existing lines born with cost_snapshot but NULL margin
        # (typed-catalog / CHI doors created before 2026-05-07). Back-derive
        # margin from current cost + unit_price and proceed. Genuine free-form
        # lines (no cost snapshot, no usable unit_price) still 409.
        healed = _derive_margin_pct(line.cost_snapshot, line.unit_price)
        if healed is not None:
            line.margin_pct_snapshot = healed
            line.pricing_source = line.pricing_source or "client_cost"
            is_engine_line = True
        else:
            raise HTTPException(
                status_code=409,
                detail="cannot apply engine fields to a manually-priced line; recreate the line via cost+pricing_category",
            )
    if clear_override:
        line.margin_pct_override = None
    if new_override is not None:
        line.margin_pct_override = Decimal(str(new_override))
    if new_cost is not None:
        line.cost_snapshot = _money(new_cost)
    if is_engine_line and (new_cost is not None or new_override is not None or clear_override):
        # Re-derive sell from frozen margin_pct_snapshot (or override if set).
        # Per decision A: admin tier edits never silently re-price old lines.
        from gdx_dispatch.services.pricing_engine import sell_from_cost

        effective_margin = line.margin_pct_override or line.margin_pct_snapshot
        new_sell = sell_from_cost(
            Decimal(str(line.cost_snapshot)),
            Decimal(str(effective_margin)),
        )
        line.unit_price = _money(float(new_sell))
        line.pricing_source = (
            "line_override" if line.margin_pct_override is not None else line.pricing_source
        )

    line.line_total = _money((line.quantity or 0) * _to_float(line.unit_price))
    db.flush()
    _recalculate_total(estimate, db)
    db.commit()
    db.refresh(line)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="patch_line",
                entity_type="estimate_line",
                entity_id=str(line_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('patch_line_audit_failed')
    return _serialize_line(line)


@router.delete("/{estimate_id}/lines/{line_id}", response_model=None)
def delete_line(
    estimate_id: UUID,
    line_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    _ensure_editable(estimate)

    line = db.execute(
        select(EstimateLine).where(EstimateLine.id == line_id, EstimateLine.estimate_id == estimate.id)
    ).scalar_one_or_none()
    if not line:
        raise HTTPException(status_code=404, detail="line not found")

    db.delete(line)
    db.flush()
    _recalculate_total(estimate, db)
    db.commit()
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_line",
                entity_type="estimate_line",
                entity_id=str(line_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_line_audit_failed')
    return {"deleted": True}


_DEFAULT_SUBJECT_TEMPLATE = "{{job_title}}"
_DEFAULT_BODY_TEMPLATE = (
    "Hi {{customer_name}},\n\n"
    "Please see the attached estimate for {{job_title}}.\n\n"
    "Reply to this email with any questions, or to move forward.\n\n"
    "Thanks,\n{{company_name}}"
)


def _render_template(tpl: str, ctx: dict[str, str]) -> str:
    """Lightweight {{placeholder}} substitution — no logic, no escapes."""
    out = tpl
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
        out = out.replace("{{ " + k + " }}", v)
    return out


@router.get("/{estimate_id}/email-compose", response_model=None)
def estimate_email_compose(
    estimate_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Return a prebuilt compose payload for the in-app composer:
    {to, subject, body_text, pdf, extra_attachments}.
    Subject/body come from per-tenant templates configurable in
    Settings → Feature Settings."""
    import base64 as _b64
    from gdx_dispatch.core.pdf_generator import generate_estimate_pdf
    from gdx_dispatch.routers.pdf import _branding_payload, _estimate_attachments_for_pdf, _estimate_payload

    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    customer = None
    if estimate.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == estimate.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()

    job_title = ""
    if estimate.job_id:
        job = db.execute(
            select(Job).where(Job.id == estimate.job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if job:
            job_title = (job.title or "").strip()

    company_name = "Your Service Company"
    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
    if settings_obj and settings_obj.company_name:
        company_name = settings_obj.company_name

    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")

    # Per-tenant templates (Settings → Feature Settings). Empty string falls
    # back to the platform defaults so a fresh tenant still gets a sane email.
    subject_tpl = ""
    body_tpl = ""
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        if tenant_id:
            f = get_features(tenant_id)
            subject_tpl = (f.email_subject_template or "").strip()
            body_tpl = (f.email_body_template or "").strip()
    except Exception:
        log.exception("email_compose_read_features_failed")
    if not subject_tpl:
        subject_tpl = _DEFAULT_SUBJECT_TEMPLATE
    if not body_tpl:
        body_tpl = _DEFAULT_BODY_TEMPLATE

    # Show tax-inclusive total in the email so it matches the attached PDF.
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
    _ctx_totals = compute_estimate_totals(estimate, db)
    label_or_job = job_title or (estimate.label or "").strip() or f"Estimate {estimate.estimate_number or ''}".strip()
    ctx = {
        "customer_name": (customer.name if customer else "") or "there",
        "job_title": label_or_job,
        "estimate_number": estimate.estimate_number or "",
        "estimate_label": (estimate.label or "").strip(),
        "company_name": company_name,
        "total": f"${_ctx_totals['total']:.2f}",
    }
    subject = _render_template(subject_tpl, ctx).strip() or label_or_job
    body_text = _render_template(body_tpl, ctx)
    images, files = _estimate_attachments_for_pdf(db, estimate.id, tenant_id)
    default_terms = ""
    deposit_pct = 0
    hide_line_prices_default = False
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        if tenant_id:
            features = get_features(tenant_id)
            default_terms = features.default_terms
            deposit_pct = features.deposit_pct
            hide_line_prices_default = features.hide_line_prices
    except Exception:
        default_terms = ""
        deposit_pct = 0
        hide_line_prices_default = False
    pdf_bytes = generate_estimate_pdf(
        estimate_data=_estimate_payload(
            estimate, customer, default_terms=default_terms,
            attachment_images=images, attachment_files=files,
            deposit_pct=deposit_pct, hide_line_prices_default=hide_line_prices_default, db=db,
        ),
        tenant_branding=_branding_payload(db),
    )
    pdf_b64 = _b64.b64encode(pdf_bytes).decode("ascii")
    pdf_name = f"estimate-{estimate.estimate_number or str(estimate.id)[:8]}.pdf"

    extra = []
    rows = db.execute(
        select(Document)
        .where(Document.estimate_id == estimate_id, Document.deleted_at.is_(None))
        .order_by(Document.uploaded_at.asc())
    ).scalars().all()
    for d in rows:
        extra.append({
            "id": str(d.id),
            "name": d.original_name,
            "content_type": d.content_type or "application/octet-stream",
            "file_size": int(d.file_size or 0),
        })

    return {
        "to": [customer.email] if (customer and customer.email) else [],
        "customer_id": str(customer.id) if customer else None,
        "subject": subject,
        "body_text": body_text,
        "pdf": {
            "name": pdf_name,
            "content_type": "application/pdf",
            "content_base64": pdf_b64,
            "size_bytes": len(pdf_bytes),
        },
        "extra_attachments": extra,
    }


@router.post("/{estimate_id}/mark-sent", response_model=None)
def mark_estimate_sent(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Flip status to 'sent' without firing a server-side email. Used when
    the operator composes the email manually in their own mail client."""
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status in {"accepted", "declined"}:
        raise HTTPException(status_code=409, detail="estimate is finalized")
    estimate.status = "sent"
    estimate.sent_at = utcnow()
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_marked_sent",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"status": estimate.status, "channel": "manual"},
    )
    db.commit()
    return _serialize_estimate(estimate, include_lines=False)


@router.post("/{estimate_id}/send", response_model=None)
def send_estimate(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status in {"accepted", "declined"}:
        raise HTTPException(status_code=409, detail="estimate is finalized")
    estimate.status = "sent"
    estimate.sent_at = utcnow()
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_sent",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"status": estimate.status},
    )
    db.commit()

    # Send the estimate email to the customer. Routes through the
    # unified transactional-email helper so an Outlook-connected user
    # actually delivers via Graph; falls back to SMTP via email_settings.
    email_sent = False
    email_provider: str | None = None
    email_skip_reason: str | None = None
    try:
        from gdx_dispatch.core.email_sender import build_estimate_email_html
        from gdx_dispatch.core.transactional_email import send_transactional_email
        tid = str(estimate.company_id) if estimate.company_id else None
        if tid and estimate.customer_id:
            cust = db.execute(
                select(Customer).where(
                    Customer.id == estimate.customer_id,
                    Customer.deleted_at.is_(None),
                )
            ).scalar_one_or_none()
            if cust and cust.email:
                # Get line items via ORM
                lines_data = []
                try:
                    lines = db.execute(
                        select(EstimateLine)
                        .where(EstimateLine.estimate_id == estimate.id)
                        .order_by(EstimateLine.sort_order)
                    ).scalars().all()
                    lines_data = [
                        {
                            "description": ln.description,
                            "quantity": ln.quantity,
                            "unit_price": _to_float(ln.unit_price),
                            "line_total": _to_float(ln.line_total),
                        }
                        for ln in lines
                    ]
                except Exception:
                    log.exception("send_estimate caught exception")
                    pass
                # Get company name via ORM
                company_name = "Your Service Company"
                try:
                    settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
                    if settings_obj and settings_obj.company_name:
                        company_name = settings_obj.company_name
                except Exception:
                    log.exception("send_estimate caught exception")
                    pass
                # Tax-inclusive total — matches the attached PDF.
                from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
                _email_totals = compute_estimate_totals(estimate, db)
                html = build_estimate_email_html(
                    company_name=company_name,
                    estimate_number=estimate.estimate_number or str(estimate.id)[:8],
                    customer_name=cust.name or "Valued Customer",
                    line_items=lines_data,
                    total=_email_totals["total"],
                    notes=estimate.notes or "",
                    description=estimate.description or "",
                )
                email_sent, email_provider, email_skip_reason = send_transactional_email(
                    tenant_db=db,
                    tenant_id=tid,
                    user_id=str(_actor_id(_)),
                    to_email=cust.email,
                    to_name=cust.name or "",
                    subject=f"Estimate #{estimate.estimate_number} from {company_name}",
                    html_body=html,
                )
            elif cust:
                email_skip_reason = "customer_has_no_email"
            else:
                email_skip_reason = "customer_not_found"
        elif not estimate.customer_id:
            email_skip_reason = "estimate_has_no_customer"
    except Exception:
        log.exception("estimate_email_send_failed")
        email_skip_reason = "exception"

    payload = _serialize_estimate(estimate, include_lines=False)
    payload["email_sent"] = email_sent
    if email_provider:
        payload["email_provider"] = email_provider
    if email_skip_reason:
        payload["email_skip_reason"] = email_skip_reason
    return payload


def _holding_area_id_by_name(db: Session, name: str) -> str | None:
    """Resolve a tenant holding-area row by name. Returns None if missing.

    Used by the accept/convert flow to land an accepted estimate's new Job
    in the "Order Doors" lane automatically (2026-05-13 directive). Missing
    area is logged and the job is created without holding_area_id rather
    than failing the customer-facing accept — the dispatcher can re-route.
    """
    try:
        row = db.execute(
            _text("SELECT id FROM holding_areas WHERE name = :n LIMIT 1"),
            {"n": name},
        ).first()
        return str(row[0]) if row else None
    except Exception:
        logging.getLogger(__name__).exception("holding_area_lookup_failed name=%s", name)
        return None


def _copy_estimate_lines_to_job(estimate, new_job, db: Session) -> int:
    """Copy each estimate line onto the job as a parts-needed row (#56).

    Receiving, the field tech, and invoicing read job_parts_needed, so the
    agreed parts/labor must land there — not just on the estimate. The full
    captured spec stays on the linked estimate line; a readable summary
    (category, unit price, scalar line_metadata) rides along in notes.
    ponytail: job_parts_needed has no JSON column — add one if the captured
    spec ever needs to be queryable on the job side.
    """
    lines = db.execute(
        select(EstimateLine)
        .where(EstimateLine.estimate_id == estimate.id)
        .order_by(EstimateLine.sort_order)
    ).scalars().all()
    now = utcnow()
    copied = 0
    for line in lines:
        md = line.line_metadata if isinstance(line.line_metadata, dict) else {}
        note_bits: list[str] = []
        if line.category:
            note_bits.append(str(line.category))
        if line.unit_price:
            note_bits.append(f"${_to_float(line.unit_price):.2f} ea")
        spec = "; ".join(
            f"{k}={v}" for k, v in md.items() if not isinstance(v, (dict, list))
        )
        if spec:
            note_bits.append(spec)
        db.add(JobPartNeeded(
            id=str(uuid4()),
            company_id=str(new_job.company_id or estimate.company_id or ""),
            job_id=str(new_job.id),
            part_name=(line.description or "Item")[:200],
            quantity=int(line.quantity or 1),
            supplier=(str(md.get("vendor") or md.get("supplier") or "")[:200] or None),
            sku=(str(md.get("sku") or "")[:64] or None),
            status="needed",
            notes=(" • ".join(note_bits) or None),
            created_at=now,
            updated_at=now,
        ))
        copied += 1
    return copied


def _create_job_from_estimate(estimate, db: Session, actor: str) -> object:
    """Create a Job linked to this estimate. Idempotent — caller guards.

    Lands the new job in the "Order Doors" holding area so the dispatcher
    sees a clear "doors pending arrival" queue. Audit-logs both sides.
    """
    new_job = Job(
        id=uuid4(),
        customer_id=estimate.customer_id,
        title=(estimate.label or f"Estimate {estimate.estimate_number}").strip()[:200],
        description=estimate.description or estimate.notes,
        lifecycle_stage="scheduled",
        dispatch_status="unassigned",
        billing_status="unbilled",
        # Sold estimates become installs (2026-05-13 directive). The dispatcher
        # can re-classify if a one-off case slips through (e.g., a sold
        # service-call quote), but the default is an install job because the
        # business case for an accepted quote is door / opener installation.
        job_type="Installation",
        priority="Normal",
        status="Scheduled",
        company_id=estimate.company_id or "",
        is_demo=False,
        created_at=utcnow(),
        updated_at=utcnow(),
        holding_area_id=_holding_area_id_by_name(db, "Order Doors"),
    )
    db.add(new_job)
    db.flush()

    copied_lines = _copy_estimate_lines_to_job(estimate, new_job, db)

    estimate.job_id = new_job.id
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(new_job)

    log_audit_event_sync(
        db=db, tenant_id=None, user_id=actor,
        action="estimate_converted_to_job",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"job_id": str(new_job.id), "estimate_number": estimate.estimate_number},
    )
    log_audit_event_sync(
        db=db, tenant_id=None, user_id=actor,
        action="job_created_from_estimate",
        entity_type="job",
        entity_id=str(new_job.id),
        details={"estimate_id": str(estimate.id), "title": new_job.title, "lines_copied": copied_lines},
    )
    db.commit()
    return new_job


@router.post("/{estimate_id}/accept", response_model=None)
def accept_estimate(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status == "accepted":
        raise HTTPException(status_code=409, detail="already accepted")
    if estimate.status == "declined":
        raise HTTPException(status_code=409, detail="cannot accept a declined estimate")
    estimate.status = "accepted"
    estimate.accepted_at = utcnow()
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    actor = _actor_id(_)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=actor,
        action="estimate_accepted",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"status": estimate.status},
    )
    db.commit()

    # 2026-05-13 directive: accept = job created. The dispatcher used to
    # have to click a separate "Convert to Job" button, which left accepted
    # estimates invisible to the dispatch board. Auto-create when we have
    # a customer; surface a warning detail in the response if we don't so
    # the UI knows it needs a follow-up nudge.
    auto_converted_job_id: str | None = None
    convert_skipped_reason: str | None = None
    if estimate.job_id is not None:
        convert_skipped_reason = "already_linked"
    elif estimate.customer_id is None:
        convert_skipped_reason = "no_customer"
    else:
        try:
            new_job = _create_job_from_estimate(estimate, db, actor)
            auto_converted_job_id = str(new_job.id)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "auto_convert_failed_on_accept estimate=%s", estimate.id
            )
            convert_skipped_reason = "convert_failed"
            # The estimate-accept transaction has already committed. Without
            # this audit hook the trail would show estimate_accepted with no
            # downstream job event — the trail must reflect what actually
            # happened so an operator can recover via /convert-to-job later.
            try:
                log_audit_event_sync(
                    db=db, tenant_id=None, user_id=actor,
                    action="estimate_auto_convert_failed",
                    entity_type="estimate",
                    entity_id=str(estimate.id),
                    details={"error": str(exc)[:500]},
                )
                db.commit()
            except Exception:
                logging.getLogger(__name__).exception("auto_convert_audit_failed")

    payload = _serialize_estimate(estimate, include_lines=False)
    if auto_converted_job_id:
        payload["auto_converted_job_id"] = auto_converted_job_id
    if convert_skipped_reason:
        payload["auto_convert_skipped"] = convert_skipped_reason
    return payload


@router.post("/{estimate_id}/decline", response_model=None)
def decline_estimate(
    estimate_id: UUID,
    payload: DeclineIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status == "declined":
        raise HTTPException(status_code=409, detail="already declined")
    if estimate.status == "accepted":
        raise HTTPException(status_code=409, detail="cannot decline an accepted estimate")
    estimate.status = "declined"
    estimate.declined_at = utcnow()
    estimate.declined_reason = payload.reason.strip() if payload.reason else None
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_declined",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"reason": estimate.declined_reason},
    )
    db.commit()
    return _serialize_estimate(estimate, include_lines=False)


# ---------------------------------------------------------------------------
# Convert estimate → job (closes EstimatesView + EstimateDetailView Vue gap)
# ---------------------------------------------------------------------------
# Creates a new Job linked to this estimate via estimate.job_id and returns
# both ids. Requires the estimate to be in 'accepted' status (or 'sent' if
# force=true) and not already linked to a job. Audit logged on both sides.


@router.post("/{estimate_id}/convert-to-job", response_model=None)
def convert_estimate_to_job(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Manual convert. Idempotent if already linked; useful as a recovery
    path when the auto-convert on accept skipped (no_customer) and the
    customer is now attached, or when force-converting a pre-2026-05-13
    accepted estimate that never had its job created.
    """
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=False)
    if estimate.job_id is not None:
        raise HTTPException(
            status_code=409,
            detail=f"estimate already converted to job {estimate.job_id}",
        )
    if estimate.status not in ("accepted",):
        raise HTTPException(
            status_code=409,
            detail=f"estimate must be in 'accepted' status to convert; current: {estimate.status}",
        )
    if estimate.customer_id is None:
        raise HTTPException(
            status_code=422,
            detail="estimate has no customer; cannot convert to job",
        )

    new_job = _create_job_from_estimate(estimate, db, _actor_id(_))
    db.refresh(estimate)
    return {
        "estimate_id": str(estimate.id),
        "job_id": str(new_job.id),
        "status": "converted",
        "job": {
            "id": str(new_job.id),
            "title": new_job.title,
            "customer_id": str(new_job.customer_id) if new_job.customer_id else None,
            "lifecycle_stage": new_job.lifecycle_stage,
            "company_id": new_job.company_id,
        },
    }


@router.post("/{estimate_id}/duplicate", response_model=None, status_code=201)
def duplicate_estimate(
    estimate_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Full clone of an estimate: same customer / job-name / jobsite / description /
    notes / tax / lines. Resets status=draft, mints a new estimate_number and
    public_token, clears sent/accepted/declined/signed state and job linkage.

    Edge cases (auditor 2026-05-27):
      - proposal_mode estimates clone their ProposalTier rows too; without
        this, the duplicate has `proposal_mode=true` and zero tiers, which
        renders an empty good/better/best picker on /mobile/quoting.
      - customer_id is re-validated against deleted_at (mirroring the create
        path); if the original customer was later soft-deleted/merged, the
        duplicate starts with customer_id=NULL so the user must re-pick.
    """
    from gdx_dispatch.modules.proposals.models import ProposalTier

    source = _get_estimate_or_404(estimate_id, db, include_lines=True)
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "tenant-test")

    # Re-validate customer — clone-from-old-estimate is the realistic trigger
    # for the source customer having been merged/soft-deleted since.
    cloned_customer_id = None
    if source.customer_id is not None:
        live = db.execute(
            select(Customer).where(Customer.id == source.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()
        if live is not None:
            cloned_customer_id = source.customer_id

    new_estimate = Estimate(
        job_id=None,  # duplicates start unattached; original Job keeps its estimate
        customer_id=cloned_customer_id,
        estimate_number=_next_estimate_number(db),
        label=source.label,
        jobsite_address=source.jobsite_address,
        description=source.description,
        notes=source.notes,
        tax_rate=source.tax_rate,
        discount=source.discount,
        proposal_mode=bool(source.proposal_mode),
        hide_line_prices=source.hide_line_prices,
        status="draft",
        total=Decimal("0.00"),
        public_token=secrets.token_urlsafe(48)[:64],
        company_id=tenant_id,
    )
    db.add(new_estimate)
    db.flush()

    running_total = Decimal("0.00")
    source_lines = sorted(source.lines, key=lambda ln: (ln.sort_order, ln.created_at, ln.id))
    for line in source_lines:
        line_total = Decimal(str(line.line_total or 0))
        db.add(EstimateLine(
            estimate_id=new_estimate.id,
            description=line.description,
            category=line.category,
            quantity=line.quantity,
            unit_price=line.unit_price,
            line_total=line_total,
            sort_order=line.sort_order,
            cost_snapshot=line.cost_snapshot,
            margin_pct_snapshot=line.margin_pct_snapshot,
            margin_pct_override=line.margin_pct_override,
            pricing_source=line.pricing_source,
            labor_price_item_id=line.labor_price_item_id,
            estimated_man_hours=line.estimated_man_hours,
            company_id=tenant_id,
        ))
        running_total += line_total
    new_estimate.total = running_total

    # Clone proposal tiers when the source was in proposal_mode. Skip the
    # accepted_tier_id — the duplicate starts unaccepted by design.
    tier_count = 0
    if source.proposal_mode:
        source_tiers = db.execute(
            select(ProposalTier).where(ProposalTier.estimate_id == source.id)
        ).scalars().all()
        for tier in source_tiers:
            db.add(ProposalTier(
                estimate_id=new_estimate.id,
                tier_name=tier.tier_name,
                description=tier.description,
                total_price=tier.total_price,
                includes_parts=tier.includes_parts,
                warranty_months=tier.warranty_months,
                stripe_payment_link=None,  # payment links are per-estimate; mint new on demand
                display_order=tier.display_order,
            ))
            tier_count += 1

    db.commit()
    db.refresh(new_estimate)

    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_duplicated",
        entity_type="estimate",
        entity_id=str(new_estimate.id),
        details={
            "source_estimate_id": str(source.id),
            "source_estimate_number": source.estimate_number,
            "new_estimate_number": new_estimate.estimate_number,
            "line_count": len(source_lines),
            "tier_count": tier_count,
            "customer_dropped": source.customer_id is not None and cloned_customer_id is None,
            "total": float(running_total),
        },
    )
    db.commit()
    return _serialize_estimate(new_estimate, include_lines=True)


# ---------------------------------------------------------------------------
# Estimate Conversion Rate Dashboard (#198)
# ---------------------------------------------------------------------------

@router.get("/analytics/conversion-rate")
def estimate_conversion_rate(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Conversion rate: sent vs accepted, by job type."""
    from collections import defaultdict

    estimates = db.query(Estimate).filter(Estimate.deleted_at.is_(None)).all()

    by_type: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "accepted": 0})
    total_sent = 0
    total_accepted = 0

    for est in estimates:
        status = (est.status or "").lower()
        if status == "draft":
            continue
        job_type = "Unknown"
        if est.job_id:
            job = db.get(Job, est.job_id)
            if job:
                job_type = job.job_type or "Unknown"

        by_type[job_type]["sent"] += 1
        total_sent += 1
        if status == "accepted":
            by_type[job_type]["accepted"] += 1
            total_accepted += 1

    overall_rate = round(total_accepted / max(total_sent, 1) * 100, 1)

    return {
        "overall": {"sent": total_sent, "accepted": total_accepted, "rate_pct": overall_rate},
        "by_job_type": {
            k: {**v, "rate_pct": round(v["accepted"] / max(v["sent"], 1) * 100, 1)}
            for k, v in sorted(by_type.items())
        },
    }


# ---------------------------------------------------------------------------
# Estimate Expiration (#199) — auto-mark expired after valid_until
# ---------------------------------------------------------------------------

@router.post("/expire-stale")
def expire_stale_estimates(
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
) -> dict:
    """Mark estimates as expired if past their valid_until date."""
    now = utcnow()
    stale = (
        db.query(Estimate)
        .filter(
            Estimate.status.in_(("sent", "draft")),
            Estimate.valid_until.isnot(None),
            Estimate.valid_until < now,
            Estimate.deleted_at.is_(None),
        )
        .all()
    )
    expired_ids = []
    for est in stale:
        est.status = "expired"
        est.updated_at = now
        expired_ids.append(str(est.id))
    if expired_ids:
        db.commit()

    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="expire_stale_estimates",
                entity_type="estimate",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('expire_stale_estimates_audit_failed')
    return {"expired_count": len(expired_ids), "estimate_ids": expired_ids}


# ---------------------------------------------------------------------------
# Attachments — pictures + files attached to an estimate.
# Stored on disk under UPLOAD_DIR/<tenant>/estimate/<estimate_id>/<file>; row
# persisted to the existing `documents` table with estimate_id FK.
# ---------------------------------------------------------------------------

ESTIMATE_ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024  # 25MB
ESTIMATE_ATTACHMENT_ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif", "image/gif",
    "application/pdf",
}


def _attachment_dir(tenant_id: str, estimate_id: str) -> Path:
    # Constrain to the upload root so a crafted tenant_id can't traverse out.
    # realpath + startswith is the form CodeQL recognizes as a barrier; the
    # trailing os.sep stops a sibling like "<root>-evil". (CodeQL path-injection)
    base = os.path.realpath(os.getenv("UPLOAD_DIR", "/app/uploads"))
    candidate = os.path.realpath(os.path.join(base, tenant_id, "estimate", estimate_id))
    if not candidate.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Invalid attachment path")
    return Path(candidate)


def _sanitize_attachment_name(name: str | None) -> str:
    import re
    candidate = os.path.basename((name or "").replace("\\", "/")).replace("\x00", "")
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", candidate).strip("._")
    if not candidate:
        candidate = f"file-{uuid4().hex}"
    return candidate[:120]


def _serialize_attachment(doc: Document) -> dict[str, object]:
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "original_name": doc.original_name,
        "content_type": doc.content_type,
        "file_size": int(doc.file_size or 0),
        "uploaded_by": doc.uploaded_by,
        "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None,
        "download_url": f"/api/estimates/{doc.estimate_id}/attachments/{doc.id}/download",
    }


@router.get("/{estimate_id}/attachments", response_model=None)
def list_estimate_attachments(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    _get_estimate_or_404(estimate_id, db)
    rows = db.execute(
        select(Document)
        .where(Document.estimate_id == estimate_id, Document.deleted_at.is_(None))
        .order_by(Document.uploaded_at.desc())
    ).scalars().all()
    return [_serialize_attachment(d) for d in rows]


@router.post("/{estimate_id}/attachments", response_model=None, status_code=201)
def upload_estimate_attachment(
    estimate_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    ct = (file.content_type or "").strip().lower()
    if ct not in ESTIMATE_ATTACHMENT_ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {ct}")
    data = file.file.read()
    if len(data) > ESTIMATE_ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25MB limit")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")
    sanitized = _sanitize_attachment_name(file.filename)
    stored = f"{uuid4().hex}-{sanitized}"
    out_dir = _attachment_dir(tenant_id, str(estimate_id))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / stored
    with out_path.open("wb") as fh:
        fh.write(data)

    doc = Document(
        filename=stored,
        original_name=sanitized,
        file_size=len(data),
        content_type=ct,
        uploaded_by=str((user or {}).get("name") or (user or {}).get("email") or (user or {}).get("sub") or "system"),
        estimate_id=estimate_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    try:
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((user or {}).get("sub") or (user or {}).get("user_id") or "system"),
            action="estimate_attachment_uploaded",
            entity_type="estimate",
            entity_id=str(estimate_id),
            details={"document_id": str(doc.id), "filename": sanitized, "size_bytes": len(data)},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("estimate_attachment_audit_failed")

    return _serialize_attachment(doc)


@router.get("/{estimate_id}/attachments/{document_id}/download", response_model=None)
def download_estimate_attachment(
    estimate_id: UUID,
    document_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    estimate = _get_estimate_or_404(estimate_id, db)
    doc = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.estimate_id == estimate_id,
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Attachment not found")
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")
    base = str(_attachment_dir(tenant_id, str(estimate_id)))
    fullpath = os.path.realpath(os.path.join(base, doc.filename))
    if not fullpath.startswith(base + os.sep) or not os.path.isfile(fullpath):
        raise HTTPException(status_code=404, detail="File missing on disk")
    return FileResponse(
        path=fullpath,
        media_type=doc.content_type or "application/octet-stream",
        filename=doc.original_name,
    )


@router.delete("/{estimate_id}/attachments/{document_id}", response_model=None)
def delete_estimate_attachment(
    estimate_id: UUID,
    document_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _get_estimate_or_404(estimate_id, db)
    doc = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.estimate_id == estimate_id,
            Document.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Attachment not found")
    doc.deleted_at = utcnow()
    db.commit()
    try:
        tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((user or {}).get("sub") or (user or {}).get("user_id") or "system"),
            action="estimate_attachment_deleted",
            entity_type="estimate",
            entity_id=str(estimate_id),
            details={"document_id": str(document_id)},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("estimate_attachment_delete_audit_failed")
    return {"ok": True}
