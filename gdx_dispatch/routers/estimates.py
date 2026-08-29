from __future__ import annotations

import logging
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy import text as _text
from sqlalchemy.orm import Session, selectinload

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_role
from gdx_dispatch.core.pricing_provenance import derive_margin_pct
from gdx_dispatch.core.upload_limits import assert_upload_within_limit
from gdx_dispatch.models.tenant_models import Customer, Document, Job, JobPartNeeded
from gdx_dispatch.modules.deposits import (
    DepositError,
    adopt_orphan_deposit_invoices,
    create_deposit_invoice,
    deposit_skip_reason,
    deposit_summary,
    find_deposit_invoice_for_estimate,
)
from gdx_dispatch.modules.estimates_features import (
    get_features,
    require_line_margin_override_allowed,
)
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


# Moved to core.pricing_provenance so the estimate and invoice sides cannot
# drift into two different formulas for the same money question. Re-imported
# under the old private name so the call sites below are unchanged.
_derive_margin_pct = derive_margin_pct

def _next_estimate_number(db: Session) -> str:
    # Single source of truth — see proposals.service.next_estimate_number.
    from gdx_dispatch.modules.proposals.service import next_estimate_number
    return next_estimate_number(db)


def _next_duplicate_label(db: Session, source_label: str | None) -> str | None:
    """Job name for a duplicated estimate: append an incrementing "-N" suffix.

    Duplicates used to copy the source label verbatim, so option variants of
    the same job (same jobsite, different doors) were indistinguishable in
    lists. Instead we append a numeric suffix — "Front door" -> "Front door-1"
    -> "Front door-2". If the source already ends in a "-N" suffix we increment
    the shared base rather than stacking ("Front door-1" -> "Front door-2", not
    "-1-1"), and we pick the lowest free N so re-duplicating the original keeps
    counting up instead of colliding with an earlier copy.
    """
    import re

    if not source_label or not source_label.strip():
        return source_label

    m = re.match(r"^(.*)-(\d+)$", source_label)
    base = m.group(1) if m else source_label

    # Find suffixes already in use for this base so we pick the next free one.
    # No company/tenant filter — mirrors _next_estimate_number (single-tenant).
    like_base = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    existing = db.execute(
        select(Estimate.label).where(Estimate.label.like(f"{like_base}-%", escape="\\"))
    ).scalars().all()
    used: set[int] = set()
    pattern = re.compile(re.escape(base) + r"-(\d+)$")
    for lbl in existing:
        mm = pattern.match(lbl or "")
        if mm:
            used.add(int(mm.group(1)))

    n = 1
    while n in used:
        n += 1
    suffix = f"-{n}"
    # Respect the String(200) label column limit if the base is very long.
    return f"{base[: 200 - len(suffix)]}{suffix}"


def _next_duplicate_estimate_number(db: Session, source_number: str | None) -> str:
    """Option-variant number for a duplicated estimate: ``<canonical>-N``.

    Doug 2026-07-30: ~95% of duplicates are multiple options for ONE customer,
    so the copy should stay visibly tied to the original — EST-000042 ->
    EST-000042-1, -2, -3 — instead of getting an unrelated fresh number.
    Re-duplicating any variant increments the SHARED base (EST-000042-1 ->
    EST-000042-2, never a stacked -1-1), and we pick the lowest free N.

    Only canonical ``EST-NNNNNN`` numbers get this treatment; a legacy/odd
    source number falls back to a fresh canonical number. Suffixed variants are
    len != 10 so _next_estimate_number ignores them — an option never advances
    the main EST counter.
    """
    import re

    m = re.match(r"^(EST-\d{6})(?:-\d+)?$", source_number or "")
    if not m:
        return _next_estimate_number(db)
    base = m.group(1)

    like_base = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    existing = db.execute(
        select(Estimate.estimate_number).where(
            Estimate.estimate_number.like(f"{like_base}-%", escape="\\")
        )
    ).scalars().all()
    used: set[int] = set()
    pattern = re.compile(re.escape(base) + r"-(\d+)$")
    for num in existing:
        mm = pattern.match(num or "")
        if mm:
            used.add(int(mm.group(1)))

    n = 1
    while n in used:
        n += 1
    return f"{base}-{n}"


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
        # Good/better/best. proposal_mode flips the customer-facing document
        # from one itemized total to a tier picker; accepted_tier_id records
        # which tier they chose. Both were unserialized, so the office UI had
        # no way to see (let alone edit) tiers that mobile could create.
        "proposal_mode": bool(getattr(estimate, "proposal_mode", False)),
        "accepted_tier_id": (
            str(estimate.accepted_tier_id) if getattr(estimate, "accepted_tier_id", None) else None
        ),
        "status": estimate.status,
        "total": _to_float(estimate.total),
        # valid_until is the expiry date (set on send from the tenant's
        # estimate_expiry_days). It was never serialized, so the detail view's
        # "Expires:" line always showed "—". The frontend maps valid_until →
        # expires_at, so emitting it here is the fix.
        "valid_until": estimate.valid_until.isoformat() if getattr(estimate, "valid_until", None) else None,
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
    # An accepted TIER is the contract: est.total was set to the tier's price
    # at accept (2026-08-14 fix), and a post-accept line edit re-summing the
    # base lines would silently revert an $8,000 accept to its $500 scope
    # lines. Line edits on finalized estimates are 409-gated anyway
    # (_ensure_editable); this guard covers reopened-then-edited estimates
    # that still carry accepted_tier_id.
    if estimate.accepted_tier_id is not None:
        from gdx_dispatch.modules.proposals.models import ProposalTier
        from gdx_dispatch.modules.proposals.service import tier_contract_subtotal

        tier = db.execute(
            select(ProposalTier).where(ProposalTier.id == estimate.accepted_tier_id)
        ).scalar_one_or_none()
        if tier is not None:
            estimate.total = _money(_to_float(tier_contract_subtotal(db, tier)))
            estimate.updated_at = utcnow()
            return
    total = db.execute(
        select(func.sum(EstimateLine.line_total)).where(EstimateLine.estimate_id == estimate.id)
    ).scalar_one_or_none() or 0
    estimate.total = _money(_to_float(total))
    estimate.updated_at = utcnow()


def _actor_id(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or user.get("id") or "system")


