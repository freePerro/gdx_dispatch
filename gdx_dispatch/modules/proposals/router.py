from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor, utcnow
from gdx_dispatch.core.customer_views import record_customer_view
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.modules.deposits.service import (
    DepositError,
    create_deposit_invoice,
    deposit_summary,
    find_deposit_invoice_for_estimate,
)
from gdx_dispatch.modules.estimates_features import effective_hide_line_prices, get_features
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier
from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
from gdx_dispatch.modules.proposals.service import (
    accept_tier,
    add_proposal_tier,
    add_tier_line,
    delete_proposal_tier,
    delete_tier_line,
    tier_contract_lines,
    tier_contract_subtotal,
    update_proposal_tier,
    update_tier_line,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["proposals"])


def _require_dispatch(user: dict = Depends(get_current_user)) -> dict:
    """Accepting a proposal tier is a financial commitment — dispatch/admin only."""
    if not is_dispatch_manager(user):
        raise HTTPException(status_code=403, detail="dispatcher or admin role required")
    return user

# tier_name is the DB Enum("good","better","best"). Typed as a bare `str` it
# reached the driver unvalidated and any other value came back as a 500 from
# psycopg rather than a 422 — Literal moves the rejection to the request edge.
# The bounds mirror the deleted /api/proposals router's Field limits.
TierName = Literal["good", "better", "best"]


class TierIn(BaseModel): tier_name: TierName; description: str | None = Field(default=None, max_length=5000); total_price: float = Field(default=0, ge=0, le=10_000_000); warranty_months: int = Field(default=0, ge=0, le=600)  # noqa: E701,E702
class TierPatch(BaseModel): tier_name: TierName | None = None; description: str | None = Field(default=None, max_length=5000); total_price: float | None = Field(default=None, ge=0, le=10_000_000); warranty_months: int | None = Field(default=None, ge=0, le=600); includes_parts: bool | None = None  # noqa: E701,E702
class AcceptIn(BaseModel): tier_id: UUID  # noqa: E701,E702
class TierLineIn(BaseModel): description: str = Field(min_length=1, max_length=5000); quantity: int = Field(default=1, ge=1, le=10_000); unit_price: float = Field(default=0, ge=0, le=10_000_000); category: str | None = Field(default=None, max_length=80)  # noqa: E701,E702
class TierLinePatch(BaseModel): description: str | None = Field(default=None, min_length=1, max_length=5000); quantity: int | None = Field(default=None, ge=1, le=10_000); unit_price: float | None = Field(default=None, ge=0, le=10_000_000); category: str | None = Field(default=None, max_length=80)  # noqa: E701,E702


def _tier_line_dict(ln) -> dict[str, object]:
    return {
        "id": str(ln.id),
        "description": ln.description,
        "category": ln.category,
        "quantity": int(ln.quantity or 1),
        "unit_price": float(ln.unit_price or 0),
        "line_total": float(ln.line_total or 0),
        "sort_order": ln.sort_order,
    }


def _tier_dict(db: Session, t: ProposalTier) -> dict[str, object]:
    """Staff wire shape for a tier + its lines. Explicit dict (not the ORM
    row) so the new `lines` relationship serializes deterministically."""
    return {
        "id": str(t.id),
        "estimate_id": str(t.estimate_id),
        "tier_name": t.tier_name,
        "description": t.description,
        "total_price": float(t.total_price or 0),
        "includes_parts": bool(t.includes_parts),
        "warranty_months": t.warranty_months,
        "stripe_payment_link": t.stripe_payment_link,
        "display_order": t.display_order,
        "lines": [_tier_line_dict(ln) for ln in tier_contract_lines(db, t)],
    }

@router.get("/estimates/{estimate_id}/proposal", response_model=None)
def get_proposal(estimate_id: UUID, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> list[dict[str, object]]:
    if not db.execute(select(Estimate.id).where(Estimate.id == estimate_id)).scalar_one_or_none(): raise HTTPException(status_code=404, detail="Estimate not found")  # noqa: E701,E702
    tiers = db.execute(select(ProposalTier).where(ProposalTier.estimate_id == estimate_id).order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())).scalars().all()
    return [_tier_dict(db, t) for t in tiers]

