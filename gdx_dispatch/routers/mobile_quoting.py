"""Sprint tech_mobile Phase 2.1 — On-Truck Quoting.

Mobile-scoped wrappers around estimates + proposal_tiers so techs can
build a Good/Better/Best quote on the truck, hand the phone to the
customer, capture a signature, and hand back. Read-only on the pricing
side: techs CANNOT discount or override line prices (S2-A6); office
handles overrides via the existing /api/estimates router.

Endpoints (all under /api/mobile, gated on the "mobile" module):

    GET  /api/mobile/quotes/services           — service preset catalog
    GET  /api/mobile/quotes/decline-reasons    — tenant-configured reason list
    POST /api/mobile/jobs/{job_id}/quote       — build a quote from preset
                                                  or custom lines
    GET  /api/mobile/jobs/{job_id}/quote       — list quotes for this job
    POST /api/mobile/quotes/{estimate_id}/accept   — customer-on-truck accept
                                                      with signature
    POST /api/mobile/quotes/{estimate_id}/decline  — decline with reason
    GET  /api/mobile/quotes/{estimate_id}      — quote detail (with tiers)
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID as _UUID
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as _text
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.service_presets import (
    DEFAULT_SERVICE_PRESETS,
    find_default_preset,
    list_default_services,
)
from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except Exception:
    log.exception("mobile_quoting_auth_import_failed_using_fallback")

    async def get_current_user() -> dict[str, Any]:
        return {}


router = APIRouter(
    prefix="/api/mobile",
    tags=["mobile-quoting"],
    dependencies=[Depends(require_module("mobile"))],
)


def _jr(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


def _tenant_id(request: Request) -> str:
    state = getattr(request, "state", None)
    tenant = getattr(state, "tenant", None) or {}
    return str(tenant.get("id") or getattr(state, "tenant_id", "") or "")


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "")


def _money(v: Decimal | float | str) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


def _job_belongs_to_tech(db: Session, job_id: str, user_id: str, tenant_id: str) -> bool:
    """True if this job is assigned to the calling user (or any of their tech rows).

    Mirrors the gate used in mobile.py — connection-isolated tenant DB,
    so we don't need an explicit tenant_id filter, but we do need to
    confirm the job is assigned to *this* user.
    """
    if not job_id or not user_id:
        return False
    row = db.execute(
        _text(
            """
            SELECT 1 FROM jobs
            WHERE id = :jid AND deleted_at IS NULL AND assigned_to = :uid
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": user_id},
    ).scalar()
    if row:
        return True
    # Multi-tech (Phase 1.4) — check job_assignments via Technician.user_id
    row = db.execute(
        _text(
            """
            SELECT 1 FROM job_assignments ja
            JOIN technicians t ON t.id = ja.tech_id
            WHERE ja.job_id = :jid AND CAST(t.user_id AS TEXT) = :uid AND t.active IS NOT FALSE
            LIMIT 1
            """
        ),
        {"jid": job_id, "uid": user_id},
    ).scalar()
    return bool(row)


def _serialize_quote(estimate: Estimate, *, include_tiers: bool = True, include_lines: bool = False, db: Session | None = None) -> dict[str, Any]:
    # Tax-inclusive total + breakdown so the tech (and the accept screen) sees
    # the same number the customer sees on the PDF / acceptance email.
    _t = compute_estimate_totals(estimate, db)
    out: dict[str, Any] = {
        "id": str(estimate.id),
        "estimate_number": estimate.estimate_number,
        "job_id": str(estimate.job_id) if estimate.job_id else None,
        "customer_id": str(estimate.customer_id) if estimate.customer_id else None,
        "label": estimate.label,
        "description": getattr(estimate, "description", None),
        "notes": estimate.notes,
        "status": estimate.status,
        "subtotal": _t["subtotal"],
        "discount": _t["discount"],
        "tax": _t["tax"],
        "tax_rate_pct": _t["tax_rate_pct"],
        "total": _t["total"],
        "valid_until": estimate.valid_until.isoformat() if estimate.valid_until else None,
        "sent_at": estimate.sent_at.isoformat() if estimate.sent_at else None,
        "accepted_at": estimate.accepted_at.isoformat() if estimate.accepted_at else None,
        "declined_at": estimate.declined_at.isoformat() if estimate.declined_at else None,
        "declined_reason": estimate.declined_reason,
        "accepted_tier_id": str(estimate.accepted_tier_id) if estimate.accepted_tier_id else None,
        "signed_by": getattr(estimate, "signed_by", None),
        "signed_at": estimate.signed_at.isoformat() if getattr(estimate, "signed_at", None) else None,
        "has_signature": bool(getattr(estimate, "signature_data", None)),
        "proposal_mode": bool(estimate.proposal_mode),
    }
    if include_tiers and db is not None:
        tiers = db.execute(
            select(ProposalTier)
            .where(ProposalTier.estimate_id == estimate.id)
            .order_by(ProposalTier.display_order.asc())
        ).scalars().all()
        out["tiers"] = [
            {
                "id": str(t.id),
                "tier_name": t.tier_name,
                "description": t.description,
                "total_price": float(t.total_price or 0),
                "warranty_months": t.warranty_months,
                "includes_parts": bool(t.includes_parts),
                "display_order": t.display_order,
            }
            for t in tiers
        ]
    if include_lines and db is not None:
        lines = db.execute(
            select(EstimateLine)
            .where(EstimateLine.estimate_id == estimate.id)
            .order_by(EstimateLine.sort_order.asc())
        ).scalars().all()
        out["lines"] = [
            {
                "id": str(ln.id),
                "description": ln.description,
                "quantity": ln.quantity,
                "unit_price": float(ln.unit_price or 0),
                "line_total": float(ln.line_total or 0),
                "sort_order": ln.sort_order,
            }
            for ln in lines
        ]
    return out