def _emit_estimate_decision(db: Session, estimate: Estimate, event: str) -> None:
    """Stage an estimate.accepted / estimate.declined webhook. Staged before the
    caller's commit so it dispatches with the business transaction; guarded so it
    can never fail the decision write."""
    from gdx_dispatch.core.webhooks.emit import emit_domain_event

    tid = str(getattr(estimate, "company_id", "") or "")
    cid = getattr(estimate, "customer_id", None)
    emit_domain_event(
        db,
        event,
        str(estimate.id),
        {
            "estimate_id": str(estimate.id),
            "estimate_number": getattr(estimate, "estimate_number", None),
            "status": estimate.status,
            "customer_id": str(cid) if cid else None,
            "company_id": tid,
        },
        tenant_id=tid,
    )


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
    from gdx_dispatch.services.customer_rolling_volume import get_or_refresh
    from gdx_dispatch.services.pricing_engine import CustomerView

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
    # M25: db.get ignored active=False and effective_to — an archived matrix
    # row kept pricing NEW lines at its retired price. Archived == absent.
    _expired = False
    _eff_to = getattr(row, "effective_to", None) if row is not None else None
    if _eff_to is not None:
        from datetime import date as _date
        from datetime import datetime as _dt
        _cmp = _eff_to.date() if isinstance(_eff_to, _dt) else _eff_to
        # Boundary matches billing_lanes/_is_retired ("SAME definition"):
        # a row expiring TODAY still prices; retired means strictly past.
        _expired = _cmp < _date.today()
    if row is None or getattr(row, "active", True) is False or _expired:
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
    # spec a pricing plugin attaches to this line; persisted on the
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
    # Absent for years: the editor's Valid Until field was create-only, so
    # editing it on an existing estimate silently never persisted.
    valid_until: date | None = None
    tax_rate: float | None = Field(default=None, ge=0, le=1)
    discount: float | None = Field(default=None, ge=0, le=999999.99)
    job_id: UUID | None = None
    customer_id: UUID | None = None
    # Tri-state via exclude_unset: field omitted = untouched; explicit null =
    # revert to inherit tenant default; true/false = force hide/show.
    hide_line_prices: bool | None = None
    # Turn the good/better/best tier picker on or off for this estimate.
    # Deliberately NOT `bool | None` like hide_line_prices above: that one is a
    # tri-state override where null means "inherit the tenant default", but
    # proposal_mode is a NOT NULL column with no inherit case. Typed as a plain
    # bool, an explicit null 422s instead of reaching the setattr loop below and
    # writing NULL into a NOT NULL column. Omitting the field still leaves it
    # untouched — that comes from exclude_unset, not from the default.
    # Turning it off leaves any proposal_tiers rows in place so the toggle is
    # reversible without retyping the tiers; only the presentation changes.
    proposal_mode: bool = False


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
    # spec a pricing plugin attaches to this line; persisted on the
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
    # Loss reason is MANDATORY (Doug 2026-07-29: "loss reason is manditory").
    # Every lost estimate must record WHY, so win/loss can be reported on.
    # Mirrors the mobile path (DeclineQuoteIn already requires it); this closes
    # the desktop hole where reason was optional and 85% of declines had none.
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("a loss reason is required to decline an estimate")
        return trimmed


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
    status: str | None = Query(
        default=None,
        pattern=r"^(draft|sent|accepted|declined|rejected|expired)$",
    ),
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, object]]:
    q = select(Estimate).where(Estimate.deleted_at.is_(None))
    if status:
        q = q.where(Estimate.status == status)
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
                # (plugin-captured / typed-catalog doors etc. that don't go through the engine path).
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
            # from a pricing plugin. See EstimateLine.line_metadata.
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
    current_user: dict = Depends(get_current_user),
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    current_user: dict = Depends(get_current_user),
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    current_user: dict = Depends(get_current_user),
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
        # Cost without a pricing_category (typed-catalog / plugin-capture fallback path).
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    current_user: dict = Depends(get_current_user),
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
    # WYSIWYG: an explicitly-sent unit_price is authoritative and must never
    # be overwritten by the engine re-derive below. The autosave client sends
    # unit_price + cost on every flush; before this guard, the re-derive
    # silently reverted manual price edits to cost_snapshot × frozen margin
    # (screen showed the typed price, DB/PDF showed the tier price). A price
    # sent as null keeps the legacy engine-derivation behavior.
    explicit_price = updates.get("unit_price") is not None
    old_unit_price = _to_float(line.unit_price)
    old_cost_snapshot = _to_float(line.cost_snapshot)

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
        # (typed-catalog / plugin-captured doors created before 2026-05-07). Back-derive
        # margin from current cost + unit_price and proceed. Genuine free-form
        # lines (no cost snapshot, no usable unit_price) still 409 — unless
        # the caller sent an explicit price, in which case there is nothing
        # to re-derive and rejecting would abort the client's flush loop.
        healed = _derive_margin_pct(line.cost_snapshot, line.unit_price)
        if healed is not None:
            line.margin_pct_snapshot = healed
            line.pricing_source = line.pricing_source or "client_cost"
            is_engine_line = True
        elif not explicit_price:
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
    if explicit_price:
        # Manual price wins — no sell re-derivation. Keep margin bookkeeping
        # consistent so future cost-only PATCHes re-derive from what the
        # operator actually charges (mirrors the POST cost-fallback path).
        # Skipped when an override is being set: the override is the operator's
        # margin record and the client computes the price from it. Below-cost
        # prices back-derive a negative margin, which the snapshot can hold.
        price_changed = abs(_to_float(line.unit_price) - old_unit_price) > 0.005
        cost_changed = new_cost is not None and abs(_to_float(line.cost_snapshot) - old_cost_snapshot) > 0.005
        if new_override is not None:
            # UI margin edit — the client computes the price from the override
            # and sends both; the override is the operator's margin record.
            line.pricing_source = "line_override"
        elif price_changed or cost_changed:
            derived = _derive_margin_pct(line.cost_snapshot, line.unit_price)
            if derived is not None:
                line.margin_pct_snapshot = derived
                line.pricing_source = line.pricing_source or "client_cost"
    elif is_engine_line and (new_cost is not None or new_override is not None or clear_override):
        # Re-derive sell from frozen margin_pct_snapshot (or override if set).
        # Per decision A: admin tier edits never silently re-price old lines.
        from gdx_dispatch.services.pricing_engine import sell_from_cost

        # M25: `or` discarded an EXPLICIT 0% override (sell-at-cost) back to
        # the tier margin. The very next guard uses the correct idiom.
        effective_margin = (
            line.margin_pct_override
            if line.margin_pct_override is not None
            else line.margin_pct_snapshot
        )
        # A below-cost manual price leaves a negative snapshot behind —
        # sell_from_cost would raise PricingConfigError (500). An operator's
        # below-cost price is deliberate: keep the price, apply the cost edit.
        if Decimal("0") <= Decimal(str(effective_margin)) < Decimal("1"):
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
    current_user: dict = Depends(get_current_user),
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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


def _public_proposal_url(estimate: Estimate) -> str:
    """Absolute customer-facing approval-page URL, or "" when
    GDX_PUBLIC_BASE_URL is unset. The explicit guard matters: a bare f-string
    over an empty base yields "/proposals/<tok>" — truthy, so the email
    builder would render a relative href that is dead in a mail client."""
    base = os.getenv("GDX_PUBLIC_BASE_URL", "").rstrip("/")
    if not (base and estimate.public_token):
        return ""
    return f"{base}/proposals/{estimate.public_token}"


def _render_template(tpl: str, ctx: dict[str, str]) -> str:
    """Lightweight {{placeholder}} substitution — no logic, no escapes."""
    out = tpl
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", v)
        out = out.replace("{{ " + k + " }}", v)
    return out


def _estimate_pdf_bytes(
    db: Session,
    estimate: Estimate,
    customer: Customer | None,
    tenant_id: str,
) -> bytes:
    """Render the customer-facing estimate PDF — the same bytes /pdf serves
    and the composer attaches. Shared by email-compose and /send so every
    outbound email carries the identical document."""
    from gdx_dispatch.core.pdf_generator import generate_estimate_pdf
    from gdx_dispatch.routers.pdf import (
        _branding_payload,
        _estimate_attachments_for_pdf,
        _estimate_payload,
        _template_config,
    )

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
    return generate_estimate_pdf(
        estimate_data=_estimate_payload(
            estimate, customer, default_terms=default_terms,
            attachment_images=images, attachment_files=files,
            deposit_pct=deposit_pct, hide_line_prices_default=hide_line_prices_default, db=db,
        ),
        tenant_branding=_branding_payload(db),
        template_config=_template_config(db, "estimate"),
    )