@router.post("/estimates/{estimate_id}/proposal-tiers", response_model=None)
def post_proposal_tier(estimate_id: UUID, payload: TierIn, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    return _tier_dict(db, add_proposal_tier(estimate_id, payload.tier_name, payload.description, payload.total_price, payload.warranty_months, db, actor=resolve_audit_actor(current_user)))

@router.patch("/estimates/{estimate_id}/proposal-tiers/{tier_id}", response_model=None)
def patch_proposal_tier(estimate_id: UUID, tier_id: UUID, payload: TierPatch, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    return _tier_dict(db, update_proposal_tier(estimate_id, tier_id, payload.model_dump(exclude_unset=True), db, actor=resolve_audit_actor(current_user)))

# ── tier line items (2026-08-14: tiers built like estimates) ────────────────

@router.post("/estimates/{estimate_id}/proposal-tiers/{tier_id}/lines", response_model=None, status_code=201)
def post_tier_line(estimate_id: UUID, tier_id: UUID, payload: TierLineIn, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    row = add_tier_line(
        estimate_id, tier_id,
        description=payload.description, quantity=payload.quantity,
        unit_price=payload.unit_price, category=payload.category,
        db=db, actor=resolve_audit_actor(current_user),
    )
    return _tier_line_dict(row)

@router.patch("/estimates/{estimate_id}/proposal-tiers/{tier_id}/lines/{line_id}", response_model=None)
def patch_tier_line(estimate_id: UUID, tier_id: UUID, line_id: UUID, payload: TierLinePatch, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, object]:
    row = update_tier_line(estimate_id, tier_id, line_id, payload.model_dump(exclude_unset=True), db, actor=resolve_audit_actor(current_user))
    return _tier_line_dict(row)

@router.delete("/estimates/{estimate_id}/proposal-tiers/{tier_id}/lines/{line_id}", status_code=204)
def del_tier_line(estimate_id: UUID, tier_id: UUID, line_id: UUID, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    delete_tier_line(estimate_id, tier_id, line_id, db, actor=resolve_audit_actor(current_user))
    return Response(status_code=204)

@router.delete("/estimates/{estimate_id}/proposal-tiers/{tier_id}", status_code=204)
def del_proposal_tier(estimate_id: UUID, tier_id: UUID, _: None = Depends(require_module("proposals")), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    delete_proposal_tier(estimate_id, tier_id, db, actor=resolve_audit_actor(current_user))
    return Response(status_code=204)

@router.post("/estimates/{estimate_id}/proposal/accept", response_model=None)
def post_accept_tier(estimate_id: UUID, payload: AcceptIn, _: None = Depends(require_module("proposals")), current_user: dict = Depends(_require_dispatch), db: Session = Depends(get_db)) -> Estimate:
    return accept_tier(estimate_id, payload.tier_id, db, actor=resolve_audit_actor(current_user))

# ── the public customer surface ──────────────────────────────────────────────
# Unauthenticated, addressed by Estimate.public_token (64-char urlsafe, unique,
# NOT NULL — possession of the token IS the authorization, same structural
# model as core/payments.py's /pay/{token}). Rate limiting is deliberately the
# general limiter only: the strict per-IP auth bucket is shared across paths
# (SPA assets proxy through the app, mail scanners prefetch links), so wiring
# it here would 429 the paying customer while spoofable first-hop XFF lets a
# scanner through anyway. The token's entropy is the defense.

_PUBLIC_LOOKUP_DETAIL = "Invalid proposal token"


def _get_public_estimate_or_404(token: str, db: Session, *, for_update: bool = False) -> Estimate:
    # deleted_at + sent_at are BOTH part of the lookup, not an afterthought.
    # public_token is minted at estimate CREATE, so without these every draft
    # and every soft-deleted estimate is a live public URL. The sibling public
    # route (core/payments.py) already filters deleted_at; sent_at is the
    # additional "we actually sent this to a customer" gate. A wrong token and
    # an unsent one return the same 404 so the response can't be used to probe
    # which is which.
    stmt = select(Estimate).where(
        Estimate.public_token == token,
        Estimate.deleted_at.is_(None),
        Estimate.sent_at.is_not(None),
    )
    if for_update:
        # Two concurrent accepts must serialize: both pass the status gate
        # otherwise and double-create the job. Real row lock on Postgres,
        # harmless no-op on SQLite (tests).
        stmt = stmt.with_for_update()
    est = db.execute(stmt).scalar_one_or_none()
    if not est:
        raise HTTPException(status_code=404, detail=_PUBLIC_LOOKUP_DETAIL)
    return est


def _public_tenant_id(request: Request | None, est: Estimate) -> str:
    tenant = getattr(getattr(request, "state", None), "tenant", {}) or {}
    return str(tenant.get("id") or est.company_id or "")


def _serialize_public_estimate(est: Estimate, db: Session, request: Request | None) -> dict[str, Any]:
    """Public wire shape. Explicit projection, NOT the ORM row: returning
    `est` whole once handed the caller `notes` — the office's internal notes
    on the job — plus company_id and a copy of the public_token itself.
    Anything a customer should not read stays off this dict by construction:
    add fields deliberately, never by spreading the model."""
    tenant_id = _public_tenant_id(request, est)
    # get_features defaults internally on read errors; the belt here exists so
    # a surprise CANNOT skip the line below — the per-estimate hide flag must
    # bind even when the tenant default is unavailable (failing open would
    # SHOW prices this estimate explicitly hides).
    default_hide = False
    deposit_pct = 0
    try:
        features = get_features(tenant_id)
        default_hide = features.hide_line_prices
        deposit_pct = max(0, min(100, int(features.deposit_pct or 0)))
    except Exception:
        log.exception("public_proposal_features_failed estimate=%s", est.id)
    hide_prices = effective_hide_line_prices(getattr(est, "hide_line_prices", None), default_hide)

    tiers = list(
        db.execute(
            select(ProposalTier)
            .where(ProposalTier.estimate_id == est.id)
            .order_by(ProposalTier.display_order.asc(), ProposalTier.id.asc())
        ).scalars().all()
    )
    lines = db.execute(
        select(EstimateLine)
        .where(EstimateLine.estimate_id == est.id)
        .order_by(EstimateLine.sort_order)
    ).scalars().all()

    # Who the estimate is FROM — the page header. /api/settings/branding is
    # auth-gated despite the module name, so the public payload carries the
    # two safe identity fields itself. Best-effort: a missing settings row
    # (fresh install, unit fixtures) degrades to a nameless header.
    company: dict[str, str] = {"name": "", "phone": ""}
    try:
        from gdx_dispatch.models.tenant_models import AppSettings

        settings_obj = db.execute(select(AppSettings).limit(1)).scalar_one_or_none()
        if settings_obj:
            company = {
                "name": settings_obj.company_name or "",
                "phone": settings_obj.phone or "",
            }
    except Exception:
        log.exception("public_proposal_company_lookup_failed")

    proposal_mode = bool(est.proposal_mode)
    body: dict[str, Any] = {
        "company": company,
        "estimate": {
            "estimate_number": est.estimate_number,
            "label": est.label,
            "jobsite_address": est.jobsite_address,
            "description": est.description,
            # "rejected" is internal (email bounced); to the customer the
            # estimate is simply open — and the word would read as "we
            # rejected YOU". Accept/decline both work from it server-side.
            "status": "sent" if est.status == "rejected" else est.status,
            "valid_until": est.valid_until.isoformat() if est.valid_until else None,
            "sent_at": est.sent_at.isoformat() if est.sent_at else None,
            "accepted_at": est.accepted_at.isoformat() if est.accepted_at else None,
            "declined_at": est.declined_at.isoformat() if est.declined_at else None,
            "accepted_tier_id": str(est.accepted_tier_id) if est.accepted_tier_id else None,
            "proposal_mode": proposal_mode,
            "hide_line_prices": hide_prices,
        },
        "tiers": [
            {
                "id": str(t.id),
                "tier_name": t.tier_name,
                "description": t.description,
                "total_price": float(t.total_price) if t.total_price is not None else 0.0,
                "includes_parts": bool(t.includes_parts),
                "warranty_months": t.warranty_months,
                "display_order": t.display_order,
                # Line-built tiers (2026-08-14): what's included in the
                # package, same public-safe projection + price stripping as
                # the estimate lines below.
                "lines": [
                    {
                        "description": tl.description,
                        "quantity": float(tl.quantity or 0),
                        **(
                            {}
                            if hide_prices
                            else {
                                "unit_price": float(tl.unit_price or 0),
                                "line_total": float(tl.line_total or 0),
                            }
                        ),
                    }
                    for tl in tier_contract_lines(db, t)
                ],
                # stripe_payment_link deliberately OMITTED: it is a payment
                # path that bypasses the deposit invoice, and it leaked here
                # before this page had any consumer.
            }
            for t in tiers
        ],
        "lines": [
            {
                "description": ln.description,
                "quantity": float(ln.quantity or 0),
                **(
                    {}
                    if hide_prices
                    else {
                        "unit_price": float(ln.unit_price or 0),
                        "line_total": float(ln.line_total or 0),
                    }
                ),
            }
            for ln in lines
        ],
        "deposit_pct": deposit_pct,
    }

    # Totals for line estimates — and for ACCEPTED proposals (2026-08-14):
    # once a tier is accepted, est.total IS the tier's contract subtotal and
    # the totals engine taxes off the tier's own lines, so the number is
    # finally true. An OPEN proposal still gets NO single total — `est.total`
    # can be the HIGHEST tier (mobile builder), and any one number would show
    # the best-tier price to a customer picking good.
    if not proposal_mode or est.accepted_tier_id is not None:
        try:
            t = compute_estimate_totals(est, db)
            body["totals"] = {
                "subtotal": t["subtotal"],
                "discount": t["discount"],
                "tax": t["tax"],
                "tax_rate_pct": t["tax_rate_pct"],
                "total": t["total"],
            }
        except Exception:
            log.exception("public_proposal_totals_failed estimate=%s", est.id)

    if est.status == "declined":
        body["estimate"]["declined_reason"] = est.declined_reason

    # Accept-then-abandon recovery (portal pattern): an accepted estimate with
    # a still-owed deposit re-surfaces its Pay link on every load.
    if est.status == "accepted":
        try:
            dep = find_deposit_invoice_for_estimate(db, est.id)
            if dep is not None and float(dep.balance_due or 0) > 0:
                body["deposit"] = deposit_summary(dep)
        except Exception:
            log.exception("public_proposal_deposit_lookup_failed estimate=%s", est.id)

    return body


@router.get("/proposals/{token}")
def get_public_proposal(token: str, request: Request = None, db: Session = Depends(get_db)) -> dict[str, object]:
    est = _get_public_estimate_or_404(token, db)
    # The customer opened the estimate we sent. Never blocks the response.
    # (Known limit: mail-scanner prefetch also lands here — "viewed" is a
    # hint, never evidence.)
    record_customer_view(
        db,
        action="estimate_viewed_by_customer",
        entity_type="estimate",
        entity_id=est.id,
        tenant_id=getattr(est, "company_id", None),
        request=request,
        sent_at=getattr(est, "sent_at", None),
        details={"estimate_number": est.estimate_number},
    )
    return _serialize_public_estimate(est, db, request)


class PublicAcceptIn(BaseModel):
    tier_id: UUID | None = None


class PublicDeclineIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


_PUBLIC_ACTOR = "customer:public-link"


def _client_ip(request: Request | None) -> str | None:
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


@router.post("/proposals/{token}/accept")
def public_proposal_accept(
    token: str,
    request: Request = None,
    payload: PublicAcceptIn | None = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    est = _get_public_estimate_or_404(token, db, for_update=True)

    # Re-click idempotency: an emailed link gets double-clicked routinely and
    # must not dead-end. Deliberate divergence from the portal's 409 — the
    # portal has an authenticated UI to absorb that; this page does not.
    if est.status == "accepted":
        body = _serialize_public_estimate(est, db, request)
        body["already_accepted"] = True
        return body
    # "rejected" (email bounced) stays acceptable: an accept is the strongest
    # possible proof the customer DID receive the estimate (portal rule).
    if est.status not in ("sent", "rejected"):
        raise HTTPException(status_code=409, detail="This estimate is no longer open for acceptance")

    # Tier resolution — NEW gated code on purpose: service.accept_tier has no
    # status gates and must never back a public endpoint.
    tier = None
    tier_id = payload.tier_id if payload else None
    if bool(est.proposal_mode):
        has_tiers = db.execute(
            select(ProposalTier.id).where(ProposalTier.estimate_id == est.id).limit(1)
        ).scalar_one_or_none() is not None
        if has_tiers:
            if tier_id is None:
                raise HTTPException(status_code=422, detail="Choose a package to accept this estimate")
            tier = db.execute(
                select(ProposalTier).where(
                    ProposalTier.id == tier_id, ProposalTier.estimate_id == est.id
                )
            ).scalar_one_or_none()
            if tier is None:
                raise HTTPException(status_code=404, detail="Proposal tier not found")
    if tier is None and tier_id is not None:
        raise HTTPException(status_code=422, detail="This estimate has no selectable packages")

    now = utcnow()
    est.status = "accepted"
    est.accepted_at = now
    est.updated_at = now
    if tier is not None:
        est.accepted_tier_id = tier.id
        # The accepted price lands in the billing data model (2026-08-14 fix,
        # mobile's convention): est.total drives totals/invoice/deposit-default.
        est.total = tier_contract_subtotal(db, tier)
    db.commit()
    db.refresh(est)

    tenant_id = _public_tenant_id(request, est)
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id=_PUBLIC_ACTOR,
        action="public_estimate_accepted",
        entity_type="estimate",
        entity_id=str(est.id),
        details={
            "estimate_number": est.estimate_number,
            "client_ip": _client_ip(request),
            "tier_id": str(tier.id) if tier else None,
            "tier_name": tier.tier_name if tier else None,
        },
    )
    db.commit()

    # Mirror the staff/portal accept flow (2026-05-13 directive: accept = job
    # created) so a public acceptance lands on the dispatch board too.
    if est.job_id is None:
        try:
            from gdx_dispatch.routers.estimates import _create_job_from_estimate

            _create_job_from_estimate(est, db, _PUBLIC_ACTOR)
        except Exception:
            log.exception("public_accept_auto_convert_failed estimate=%s", est.id)
            try:
                log_audit_event_sync(
                    db=db,
                    tenant_id=tenant_id,
                    user_id=_PUBLIC_ACTOR,
                    action="estimate_auto_convert_failed",
                    entity_type="estimate",
                    entity_id=str(est.id),
                    details={"source": "public"},
                )
                db.commit()
            except Exception:
                log.exception("public_accept_audit_failed")

    # One-motion deposit (portal pattern): acceptance creates the deposit
    # invoice and the response carries its public /pay URL. Failures never
    # un-accept the estimate.
    deposit_payload: dict[str, Any] | None = None
    try:
        db.refresh(est)
        pct = max(0, min(100, int(get_features(tenant_id).deposit_pct or 0)))
        if pct > 0 and est.customer_id is not None:
            if tier is not None:
                # The tier's contract subtotal (Σ its lines when line-built,
                # else its price) — the SAME number est.total was just set to,
                # and the cap must compare against it, not the tier-blind
                # estimate-lines total.
                base_amount = float(tier_contract_subtotal(db, tier))
                cap: float | None = base_amount
            else:
                base_amount = float(compute_estimate_totals(est, db)["total"] or 0)
                cap = None
            amount = round(base_amount * pct / 100.0, 2)
            if amount > 0:
                dep_inv = create_deposit_invoice(
                    db,
                    estimate=est,
                    amount=amount,
                    tenant_id=tenant_id,
                    actor=_PUBLIC_ACTOR,
                    source="public_accept",
                    cap_total=cap,
                )
                deposit_payload = deposit_summary(dep_inv)
                deposit_payload["pct"] = pct
    except DepositError as exc:
        log.warning("public_accept_deposit_skipped estimate=%s: %s", est.id, exc)
    except Exception:
        log.exception("public_accept_deposit_failed estimate=%s", est.id)

    body = _serialize_public_estimate(est, db, request)
    if deposit_payload:
        body["deposit"] = deposit_payload
    return body


@router.post("/proposals/{token}/decline")
def public_proposal_decline(
    token: str,
    request: Request = None,
    payload: PublicDeclineIn | None = None,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    est = _get_public_estimate_or_404(token, db, for_update=True)

    if est.status == "declined":
        body = _serialize_public_estimate(est, db, request)
        body["already_declined"] = True
        return body
    if est.status not in ("sent", "rejected"):
        raise HTTPException(status_code=409, detail="This estimate is no longer open for decline")

    # Reason optional on purpose: customer declines never require one (staff
    # declines do — that rule lives in the staff endpoint).
    reason = payload.reason.strip() if payload and payload.reason else None
    now = utcnow()
    est.status = "declined"
    est.declined_at = now
    est.declined_reason = reason
    est.updated_at = now
    db.commit()
    db.refresh(est)

    log_audit_event_sync(
        db=db,
        tenant_id=_public_tenant_id(request, est),
        user_id=_PUBLIC_ACTOR,
        action="public_estimate_declined",
        entity_type="estimate",
        entity_id=str(est.id),
        details={
            "estimate_number": est.estimate_number,
            "client_ip": _client_ip(request),
            "reason": reason,
        },
    )
    db.commit()

    return _serialize_public_estimate(est, db, request)