def _resolved_services(db: Session, request: Request) -> list[dict[str, Any]]:
    """Return the tenant's effective service-preset catalog.

    Tenants may override ``tech_mobile.service_presets_override`` with a
    list of JSON strings, each one a service block. Empty list → use
    platform defaults.
    """
    override_raw = get_tenant_mobile_setting(
        db, "tech_mobile.service_presets_override", default=[], request=request,
    )
    if isinstance(override_raw, list) and override_raw:
        import json
        out = []
        for entry in override_raw:
            try:
                if isinstance(entry, dict):
                    out.append(entry)
                elif isinstance(entry, str):
                    out.append(json.loads(entry))
            except Exception:
                log.exception("service_preset_override_parse_failed entry=%r", entry)
        if out:
            return out
    return list_default_services()


def _next_estimate_number(db: Session) -> str:
    """Generate the next estimate number. Mirrors estimates._next_estimate_number."""
    row = db.execute(
        _text("SELECT estimate_number FROM estimates ORDER BY created_at DESC LIMIT 1")
    ).first()
    if row and row[0] and row[0].startswith("EST-"):
        try:
            n = int(row[0].split("-", 1)[1]) + 1
            return f"EST-{n:06d}"
        except (ValueError, AttributeError):
            pass
    return f"EST-{datetime.now(UTC):%y%m}{secrets.token_hex(2).upper()}"


# ---------------------------------------------------------------------------
# Pydantic input shapes
# ---------------------------------------------------------------------------


class QuoteLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    quantity: int = Field(default=1, gt=0, le=9999)
    unit_price: float = Field(ge=0, le=999999.99)


class QuoteTierIn(BaseModel):
    tier_name: str = Field(pattern="^(good|better|best)$")
    label: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    line_items: list[QuoteLineIn] = Field(default_factory=list)
    warranty_months: int = Field(default=0, ge=0, le=9999)
    includes_parts: bool = False


class BuildQuoteIn(BaseModel):
    """Body for POST /api/mobile/jobs/{job_id}/quote.

    Either reference a preset (service + tier_ids list) OR pass custom
    tiers. If both are present, custom wins.
    """

    service: str | None = Field(default=None, max_length=80)
    label: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    customer_id: str | None = Field(default=None)
    # Custom tiers (when not using a preset)
    tiers: list[QuoteTierIn] = Field(default_factory=list)
    # When using a preset: which tier ids from that service to include.
    # Empty / None → include all 3 (good/better/best).
    preset_tier_ids: list[str] = Field(default_factory=list)


class AcceptQuoteIn(BaseModel):
    chosen_tier_id: str = Field(min_length=1)
    signature_data: str | None = Field(default=None, max_length=1_400_000)
    signed_by: str | None = Field(default=None, max_length=200)
    # Deposit at acceptance (2026-07-23, opt-IN on mobile). Unlike the
    # portal — where estimates are the door-order flow — mobile quotes are
    # often same-day service repairs where a 50% "due now" demand is
    # nonsense (implementation-audit catch). The tech must flip the toggle:
    # collect_deposit=True → tenant deposit percent (estimate_deposit_pct,
    # the PDF's "% Down") × accepted tier total; an explicit positive
    # deposit_amount overrides the percent. Both absent → no deposit.
    deposit_amount: float | None = Field(default=None, ge=0, le=1_000_000)
    collect_deposit: bool = False


class DeclineQuoteIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# GET /api/mobile/quotes/services
# ---------------------------------------------------------------------------


@router.get("/quotes/services", response_model=None)
def list_quote_services(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Return the service-preset catalog (Good/Better/Best per service)."""
    return _jr({"services": _resolved_services(db, request)})


# ---------------------------------------------------------------------------
# GET /api/mobile/quotes/decline-reasons
# ---------------------------------------------------------------------------


@router.get("/quotes/decline-reasons", response_model=None)
def list_decline_reasons(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    reasons = get_tenant_mobile_setting(
        db, "tech_mobile.quote_decline_reasons", request=request,
    )
    if not isinstance(reasons, list):
        reasons = []
    return _jr({"reasons": [str(r) for r in reasons]})


# ---------------------------------------------------------------------------
# POST /api/mobile/jobs/{job_id}/quote
# ---------------------------------------------------------------------------


@router.post("/jobs/{job_id}/quote", response_model=None, status_code=201)
def build_quote(
    job_id: str,
    payload: BuildQuoteIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Create an Estimate (proposal_mode=True) with up to three tiers.

    - If ``payload.tiers`` is non-empty, use those (custom).
    - Else if ``payload.service`` is set, build from the platform / tenant
      preset catalog.
    - Else 400.

    Pricing is read-only on mobile (S2-A6) — line unit_prices flow as-is
    from the preset or the custom payload. The estimates router still
    governs office-side overrides.
    """
    tenant_id = _tenant_id(request)
    user = current_user or {}
    user_id = _user_id(user)

    if not _job_belongs_to_tech(db, job_id, user_id, tenant_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)

    # Resolve tiers — either from preset or custom payload.
    tiers_to_create: list[dict[str, Any]] = []
    if payload.tiers:
        for t in payload.tiers:
            tiers_to_create.append(
                {
                    "tier_name": t.tier_name,
                    "label": t.label or t.tier_name.title(),
                    "description": t.description or "",
                    "line_items": [li.model_dump() for li in t.line_items],
                    "warranty_months": t.warranty_months,
                    "includes_parts": t.includes_parts,
                }
            )
    elif payload.service:
        services = _resolved_services(db, request)
        match = next((s for s in services if s.get("service") == payload.service), None)
        if not match:
            return _jr({"detail": f"unknown service: {payload.service}"}, 400)
        wanted = set(payload.preset_tier_ids) if payload.preset_tier_ids else None
        for tier in match.get("tiers", []):
            if wanted and tier.get("id") not in wanted:
                continue
            tiers_to_create.append(
                {
                    "tier_name": tier.get("id", "good"),
                    "label": tier.get("label", ""),
                    "description": tier.get("description", ""),
                    "line_items": tier.get("line_items", []),
                    "warranty_months": int(tier.get("warranty_months") or 0),
                    "includes_parts": bool(tier.get("includes_parts")),
                }
            )
    else:
        return _jr({"detail": "either 'service' or 'tiers' is required"}, 400)

    if not tiers_to_create:
        return _jr({"detail": "no tiers to create"}, 400)

    # Resolve customer_id from job if not provided.
    customer_id = payload.customer_id
    if not customer_id:
        row = db.execute(
            _text("SELECT customer_id FROM jobs WHERE id = :jid"),
            {"jid": job_id},
        ).first()
        if row and row[0]:
            customer_id = str(row[0])

    validity_days = int(get_tenant_mobile_setting(
        db, "tech_mobile.estimate_validity_days", request=request,
    ) or 30)
    now = datetime.now(UTC)
    valid_until = now + timedelta(days=validity_days)

    estimate = Estimate(
        id=uuid4(),
        job_id=_UUID(job_id),
        customer_id=_UUID(customer_id) if customer_id else None,
        estimate_number=_next_estimate_number(db),
        label=payload.label or (payload.service or "Quote"),
        notes=payload.notes,
        proposal_mode=True,
        status="draft",
        valid_until=valid_until,
        company_id=str(tenant_id),
        public_token=secrets.token_urlsafe(48)[:64],
        created_at=now,
    )
    db.add(estimate)
    db.flush()

    # Create one ProposalTier per tier_to_create AND the underlying lines.
    # Lines live on the Estimate; the ProposalTier stores the rolled-up
    # total + display metadata. We track which lines belong to which tier
    # via sort_order ranges (tier 1 = 100..199, tier 2 = 200..299, etc.).
    overall_total = Decimal("0")
    sort_base = 100
    for idx, tier_spec in enumerate(tiers_to_create, start=1):
        tier_total = Decimal("0")
        for li_idx, li in enumerate(tier_spec["line_items"], start=1):
            unit = _money(li.get("unit_price", 0))
            qty = int(li.get("quantity", 1))
            line_total = _money(unit * qty)
            tier_total += line_total
            db.add(
                EstimateLine(
                    id=uuid4(),
                    estimate_id=estimate.id,
                    description=str(li.get("description", "")),
                    quantity=qty,
                    unit_price=unit,
                    line_total=line_total,
                    sort_order=sort_base + li_idx,
                    company_id=str(tenant_id),
                )
            )
        db.add(
            ProposalTier(
                id=uuid4(),
                estimate_id=estimate.id,
                tier_name=tier_spec["tier_name"],
                description=(tier_spec["label"] + " — " + tier_spec["description"]).strip(" —"),
                total_price=_money(tier_total),
                includes_parts=bool(tier_spec["includes_parts"]),
                warranty_months=int(tier_spec["warranty_months"]),
                display_order=idx,
            )
        )
        sort_base += 100
        overall_total = max(overall_total, tier_total)

    # Estimate.total = the highest tier (so list views show meaningful
    # number); the customer signs against the chosen tier's total at
    # accept time.
    estimate.total = _money(overall_total)
    estimate.status = "sent"
    estimate.sent_at = now
    db.commit()
    db.refresh(estimate)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_quote_built",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={"job_id": job_id, "service": payload.service, "tier_count": len(tiers_to_create)},
        request=request,
    )
    db.commit()
    return _jr(_serialize_quote(estimate, db=db, include_lines=True), 201)