@router.get("/{estimate_id}/email-compose", response_model=None)
def estimate_email_compose(
    estimate_id: UUID,
    request: Request,
    contact_id: str | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Return a prebuilt compose payload for the in-app composer:
    {to, recipients, subject, body_text, pdf, extra_attachments}.
    Subject/body come from per-tenant templates configurable in
    Settings → Feature Settings — rendered by the SAME prep the send path
    uses, so the composer previews exactly what /send delivers.
    ?contact_id=<id> re-renders the prefill addressed to that contact."""
    import base64 as _b64

    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or estimate.company_id or "")
    prep = _prepare_estimate_email(db, estimate, contact_id=contact_id)
    customer = prep["customer"]
    recipient = prep["recipient"]
    subject = prep["subject"]
    body_text = prep["body_text"]
    pdf_bytes = _estimate_pdf_bytes(db, estimate, customer, tenant_id)
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
        "to": [recipient.email] if (recipient and recipient.ok) else [],
        "recipients": _estimate_recipient_options(db, customer),
        "selected_contact_id": recipient.contact_id if recipient else None,
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


def _apply_send_expiry(estimate: Estimate) -> None:
    """On send, stamp valid_until = sent_at + the tenant's estimate_expiry_days
    (default 60). Without this, valid_until stayed NULL and the nightly expire
    task never fired.

    Refresh rule: set valid_until when it's missing OR already in the past
    (relative to this send) — so re-sending an expired estimate gives it a
    FRESH window instead of leaving a stale past date the nightly task would
    just re-expire the next night. A still-future valid_until (a deliberately
    hand-picked date) is respected and left alone.

    Note: this relies on create NOT persisting a valid_until — the /estimates
    create handler drops the field, so a fresh estimate reaches send with
    valid_until = NULL. If create is ever changed to honor payload.valid_until,
    a create-time default would look like a hand-picked future date here and
    silently defeat the tenant setting. Keep create dropping it, or teach this
    helper to distinguish a default from an override.

    Best-effort: a features read failure must not block the send."""
    sent_at = estimate.sent_at
    if not sent_at:
        return
    existing = getattr(estimate, "valid_until", None)
    if existing is not None:
        # SQLite (tests) returns naive datetimes; PG returns aware. Normalize
        # both to UTC-aware so the comparison never raises on a naive/aware mix.
        if existing.tzinfo is None:
            existing = existing.replace(tzinfo=timezone.utc)
        sent_cmp = sent_at if sent_at.tzinfo is not None else sent_at.replace(tzinfo=timezone.utc)
        if existing > sent_cmp:
            return
    try:
        days = int(get_features(str(estimate.company_id or "")).estimate_expiry_days or 60)
    except Exception:
        days = 60
    if days < 1:
        days = 60
    estimate.valid_until = estimate.sent_at + timedelta(days=days)


class MarkEstimateSentIn(BaseModel):
    # 'manual' default keeps old callers' rows meaning what they always meant
    # ("operator says it went out, channel unknown"); the mailto fallback
    # passes 'email'. Mirrors invoices' MarkSentIn — before this, the channel
    # was a HARDCODED audit blob and estimates.sent_via stayed NULL on every
    # out-of-band send (caught in the 2026-08-18 browser walk).
    channel: str = Field(default="manual", pattern=r"^(manual|email|mail)$")


@router.post("/{estimate_id}/mark-sent", response_model=None)
def mark_estimate_sent(
    estimate_id: UUID,
    payload: MarkEstimateSentIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Flip status to 'sent' without firing a server-side email. Used when
    the operator composes the email manually in their own mail client."""
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status in {"accepted", "declined"}:
        raise HTTPException(status_code=409, detail="estimate is finalized")
    channel = (payload or MarkEstimateSentIn()).channel
    estimate.status = "sent"
    estimate.sent_at = utcnow()
    estimate.sent_via = channel
    _apply_send_expiry(estimate)
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
        details={"status": estimate.status, "channel": channel},
    )
    db.commit()
    return _serialize_estimate(estimate, include_lines=False)


def _expected_valid_until(estimate: Estimate) -> datetime:
    """The valid_until this send WILL produce — mirrors _apply_send_expiry's
    rules (respect a hand-picked future date; else tenant expiry days from
    now), computed BEFORE compose so the email can print the real date. The
    old body hardcoded "valid for 30 days" while the actual default is 60."""
    existing = getattr(estimate, "valid_until", None)
    now = utcnow()
    if existing is not None:
        e = existing if existing.tzinfo is not None else existing.replace(tzinfo=timezone.utc)
        if e > now:
            return e
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        days = int(get_features(str(estimate.company_id or "")).estimate_expiry_days or 60)
    except Exception:
        days = 60
    if days < 1:
        days = 60
    return now + timedelta(days=days)


def _human_date(dt: datetime) -> str:
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def _estimate_email_templates(tenant_id: str) -> tuple[str, str]:
    """Tenant-editable subject/body templates with platform defaults.
    Shared by compose, preview and SEND — the locked decision: what the
    composer previews is what every path delivers."""
    subject_tpl = ""
    body_tpl = ""
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        if tenant_id:
            f = get_features(tenant_id)
            subject_tpl = (f.email_subject_template or "").strip()
            body_tpl = (f.email_body_template or "").strip()
    except Exception:
        log.exception("email_templates_read_features_failed")
    return subject_tpl or _DEFAULT_SUBJECT_TEMPLATE, body_tpl or _DEFAULT_BODY_TEMPLATE


def _effective_hide_line_prices(estimate: Estimate, tenant_id: str) -> bool:
    """Estimate tri-state override, else tenant default — same resolution the
    PDF uses. The email previously ignored this and leaked per-line prices
    on total-only estimates."""
    if estimate.hide_line_prices is not None:
        return bool(estimate.hide_line_prices)
    try:
        from gdx_dispatch.modules.estimates_features import get_features
        return bool(get_features(tenant_id).hide_line_prices) if tenant_id else False
    except Exception:
        return False


def _prepare_estimate_email(
    db: Session,
    estimate: Estimate,
    *,
    contact_id: str | None = None,
    body_text_override: str | None = None,
    subject_override: str | None = None,
    to_email_override: str | None = None,
) -> dict[str, object]:
    """One render for every path — composer, preview, one-click send.

    Returns {customer, recipient, subject, body_text, html, branding,
    proposal_url}. body_text is the plain-text copy (template-rendered or
    the operator's edit, approval link appended for mailto/preview); html is
    the full branded email: shell + copy + tier summary or line table +
    totals + CTA button + real expiry date."""
    from gdx_dispatch.core.email_layout import (
        email_branding,
        linkify,
        nl2br,
    )
    from gdx_dispatch.core.email_recipients import resolve_recipient
    from gdx_dispatch.core.email_sender import build_estimate_email_html
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    customer = None
    if estimate.customer_id:
        customer = db.execute(
            select(Customer).where(Customer.id == estimate.customer_id, Customer.deleted_at.is_(None))
        ).scalar_one_or_none()

    recipient = None
    if to_email_override and (to_email_override or "").strip():
        from gdx_dispatch.core.email_recipients import override_recipient
        recipient = override_recipient(
            to_email_override, (customer.name if customer else "") or "",
        )
    elif customer is not None:
        recipient = resolve_recipient(db, customer, contact_id)

    job_title = ""
    if estimate.job_id:
        job = db.execute(
            select(Job).where(Job.id == estimate.job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if job:
            job_title = (job.title or "").strip()

    branding = email_branding(db)
    company_name = branding["company_name"]
    tenant_id = str(estimate.company_id or "")

    subject_tpl, body_tpl = _estimate_email_templates(tenant_id)
    totals = compute_estimate_totals(estimate, db)
    label_or_job = job_title or (estimate.label or "").strip() or f"Estimate {estimate.estimate_number or ''}".strip()
    proposal_url = _public_proposal_url(estimate)
    if estimate.sent_at is None and estimate.status in {"accepted", "declined"}:
        proposal_url = ""
    greeting = (recipient.greeting_name if recipient and recipient.ok else "") or \
        ((customer.name if customer else "") or "there")
    ctx = {
        "customer_name": greeting,
        "job_title": label_or_job,
        "estimate_number": estimate.estimate_number or str(estimate.id)[:8],
        "estimate_label": (estimate.label or "").strip(),
        "company_name": company_name,
        "total": f"${totals['total']:,.2f}",
        "estimate_link": proposal_url,
    }
    subject = (subject_override or "").strip() or _render_template(subject_tpl, ctx).strip() or label_or_job
    if body_text_override is not None and body_text_override.strip():
        body_text = body_text_override
    else:
        body_text = _render_template(body_tpl, ctx)
    link_line = f"Review & approve online: {proposal_url}" if proposal_url else ""
    if proposal_url and proposal_url not in body_text:
        body_text = f"{body_text}\n\n{link_line}"

    # The branded email carries the link as a real button — drop the
    # appended text line so it isn't said twice; a link the OPERATOR wrote
    # into the copy (or the template placed via {{estimate_link}}) stays,
    # linkified so it is clickable in Outlook.
    copy_for_html = body_text
    if link_line and copy_for_html.rstrip().endswith(link_line):
        copy_for_html = copy_for_html.rstrip()[: -len(link_line)].rstrip()
    intro_html = "<p style=\"margin:0 0 12px;\">" + linkify(
        nl2br(copy_for_html), branding.get("accent") or "#2563eb"
    ).replace("<br><br>", "</p><p style=\"margin:0 0 12px;\">") + "</p>"

    tiers = None
    line_items: list[dict] = []
    if estimate.proposal_mode:
        from gdx_dispatch.modules.proposals.models import ProposalTier
        tier_rows = db.execute(
            select(ProposalTier)
            .where(ProposalTier.estimate_id == estimate.id)
            .order_by(ProposalTier.display_order)
        ).scalars().all()
        tiers = [
            {
                "name": (t.tier_name or "").title(),
                "price": _to_float(t.total_price),
                "description": (t.description or "").strip(),
            }
            for t in tier_rows
        ]
    else:
        lines = db.execute(
            select(EstimateLine)
            .where(EstimateLine.estimate_id == estimate.id)
            .order_by(EstimateLine.sort_order)
        ).scalars().all()
        line_items = [
            {
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_price": _to_float(ln.unit_price),
                "line_total": _to_float(ln.line_total),
            }
            for ln in lines
        ]

    html = build_estimate_email_html(
        company_name=company_name,
        estimate_number=estimate.estimate_number or str(estimate.id)[:8],
        customer_name=greeting,
        line_items=line_items,
        total=totals["total"],
        notes=estimate.notes or "",
        portal_url=proposal_url,
        description=estimate.description or "",
        branding=branding,
        intro_html=intro_html,
        tiers=tiers,
        valid_until_text=_human_date(_expected_valid_until(estimate)),
        hide_prices=_effective_hide_line_prices(estimate, tenant_id),
    )
    return {
        "customer": customer,
        "recipient": recipient,
        "subject": subject,
        "body_text": body_text,
        "html": html,
        "branding": branding,
        "proposal_url": proposal_url,
    }


def _estimate_recipient_options(db: Session, customer: Customer | None) -> list[dict[str, object]]:
    """Composer picker choices: the account email plus every live contact
    with an email. is_primary marks the default automated sends will use."""
    options: list[dict[str, object]] = []
    if customer is None:
        return options
    if customer.email:
        options.append({
            "contact_id": None,
            "name": customer.name or "",
            "email": customer.email,
            "label": "Account email",
            "is_primary": False,
        })
    from gdx_dispatch.models.tenant_models import CustomerContact
    rows = db.execute(
        select(CustomerContact).where(
            CustomerContact.customer_id == customer.id,
            CustomerContact.deleted_at.is_(None),
        ).order_by(CustomerContact.created_at)
    ).scalars().all()
    for c in rows:
        if (c.email or "").strip():
            options.append({
                "contact_id": str(c.id),
                "name": c.name or "",
                "email": c.email,
                "label": (c.label or "").strip() or "Contact",
                "is_primary": bool(c.is_primary),
            })
    return options


def _estimate_extra_attachment_payloads(
    db: Session,
    estimate: Estimate,
    tenant_id: str,
    ids: list[str],
    budget_bytes: int,
) -> tuple[list[dict[str, object]], list[str]]:
    """Load requested estimate documents as wire attachments, newest budget
    rule: attach in the order requested while raw bytes fit the remaining
    budget; return (attachments, skipped_names). Same realpath containment
    as the download endpoint."""
    import base64 as _b64

    attachments: list[dict[str, object]] = []
    skipped: list[str] = []
    if not ids:
        return attachments, skipped
    rows = db.execute(
        select(Document).where(
            Document.id.in_(ids),
            Document.estimate_id == estimate.id,
            Document.deleted_at.is_(None),
        )
    ).scalars().all()
    by_id = {str(d.id): d for d in rows}
    base = str(_attachment_dir(tenant_id, str(estimate.id)))
    remaining = budget_bytes
    for want in ids:
        doc = by_id.get(str(want))
        if doc is None:
            skipped.append(str(want))
            continue
        fullpath = os.path.realpath(os.path.join(base, doc.filename))
        if not fullpath.startswith(base + os.sep) or not os.path.isfile(fullpath):
            skipped.append(doc.original_name or str(doc.id))
            continue
        try:
            with open(fullpath, "rb") as fh:
                data = fh.read()
        except OSError:
            skipped.append(doc.original_name or str(doc.id))
            continue
        if len(data) > remaining:
            skipped.append(doc.original_name or str(doc.id))
            continue
        remaining -= len(data)
        attachments.append({
            "name": _sanitize_attachment_name(doc.original_name),
            "content_type": doc.content_type or "application/octet-stream",
            "content_base64": _b64.b64encode(data).decode("ascii"),
        })
    return attachments, skipped


class SendEstimateIn(BaseModel):
    """Optional composer payload for /send. Empty body = one-click send with
    the tenant template's default copy and the resolver's default recipient."""

    body_text: str | None = None
    subject: str | None = Field(default=None, max_length=500)
    contact_id: str | None = Field(default=None, max_length=36)
    # Free-typed address (audit fix 2026-08-18): a customer with NO stored
    # email shows the composer's free InputText — that address must reach the
    # server or the operator watches their own typing get ignored.
    to_email: str | None = Field(default=None, max_length=254)
    extra_attachment_ids: list[str] | None = None


@router.post("/{estimate_id}/email-preview", response_model=None)
def estimate_email_preview(
    estimate_id: UUID,
    payload: SendEstimateIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """The exact branded HTML /send would deliver, for the composer's
    preview pane — same prep function, zero drift by construction."""
    estimate = _get_estimate_or_404(estimate_id, db)
    p = payload or SendEstimateIn()
    prep = _prepare_estimate_email(
        db, estimate,
        contact_id=p.contact_id,
        body_text_override=p.body_text,
        subject_override=p.subject,
        to_email_override=p.to_email,
    )
    recipient = prep["recipient"]
    return {
        "subject": prep["subject"],
        "html": prep["html"],
        "to_email": recipient.email if recipient else "",
        "to_name": recipient.to_name if recipient else "",
    }


@router.post("/{estimate_id}/send", response_model=None)
def send_estimate(
    estimate_id: UUID,
    payload: SendEstimateIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    if estimate.status in {"accepted", "declined"}:
        raise HTTPException(status_code=409, detail="estimate is finalized")

    # Send the estimate email to the customer. Routes through the
    # unified transactional-email helper so an Outlook-connected user
    # actually delivers via Graph; falls back to SMTP via email_settings.
    # The status flip comes AFTER, gated on a provider actually accepting
    # the message (2026-08-13): this endpoint used to stamp sent/sent_at
    # up front, so a failed send still read "sent" everywhere. Manual
    # out-of-band delivery has its own endpoint (/mark-sent).
    email_sent = False
    email_provider: str | None = None
    email_skip_reason: str | None = None
    pdf_attached = False
    attachments_skipped: list[str] = []
    p = payload or SendEstimateIn()
    try:
        from gdx_dispatch.core.transactional_email import (
            MAX_INLINE_ATTACHMENT_BYTES,
            recently_sent,
            send_transactional_email,
        )
        tid = str(estimate.company_id) if estimate.company_id else None
        if tid and estimate.customer_id and recently_sent(db, "estimate", str(estimate.id)):
            # Server-side double-send guard: two tabs / a retried request /
            # a double-clicked button must not email the customer twice.
            email_skip_reason = "duplicate_send_suppressed"
        elif tid and estimate.customer_id:
            # One prep for composer sends and one-click sends alike: tenant
            # template copy (or the operator's edit) inside the branded
            # shell, tier summary or line table, real expiry date, CTA
            # button, person-aware recipient.
            prep = _prepare_estimate_email(
                db, estimate,
                contact_id=p.contact_id,
                body_text_override=p.body_text,
                subject_override=p.subject,
                to_email_override=p.to_email,
            )
            cust = prep["customer"]
            recipient = prep["recipient"]
            if cust is not None and recipient is not None and recipient.ok:
                # Attach the estimate PDF (same bytes the composer previews)
                # plus any operator-selected extra documents, inside one raw-
                # byte budget so an oversized set degrades attachment-by-
                # attachment instead of Graph rejecting the whole message.
                attachments: list[dict[str, object]] = []
                try:
                    import base64 as _b64

                    pdf_bytes = _estimate_pdf_bytes(db, estimate, cust, tid)
                    if len(pdf_bytes) > MAX_INLINE_ATTACHMENT_BYTES:
                        log.warning(
                            "estimate_send_pdf_too_large_to_attach estimate=%s bytes=%s",
                            estimate.id, len(pdf_bytes),
                        )
                        attachments_skipped.append("estimate PDF")
                        budget = MAX_INLINE_ATTACHMENT_BYTES
                    else:
                        attachments.append({
                            "name": f"estimate-{estimate.estimate_number or str(estimate.id)[:8]}.pdf",
                            "content_type": "application/pdf",
                            "content_base64": _b64.b64encode(pdf_bytes).decode("ascii"),
                        })
                        budget = MAX_INLINE_ATTACHMENT_BYTES - len(pdf_bytes)
                except Exception:
                    log.exception("estimate_send_pdf_attach_failed")
                    budget = MAX_INLINE_ATTACHMENT_BYTES
                extra, extra_skipped = _estimate_extra_attachment_payloads(
                    db, estimate, tid, p.extra_attachment_ids or [], budget,
                )
                attachments.extend(extra)
                attachments_skipped.extend(extra_skipped)
                email_sent, email_provider, email_skip_reason = send_transactional_email(
                    tenant_db=db,
                    tenant_id=tid,
                    user_id=str(_actor_id(_)),
                    to_email=recipient.email,
                    to_name=recipient.to_name,
                    subject=prep["subject"],
                    html_body=prep["html"],
                    attachments=attachments or None,
                    kind="document",
                    entity_type="estimate",
                    entity_id=str(estimate.id),
                    recipient_source=recipient.source,
                    recipient_contact_id=recipient.contact_id,
                )
                pdf_attached = email_sent and any(
                    a["name"].startswith("estimate-") for a in attachments
                )
            elif recipient is not None and recipient.source == "invalid_override":
                email_skip_reason = "invalid_recipient_email"
            elif cust is not None:
                email_skip_reason = "customer_has_no_email"
            else:
                email_skip_reason = "customer_not_found"
        elif not estimate.customer_id:
            email_skip_reason = "estimate_has_no_customer"
    except Exception:
        log.exception("estimate_email_send_failed")
        email_skip_reason = "exception"

    if email_sent:
        estimate.status = "sent"
        estimate.sent_at = utcnow()
        estimate.sent_via = "email"
        _apply_send_expiry(estimate)
        estimate.updated_at = utcnow()
        # estimate.sent domain event (audit round 2: SUPPORTED_TRIGGERS
        # advertised it but nothing ever emitted it — a rule on the marquee
        # trigger of an email branch sat dead forever).
        try:
            from gdx_dispatch.core.webhooks.emit import emit_domain_event
            tid_ev = str(estimate.company_id or "")
            emit_domain_event(
                db,
                "estimate.sent",
                str(estimate.id),
                {
                    "estimate_id": str(estimate.id),
                    "estimate_number": estimate.estimate_number,
                    "status": "sent",
                    "customer_id": str(estimate.customer_id) if estimate.customer_id else None,
                    "company_id": tid_ev,
                },
                tenant_id=tid_ev or None,
            )
        except Exception:
            log.exception("estimate_sent_event_emit_failed")
        db.commit()
        db.refresh(estimate)
        log_audit_event_sync(
            db=db,
            tenant_id=None,
            user_id=_actor_id(_),
            action="estimate_sent",
            entity_type="estimate",
            entity_id=str(estimate.id),
            details={"status": estimate.status, "provider": email_provider},
        )
        db.commit()
    else:
        # No provider accepted the message — the estimate's status is
        # UNCHANGED (a failed re-send must not un-send an earlier
        # delivery either). The audit row + response payload carry why.
        log_audit_event_sync(
            db=db,
            tenant_id=None,
            user_id=_actor_id(_),
            action="estimate_send_failed",
            entity_type="estimate",
            entity_id=str(estimate.id),
            details={"status": estimate.status, "skip_reason": email_skip_reason},
        )
        db.commit()

    out = _serialize_estimate(estimate, include_lines=False)
    out["email_sent"] = email_sent
    out["pdf_attached"] = pdf_attached
    if attachments_skipped:
        # Silent PDF degradation was invisible before — the UI can now say
        # "sent without <name> (too large)".
        out["attachments_skipped"] = attachments_skipped
    if email_provider:
        out["email_provider"] = email_provider
    if email_skip_reason:
        out["email_skip_reason"] = email_skip_reason
    return out


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


def _copy_tier_package_to_job(estimate, new_job, db: Session) -> int | None:
    """When a TIER was accepted, the job carries the accepted package — its
    tier lines when it is line-built, else one row named for the tier.
    Returns None when no tier is accepted (caller falls through to the
    estimate-lines copy).

    The estimate_lines rows are deliberately NOT copied on the tier path:
    on office-built proposals they are base scope under a differently-priced
    package, and on MOBILE-built proposals they are all three tiers' items
    untagged — copying them handed receiving three doors for a one-door job.
    Scope detail stays readable on the linked estimate.
    """
    if getattr(estimate, "accepted_tier_id", None) is None:
        return None
    from gdx_dispatch.modules.proposals.models import ProposalTier
    from gdx_dispatch.modules.proposals.service import tier_contract_lines

    tier = db.execute(
        select(ProposalTier).where(ProposalTier.id == estimate.accepted_tier_id)
    ).scalar_one_or_none()
    if tier is None:
        return None
    now = utcnow()
    company = str(new_job.company_id or estimate.company_id or "")
    tier_lines = tier_contract_lines(db, tier)
    copied = 0
    if tier_lines:
        for line in tier_lines:
            note_bits = []
            if line.category:
                note_bits.append(str(line.category))
            if line.unit_price:
                note_bits.append(f"${_to_float(line.unit_price):.2f} ea")
            db.add(JobPartNeeded(
                id=str(uuid4()),
                company_id=company,
                job_id=str(new_job.id),
                part_name=(line.description or "Item")[:200],
                quantity=int(line.quantity or 1),
                status="needed",
                notes=(" • ".join(note_bits) or None),
                created_at=now,
                updated_at=now,
            ))
            copied += 1
    else:
        label = {"good": "Good", "better": "Better", "best": "Best"}.get(tier.tier_name, tier.tier_name)
        name = f"{label} package"
        if tier.description:
            name = f"{name} — {tier.description}"
        db.add(JobPartNeeded(
            id=str(uuid4()),
            company_id=company,
            job_id=str(new_job.id),
            part_name=name[:200],
            quantity=1,
            status="needed",
            notes=f"Accepted tier • ${_to_float(tier.total_price):.2f}",
            created_at=now,
            updated_at=now,
        ))
        copied = 1
    return copied


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


def _bind_estimate_jobsite(estimate, new_job, db: Session, actor: str) -> None:
    """Best-effort, POST-commit: a non-blank ``jobsite_address`` becomes a
    real ``customer_locations`` binding on the new job.

    Semantics (jobsite plan PR 3, D4-revised): NULL/blank means "same as the
    customer's address" — nothing to do. Non-blank is an EXPLICIT different
    address the office wrote on the estimate; it find-or-creates a location
    row (``is_primary: false`` — inert for the customer's other jobs under
    resolver rule 2) and binds ``job.location_id`` so the tech's phone shows
    the sold jobsite, not the HQ.

    Runs strictly AFTER the conversion's own commit and is internally
    guarded (pre-code audit §3b): a failure here must never sink the accept
    — but it is never silent either (log + audit event + the raw address
    appended to the job's notes so what the customer approved isn't lost).
    Reads ONLY the stored estimate field — never anything client-supplied at
    accept time (trap 7: the public token-holder gains no address write).
    """
    from gdx_dispatch.core.job_site import normalize_address  # noqa: PLC0415

    raw = (getattr(estimate, "jobsite_address", None) or "").strip()
    if not raw or not estimate.customer_id:
        return
    try:
        # Inside the guard — EVERY failure from here down must degrade to
        # the notes-append path, never escape to the caller.
        want = normalize_address(raw)
        customer = db.execute(
            select(Customer).where(Customer.id == estimate.customer_id)
        ).scalar_one_or_none()
        if customer is not None and normalize_address(customer.address) == want:
            # Typed-but-identical: the customer address already covers it.
            return
        # Shared find-or-create (core/job_site.py) — one convergence rule for
        # the conversion bind AND the tech's fix-address endpoint (PR 4).
        from gdx_dispatch.core.job_site import find_or_create_customer_location  # noqa: PLC0415

        loc_id, created = find_or_create_customer_location(
            db, estimate.customer_id, raw,
            label=f"Jobsite ({estimate.estimate_number})",
            company_id=estimate.company_id or "",
        )
        new_job.location_id = loc_id
        db.commit()
        # Invariant #1: the auto-created row is a mutation with no router of
        # its own — it writes its own audit trail, attributed to the acting
        # user (or the public-accept actor).
        if created:
            log_audit_event_sync(
                db=db, tenant_id=None, user_id=actor,
                action="create_customer_location",
                entity_type="customer_location",
                entity_id=loc_id,
                details={
                    "source": "estimate_conversion",
                    "estimate_id": str(estimate.id),
                    "customer_id": str(estimate.customer_id),
                },
            )
        log_audit_event_sync(
            db=db, tenant_id=None, user_id=actor,
            action="estimate_jobsite_bound",
            entity_type="job",
            entity_id=str(new_job.id),
            details={
                "estimate_id": str(estimate.id),
                "location_id": loc_id,
                "location_created": created,
            },
        )
        db.commit()  # one commit carries both audit rows
    except Exception:  # noqa: BLE001 — the accept must survive ANY bind failure
        db.rollback()
        logging.getLogger(__name__).exception(
            "estimate_jobsite_bind_failed estimate=%s job=%s", estimate.id, new_job.id
        )
        # Never silent (CLAUDE.md no-silent-writes): preserve the address the
        # customer approved on the job itself, and leave an audit trail.
        try:
            # ORM, not raw SQL: a dashed-UUID param silently matches zero
            # rows on SQLite (the id is stored undashed) — the append would
            # "succeed" while writing nothing.
            new_job.notes = (new_job.notes or "") + (
                f"\n[jobsite from estimate {estimate.estimate_number}: {raw}]"
            )
            db.commit()
            log_audit_event_sync(
                db=db, tenant_id=None, user_id=actor,
                action="estimate_jobsite_bind_failed",
                entity_type="job",
                entity_id=str(new_job.id),
                details={"estimate_id": str(estimate.id)},
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logging.getLogger(__name__).exception(
                "estimate_jobsite_bind_failure_note_failed job=%s", new_job.id
            )


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

    copied_lines = _copy_tier_package_to_job(estimate, new_job, db)
    if copied_lines is None:
        copied_lines = _copy_estimate_lines_to_job(estimate, new_job, db)

    estimate.job_id = new_job.id
    estimate.updated_at = utcnow()
    # Deposit invoices born before the job existed (mobile accept creates no
    # job) get job_id backfilled so final-invoice netting can find them.
    adopt_orphan_deposit_invoices(db, estimate, new_job.id)
    db.commit()
    db.refresh(new_job)

    # The sold jobsite rides the conversion (jobsite plan PR 3). AFTER the
    # commit above, internally guarded — a bind failure must degrade to an
    # unbound job, never to no job at all.
    _bind_estimate_jobsite(estimate, new_job, db, actor)

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


class AcceptEstimateIn(BaseModel):
    """Optional accept-time deposit request (2026-07-23).

    None → no deposit invoice (backward compatible: every existing caller
    posts `{}`). A positive amount creates a billing_type='deposit' invoice
    the customer can pay immediately via the public /pay page. 0 is an
    explicit "no deposit" — same effect as None, distinct for audit trails.
    """

    deposit_amount: float | None = Field(default=None, ge=0, le=1_000_000)


@router.get("/{estimate_id}/deposit-default", response_model=None)
def estimate_deposit_default(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """What the accept dialog should pre-fill: the tenant's deposit percent
    (estimate_deposit_pct — the same number the estimate PDF has printed as
    '% Down' all along) applied to the estimate total, plus any deposit
    invoice that already exists for this estimate (mobile/portal beat us)."""
    estimate = _get_estimate_or_404(estimate_id, db)
    from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

    total = _to_float(compute_estimate_totals(estimate, db)["total"])
    pct = 0
    try:
        pct = max(0, min(100, int(get_features(str(_.get("tenant_id") or "")).deposit_pct or 0)))
    except Exception:
        log.exception("deposit_default_features_read_failed")
    existing = find_deposit_invoice_for_estimate(db, estimate.id)
    return {
        "pct": pct,
        "estimate_total": total,
        "amount": round(total * pct / 100.0, 2),
        "existing": deposit_summary(existing) if existing else None,
    }


class DepositInvoiceIn(BaseModel):
    """Explicit deposit request for an ALREADY-ACCEPTED estimate (2026-07-23,
    Doug: 'no way of applying money/deposits after the fact'). amount None →
    tenant deposit percent × estimate total."""

    amount: float | None = Field(default=None, gt=0, le=1_000_000)


@router.post("/{estimate_id}/deposit-invoice", response_model=None)
def request_deposit_invoice(
    estimate_id: UUID,
    payload: DepositInvoiceIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Create (or return the existing) deposit invoice for an accepted
    estimate — the retroactive path for estimates accepted before the
    deposit feature existed, or accepted with the deposit step skipped.
    Idempotent per estimate (same rule as the accept-time flow)."""
    estimate = _get_estimate_or_404(estimate_id, db)
    if (estimate.status or "").lower() != "accepted":
        raise HTTPException(
            status_code=409,
            detail="deposit invoices are for accepted estimates — accept it first",
        )
    existing = find_deposit_invoice_for_estimate(db, estimate.id)
    if existing is not None:
        out = deposit_summary(existing)
        out["existing"] = True
        return out

    amount = payload.amount if payload else None
    if amount is None:
        from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

        pct = 0
        try:
            pct = max(0, min(100, int(get_features(str(_.get("tenant_id") or "")).deposit_pct or 0)))
        except Exception:
            log.exception("deposit_invoice_features_read_failed")
        total = _to_float(compute_estimate_totals(estimate, db)["total"])
        amount = round(total * pct / 100.0, 2)
    if not amount or amount <= 0:
        raise HTTPException(
            status_code=422,
            detail="no deposit amount — pass one, or set a deposit percent in estimate settings",
        )
    try:
        dep_inv = create_deposit_invoice(
            db,
            estimate=estimate,
            amount=float(amount),
            tenant_id=str(_.get("tenant_id") or estimate.company_id or ""),
            actor=_actor_id(_),
            source="office_request",
        )
    except DepositError as exc:
        raise HTTPException(status_code=422, detail=deposit_skip_reason(exc)) from exc
    out = deposit_summary(dep_inv)
    out["existing"] = False
    return out


@router.post("/{estimate_id}/accept", response_model=None)
def accept_estimate(
    estimate_id: UUID,
    payload: AcceptEstimateIn | None = None,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    # M25: accept was read-then-write — two concurrent accepts both passed
    # the status check and each minted its side effects (deposit invoices
    # included). Lock the row; the loser blocks, re-reads, and 409s.
    estimate = db.execute(
        select(Estimate)
        .where(Estimate.id == estimate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if estimate.status == "accepted":
        raise HTTPException(status_code=409, detail="already accepted")
    if estimate.status == "declined":
        raise HTTPException(status_code=409, detail="cannot accept a declined estimate")
    estimate.status = "accepted"
    estimate.accepted_at = utcnow()
    estimate.updated_at = utcnow()
    _emit_estimate_decision(db, estimate, "estimate.accepted")
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

    # Deposit invoice (2026-07-23): created AFTER conversion so it lands
    # with job_id set. A deposit failure must never un-accept the estimate —
    # capture beats billing; the office can invoice the deposit manually.
    deposit_info: dict[str, object] | None = None
    deposit_skipped: str | None = None
    requested = payload.deposit_amount if payload else None
    if requested and requested > 0:
        db.refresh(estimate)
        try:
            dep_inv = create_deposit_invoice(
                db,
                estimate=estimate,
                amount=float(requested),
                tenant_id=str(_.get("tenant_id") or estimate.company_id or ""),
                actor=actor,
                source="office_accept",
            )
            deposit_info = deposit_summary(dep_inv)
        except DepositError as exc:
            deposit_skipped = deposit_skip_reason(exc)
        except Exception:
            logging.getLogger(__name__).exception(
                "deposit_invoice_failed_on_accept estimate=%s", estimate.id
            )
            deposit_skipped = "deposit invoice creation failed — create it from Billing"

    payload_out = _serialize_estimate(estimate, include_lines=False)
    if auto_converted_job_id:
        payload_out["auto_converted_job_id"] = auto_converted_job_id
    if convert_skipped_reason:
        payload_out["auto_convert_skipped"] = convert_skipped_reason
    if deposit_info:
        payload_out["deposit"] = deposit_info
    if deposit_skipped:
        payload_out["deposit_skipped"] = deposit_skipped
    return payload_out


@router.post("/{estimate_id}/decline", response_model=None)
def decline_estimate(
    estimate_id: UUID,
    payload: DeclineIn,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    # M25 audit round 2: same finalization-race shape as accept — lock and
    # recheck so decline-vs-accept cannot interleave.
    estimate = db.execute(
        select(Estimate)
        .where(Estimate.id == estimate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()
    if estimate.status == "declined":
        raise HTTPException(status_code=409, detail="already declined")
    if estimate.status == "accepted":
        raise HTTPException(status_code=409, detail="cannot decline an accepted estimate")
    estimate.status = "declined"
    estimate.declined_at = utcnow()
    # Validator guarantees a non-empty, stripped reason — no None fallback.
    estimate.declined_reason = payload.reason
    estimate.updated_at = utcnow()
    _emit_estimate_decision(db, estimate, "estimate.declined")
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


def _compute_price_drift(estimate: Estimate, db: Session) -> list[dict]:
    """Which of this estimate's lines have gone stale since it was quoted?

    Reliable only for labor-matrix lines (labor_price_item_id set): compare the
    quoted unit_price to the matrix row's CURRENT flat_price. A row that's been
    archived (deleted → FK SET NULL) or deactivated is also drift — the price
    the customer saw is no longer one we stand behind. Free-form / parts lines
    have no catalog FK, so they can't be auto-checked and are left out (the UI
    says as much). Used to warn on reopen — 'people come back months later and
    the numbers are still good' is the hope; this proves it or flags it.
    """
    import datetime as _dt

    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    def _is_retired(item: LaborPriceItem) -> bool:
        # SAME definition install_labor_line (billing_lanes) uses to refuse a
        # row: gone/inactive, OR retired by effective_to (stays active=True!),
        # OR a $0 row. Anything short of this would let a superseded row read
        # as "prices still check out" — the exact false reassurance to avoid.
        if item is None or not item.active:
            return True
        eff_to = getattr(item, "effective_to", None)
        if eff_to is not None and eff_to < _dt.date.today():
            return True
        return _to_float(item.flat_price) <= 0

    drift: list[dict] = []
    lines = getattr(estimate, "lines", None) or []
    for line in lines:
        item_id = getattr(line, "labor_price_item_id", None)
        if not item_id:
            continue  # non-labor / free-form — no reliable current price to compare
        current = db.get(LaborPriceItem, item_id)
        quoted = _to_float(line.unit_price)
        qty = int(getattr(line, "quantity", 1) or 1)
        if _is_retired(current):
            drift.append({
                "line_id": str(line.id),
                "description": line.description,
                "quantity": qty,
                "quoted_unit_price": quoted,
                "current_price": None,
                "delta": None,
                "line_delta": None,
                "reason": "matrix row no longer available",
            })
            continue
        current_price = _to_float(current.flat_price)
        if abs(current_price - quoted) >= 0.005:  # penny tolerance
            delta = round(current_price - quoted, 2)
            drift.append({
                "line_id": str(line.id),
                "description": line.description,
                "quantity": qty,
                "quoted_unit_price": quoted,
                "current_price": current_price,
                "delta": delta,
                "line_delta": round(delta * qty, 2),  # per-unit × qty = line impact
                "reason": "matrix price changed",
            })
    return drift


@router.post("/{estimate_id}/reopen", response_model=None)
def reopen_estimate(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Reopen a closed estimate so it can be re-sent — 'sometimes people come
    back months later and the numbers are still good' (Doug §15).

    Only expired / declined / rejected estimates reopen (an active draft/sent
    one has nothing to reopen; accepted is terminal). Goes back to 'draft' and
    clears the closed-state stamps (declined_at/reason, valid_until) so the
    next send re-stamps a fresh expiry window. Returns the estimate plus a
    price_drift report so the office can re-check the numbers before re-sending.
    """
    estimate = _get_estimate_or_404(estimate_id, db, include_lines=True)
    if estimate.status not in {"expired", "declined", "rejected"}:
        raise HTTPException(
            status_code=409,
            detail=f"only an expired, declined, or rejected estimate can be reopened (is '{estimate.status}')",
        )
    drift = _compute_price_drift(estimate, db)

    estimate.status = "draft"
    estimate.declined_at = None
    estimate.declined_reason = None
    estimate.valid_until = None  # a fresh window is stamped on the next send
    estimate.updated_at = utcnow()
    db.commit()
    db.refresh(estimate)
    log_audit_event_sync(
        db=db,
        tenant_id=None,
        user_id=_actor_id(_),
        action="estimate_reopened",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"drifted_line_count": len(drift)},
    )
    db.commit()
    payload = _serialize_estimate(estimate, include_lines=False)
    payload["price_drift"] = drift
    return payload


# ---------------------------------------------------------------------------
# Convert estimate → job (closes EstimatesView + EstimateView Vue gap)
# ---------------------------------------------------------------------------
# Creates a new Job linked to this estimate via estimate.job_id and returns
# both ids. Requires the estimate to be in 'accepted' status, to have a
# customer, and to not already be linked to a job — there is no force/
# override parameter. Audit logged on both sides.


@router.post("/{estimate_id}/convert-to-job", response_model=None)
def convert_estimate_to_job(
    estimate_id: UUID,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Manual convert — a recovery path when the auto-convert on accept
    skipped (no_customer) and the customer is now attached, or for a
    pre-2026-05-13 accepted estimate that never had its job created.

    NOT idempotent: an estimate that already has a job_id gets a 409
    rather than the existing job id back. Callers retrying a convert
    must treat 409 as "already done", not as a failure.
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
    """Full clone of an estimate: same customer / jobsite / description /
    notes / tax / lines. Both the job name (label) AND the estimate_number get
    an incrementing "-N" suffix so the copy reads as an option variant of the
    same base — EST-000042 -> EST-000042-1, -2, -3 (see
    _next_duplicate_estimate_number / _next_duplicate_label). Resets
    status=draft, mints a fresh public_token, clears
    sent/accepted/declined/signed state and job linkage.

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
        # Option variant of the same base — EST-000042-1, -2, -3 (Doug 2026-07-30).
        estimate_number=_next_duplicate_estimate_number(db, source.estimate_number),
        label=_next_duplicate_label(db, source.label),
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
        from gdx_dispatch.modules.proposals.models import ProposalTierLine
        from gdx_dispatch.modules.proposals.service import tier_contract_lines

        for tier in source_tiers:
            new_tier = ProposalTier(
                estimate_id=new_estimate.id,
                tier_name=tier.tier_name,
                description=tier.description,
                total_price=tier.total_price,
                includes_parts=tier.includes_parts,
                warranty_months=tier.warranty_months,
                stripe_payment_link=None,  # payment links are per-estimate; mint new on demand
                display_order=tier.display_order,
            )
            db.add(new_tier)
            db.flush()
            # Line-built tiers (2026-08-14) clone WITH their lines — a copy
            # that kept the synced price but dropped the lines would freeze a
            # read-only price nobody can edit.
            for tl in tier_contract_lines(db, tier):
                db.add(ProposalTierLine(
                    tier_id=new_tier.id,
                    description=tl.description,
                    category=tl.category,
                    quantity=tl.quantity,
                    unit_price=tl.unit_price,
                    line_total=tl.line_total,
                    sort_order=tl.sort_order,
                    company_id=tenant_id,
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
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Mark estimates as expired if past their valid_until date."""
    now = utcnow()
    stale = (
        db.query(Estimate)
        .filter(
            # "rejected" (email bounced) ages out like sent — see the
            # nightly task's filter for why.
            Estimate.status.in_(("sent", "draft", "rejected")),
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
            _audit_user = resolve_audit_actor(_audit_user_obj, _audit_req)
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
        # The customer-facing label — the door size ("16' × 7'"). Rides the
        # public proposal page and captions the PDF photo grid.
        "title": doc.title,
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
    # Optional customer-facing label ("16' × 7'"); the capture flow sends the
    # door size it already knows so nobody has to type it after the fact.
    title: str | None = Form(default=None),
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    estimate = _get_estimate_or_404(estimate_id, db)
    ct = (file.content_type or "").strip().lower()
    if ct not in ESTIMATE_ATTACHMENT_ALLOWED_MIME:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {ct}")
    # Pre-read ceiling (2026-08-26). The len() check below still stands, but by
    # then the whole body is already in memory; this refuses it first. Same cap.
    assert_upload_within_limit(file, ESTIMATE_ATTACHMENT_MAX_BYTES)
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
        title=(title or "").strip()[:255] or None,
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


class AttachmentPatch(BaseModel):
    # None = leave alone (exclude_unset guards); "" = clear the label.
    title: str | None = Field(default=None, max_length=255)


@router.patch("/{estimate_id}/attachments/{document_id}", response_model=None)
def patch_estimate_attachment(
    estimate_id: UUID,
    document_id: UUID,
    payload: AttachmentPatch,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Label a photo after the fact — the door size ("16' × 7'") on pictures
    that arrived without one (manual uploads, older captures)."""
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
    data = payload.model_dump(exclude_unset=True)
    if "title" in data:
        doc.title = (data["title"] or "").strip()[:255] or None
    db.commit()
    db.refresh(doc)
    try:
        tenant_id = str((getattr(request.state, "tenant", {}) or {}).get("id") or "")
        log_audit_event_sync(
            db,
            tenant_id=tenant_id,
            user_id=str((user or {}).get("sub") or (user or {}).get("user_id") or "system"),
            action="estimate_attachment_labeled",
            entity_type="estimate",
            entity_id=str(estimate_id),
            details={"document_id": str(document_id), "title": doc.title},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("estimate_attachment_label_audit_failed")
    return _serialize_attachment(doc)


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