# ---------------------------------------------------------------------------
# GET /api/mobile/jobs/{job_id}/quote
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/quote", response_model=None)
def list_job_quotes(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if not _job_belongs_to_tech(db, job_id, user_id, tenant_id):
        return _jr({"detail": "job not found or not assigned to you"}, 404)
    estimates = db.execute(
        select(Estimate)
        .where(Estimate.job_id == _UUID(job_id), Estimate.deleted_at.is_(None))
        .order_by(Estimate.created_at.desc())
    ).scalars().all()
    return _jr({"quotes": [_serialize_quote(e, db=db) for e in estimates]})


# ---------------------------------------------------------------------------
# GET /api/mobile/quotes/{estimate_id}
# ---------------------------------------------------------------------------


@router.get("/quotes/{estimate_id}", response_model=None)
def get_quote(
    estimate_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    estimate = db.execute(
        select(Estimate).where(Estimate.id == _UUID(estimate_id), Estimate.deleted_at.is_(None))
    ).scalar_one_or_none()
    if estimate is None:
        return _jr({"detail": "quote not found"}, 404)
    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if estimate.job_id and not _job_belongs_to_tech(db, str(estimate.job_id), user_id, tenant_id):
        return _jr({"detail": "quote not on a job assigned to you"}, 403)
    return _jr(_serialize_quote(estimate, db=db, include_lines=True))


# ---------------------------------------------------------------------------
# POST /api/mobile/quotes/{estimate_id}/accept
# ---------------------------------------------------------------------------


@router.post("/quotes/{estimate_id}/accept", response_model=None)
def accept_quote(
    estimate_id: str,
    payload: AcceptQuoteIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    estimate = db.execute(
        select(Estimate).where(Estimate.id == _UUID(estimate_id), Estimate.deleted_at.is_(None))
    ).scalar_one_or_none()
    if estimate is None:
        return _jr({"detail": "quote not found"}, 404)
    if estimate.status == "accepted":
        return _jr({"detail": "already accepted"}, 409)
    if estimate.status == "declined":
        return _jr({"detail": "cannot accept a declined quote"}, 409)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if estimate.job_id and not _job_belongs_to_tech(db, str(estimate.job_id), user_id, tenant_id):
        return _jr({"detail": "quote not on a job assigned to you"}, 403)

    # Validate chosen_tier_id belongs to this estimate.
    try:
        chosen_uuid = _UUID(payload.chosen_tier_id)
    except (ValueError, AttributeError):
        return _jr({"detail": "invalid chosen_tier_id"}, 400)

    tier = db.execute(
        select(ProposalTier).where(
            ProposalTier.id == chosen_uuid,
            ProposalTier.estimate_id == estimate.id,
        )
    ).scalar_one_or_none()
    if tier is None:
        return _jr({"detail": "chosen tier not found on this quote"}, 404)

    # Signature gating per tenant setting.
    sig_required = get_tenant_mobile_setting(
        db, "tech_mobile.signature_required_quote", request=request,
    )
    sig = (payload.signature_data or "").strip()
    if sig_required == "required" and not sig:
        return _jr({"detail": "Signature is required to accept the quote"}, 400)
    if sig_required == "off":
        sig = ""

    now = datetime.now(UTC)
    estimate.status = "accepted"
    estimate.accepted_at = now
    estimate.accepted_tier_id = chosen_uuid
    estimate.total = _money(tier.total_price)
    if hasattr(estimate, "signature_data"):
        if sig:
            estimate.signature_data = sig
            estimate.signed_by = (payload.signed_by or "").strip() or None
            estimate.signed_at = now
    estimate.updated_at = now
    db.commit()
    db.refresh(estimate)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_quote_accepted",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={
            "chosen_tier_id": str(chosen_uuid),
            "tier_name": tier.tier_name,
            "total": compute_estimate_totals(estimate, db)["total"],
            "has_signature": bool(sig),
        },
        request=request,
    )
    db.commit()

    # Deposit at acceptance (2026-07-23) — opt-IN on mobile (see
    # AcceptQuoteIn): the tech flips the Collect-deposit toggle (auto tenant
    # percent) or sends an explicit amount. A deposit failure never
    # un-accepts the quote.
    deposit_payload: dict[str, Any] | None = None
    deposit_skipped: str | None = None
    try:
        from gdx_dispatch.modules.deposits import (
            DepositError,
            create_deposit_invoice,
            deposit_summary,
        )
        from gdx_dispatch.modules.estimates_features import get_features

        amount = payload.deposit_amount
        if amount is None and payload.collect_deposit:
            pct = max(0, min(100, int(get_features(tenant_id).deposit_pct or 0)))
            accepted_total = float(_money(tier.total_price) or 0)
            amount = round(accepted_total * pct / 100.0, 2) if pct > 0 else 0.0
        if amount and amount > 0:
            try:
                dep_inv = create_deposit_invoice(
                    db,
                    estimate=estimate,
                    amount=float(amount),
                    tenant_id=tenant_id,
                    actor=user_id,
                    source="mobile_accept",
                )
                deposit_payload = deposit_summary(dep_inv)
            except DepositError as exc:
                deposit_skipped = str(exc)
    except Exception:
        logging.getLogger(__name__).exception(
            "mobile_accept_deposit_failed estimate=%s", estimate.id
        )
        deposit_skipped = "deposit invoice creation failed — office can bill it"

    resp = _serialize_quote(estimate, db=db, include_lines=True)
    if deposit_payload:
        resp["deposit"] = deposit_payload
    if deposit_skipped:
        resp["deposit_skipped"] = deposit_skipped
    return _jr(resp)


# ---------------------------------------------------------------------------
# POST /api/mobile/quotes/{estimate_id}/decline
# ---------------------------------------------------------------------------


@router.post("/quotes/{estimate_id}/decline", response_model=None)
def decline_quote(
    estimate_id: str,
    payload: DeclineQuoteIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> JSONResponse:
    estimate = db.execute(
        select(Estimate).where(Estimate.id == _UUID(estimate_id), Estimate.deleted_at.is_(None))
    ).scalar_one_or_none()
    if estimate is None:
        return _jr({"detail": "quote not found"}, 404)
    if estimate.status == "declined":
        return _jr({"detail": "already declined"}, 409)
    if estimate.status == "accepted":
        return _jr({"detail": "cannot decline an accepted quote"}, 409)

    user = current_user or {}
    user_id = _user_id(user)
    tenant_id = _tenant_id(request)
    if estimate.job_id and not _job_belongs_to_tech(db, str(estimate.job_id), user_id, tenant_id):
        return _jr({"detail": "quote not on a job assigned to you"}, 403)

    # Validate reason against tenant taxonomy (informational — accept any
    # string but log mismatch so dispatch sees if a tech typed free-form).
    reasons = get_tenant_mobile_setting(
        db, "tech_mobile.quote_decline_reasons", request=request,
    )
    if not isinstance(reasons, list):
        reasons = []
    is_taxonomy_match = payload.reason in reasons

    now = datetime.now(UTC)
    estimate.status = "declined"
    estimate.declined_at = now
    estimate.declined_reason = (
        payload.reason if not payload.notes
        else f"{payload.reason} — {payload.notes.strip()}"
    )
    estimate.updated_at = now
    db.commit()
    db.refresh(estimate)

    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=user_id,
        action="mobile_quote_declined",
        entity_type="estimate",
        entity_id=str(estimate.id),
        details={
            "reason": payload.reason,
            "free_form_notes": bool(payload.notes),
            "in_taxonomy": is_taxonomy_match,
        },
        request=request,
    )
    db.commit()
    return _jr(_serialize_quote(estimate, db=db))
