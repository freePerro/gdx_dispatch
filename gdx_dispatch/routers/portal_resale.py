"""Contractor resale quotes — the portal side, and the ONLY reader of the
reseller tables (tests/test_reseller_privacy.py holds that line).

A contractor or wholesale customer signs in to the portal, sets up their own
brand, agrees to the privacy disclaimer, and resells one of their estimates to
their own customer: a frozen, marked-up snapshot they download as a PDF under
their name. Download only — nothing is emailed from our server.

Every route needs the portal JWT AND a customer priced contractor/wholesale
(403 otherwise); every read and write is scoped to that customer, so another
customer's id answers 404. The quote routes also need the CURRENT disclaimer
accepted (403) — except delete, which a reseller can always do to their own
data. Audit rows carry ids only: who, what, when, and never the markup, the
totals or the end customer, so the reseller's margin does not surface in our
admin audit view.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_ready_db, log_audit_event_sync
from gdx_dispatch.core.branding_logo import reseller_logo_file
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.pay_periods import resolve_zone, shop_tz_name_from_settings
from gdx_dispatch.core.upload_limits import assert_upload_within_limit
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.reseller import service
from gdx_dispatch.modules.reseller.models import ResaleQuote, ResellerProfile
from gdx_dispatch.routers.portal import (
    PortalPrincipal,
    _get_customer_estimate_or_404,
    get_current_portal_customer,
)

portal_router = APIRouter(
    prefix="/portal",
    tags=["customer_portal"],
    dependencies=[Depends(require_module("customer_portal"))],
)


def _tenant_id(request: Request) -> str:
    return str(getattr(request.state, "tenant", {}).get("id", ""))


def _actor(principal: PortalPrincipal) -> str:
    return f"portal:{principal.user_id}"


def _audit(
    db: Session, request: Request, principal: PortalPrincipal, action: str,
    entity_type: str, entity_id: Any, details: dict[str, Any],
) -> None:
    log_audit_event_sync(
        db,
        tenant_id=_tenant_id(request),
        user_id=_actor(principal),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        details=details,
        request=request,
    )


def _require_eligible(db: Session, principal: PortalPrincipal) -> None:
    if not service.is_eligible(db, principal.customer_id):
        raise HTTPException(status_code=403, detail="Resale quotes are for contractor and wholesale accounts")


def _require_disclaimer(db: Session, principal: PortalPrincipal) -> ResellerProfile:
    _require_eligible(db, principal)
    profile = service.get_profile(db, principal.customer_id)
    if not service.disclaimer_current(profile):
        raise HTTPException(status_code=403, detail="Agree to the reseller terms on My Branding first")
    return profile  # type: ignore[return-value]


def _own_quote_or_404(db: Session, quote_id: UUID, principal: PortalPrincipal) -> ResaleQuote:
    quote = db.execute(
        select(ResaleQuote).where(
            ResaleQuote.id == quote_id,
            ResaleQuote.customer_id == principal.customer_id,
            ResaleQuote.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if quote is None:
        raise HTTPException(status_code=404, detail="Not found")
    return quote


# ── Profile and disclaimer ───────────────────────────────────────────────────


@portal_router.get("/reseller/profile", response_model=None)
def portal_reseller_profile(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The reseller's brand (blank before first save) and the disclaimer, with
    whether its current wording has been agreed to."""
    _require_eligible(db, principal)
    return service.serialize_profile(service.get_profile(db, principal.customer_id))


@portal_router.put("/reseller/profile", response_model=None)
def portal_save_reseller_profile(
    payload: service.ProfileIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Agree to the disclaimer (``accept_disclaimer_version``) and/or save the
    brand. The brand is exactly what the disclaimer covers, so it cannot be
    saved until the current wording has been agreed to."""
    _require_eligible(db, principal)
    profile = service.get_profile(db, principal.customer_id)
    now = datetime.now(UTC)

    accepting = payload.accept_disclaimer_version is not None
    if accepting and payload.accept_disclaimer_version != service.DISCLAIMER_VERSION:
        raise HTTPException(status_code=409, detail="The reseller terms changed. Reload and read them again.")

    sent = payload.model_fields_set - {"accept_disclaimer_version"}
    if sent and not accepting and not service.disclaimer_current(profile):
        raise HTTPException(status_code=403, detail="Agree to the reseller terms first")

    if profile is None:
        profile = ResellerProfile(customer_id=principal.customer_id, created_at=now)
        db.add(profile)
        try:
            db.flush()
        except IntegrityError as exc:
            # Two first saves at once (customer_id is unique): one wins.
            db.rollback()
            raise HTTPException(status_code=409, detail="Saved from another window. Reload and try again.") from exc

    if accepting and not service.disclaimer_current(profile):
        profile.disclaimer_version = service.DISCLAIMER_VERSION
        profile.disclaimer_accepted_at = now
        profile.disclaimer_accepted_by = principal.user_id
        profile.updated_at = now
        db.flush()
        _audit(db, request, principal, "reseller_disclaimer_accepted", "reseller_profile", profile.id,
               {"customer_id": str(principal.customer_id), "version": service.DISCLAIMER_VERSION})

    changed = []
    for field in service.PROFILE_FIELDS:
        if field not in sent:
            continue
        value = getattr(payload, field)
        if field == "default_markup_pct" and value is None:
            continue
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed.append(field)
    if changed:
        profile.updated_at = now
        db.flush()
        # Field NAMES only: what they charge and say stays theirs.
        _audit(db, request, principal, "reseller_profile_updated", "reseller_profile", profile.id,
               {"customer_id": str(principal.customer_id), "fields": changed})
    db.commit()
    db.refresh(profile)
    return service.serialize_profile(profile)


@portal_router.post("/reseller/logo", response_model=None, status_code=201)
async def portal_upload_reseller_logo(
    request: Request,
    file: UploadFile = File(...),
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Replace the reseller's logo. PNG, JPEG or WebP under 5 MB; re-encoded
    (EXIF stripped), and refused if it cannot be decoded."""
    profile = _require_disclaimer(db, principal)
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in service.ALLOWED_LOGO_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Use a PNG, JPEG or WebP image")
    assert_upload_within_limit(file, service.MAX_LOGO_BYTES)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > service.MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail="Logo too large")
    try:
        name = service.store_logo(data, content_type)
    except service.PhotoProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    old = profile.logo_file
    try:
        profile.logo_file = name
        profile.updated_at = datetime.now(UTC)
        db.flush()
        _audit(db, request, principal, "reseller_logo_uploaded", "reseller_profile", profile.id,
               {"customer_id": str(principal.customer_id)})
        db.commit()
    except Exception:
        # The file was written first; nothing will ever point at it now.
        db.rollback()
        service.remove_logo_file(name)
        raise
    service.remove_logo_file(old)
    return {"has_logo": True}


@portal_router.get("/reseller/logo", response_model=None)
def portal_reseller_logo(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> FileResponse:
    """The reseller's own logo, for the preview on My Branding."""
    _require_eligible(db, principal)
    profile = service.get_profile(db, principal.customer_id)
    path = reseller_logo_file(profile.logo_file or "") if profile else None
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media = "image/png" if path.suffix == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, no-store"})


# ── Resale quotes ────────────────────────────────────────────────────────────


@portal_router.post("/estimates/{estimate_id}/resale", response_model=None, status_code=201)
def portal_create_resale_quote(
    estimate_id: UUID,
    payload: service.ResaleQuoteIn,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Freeze a marked-up copy of one of the customer's own visible estimates.
    Writes only the resale_quotes row and its audit row: the estimate, our
    invoices and our books are read, never written."""
    from gdx_dispatch.modules.estimates_features import get_features
    from gdx_dispatch.routers.pdf import _estimate_payload

    profile = _require_disclaimer(db, principal)
    estimate = _get_customer_estimate_or_404(estimate_id, principal, db)

    markup = payload.markup_pct if payload.markup_pct is not None else (profile.default_markup_pct or Decimal("0"))
    # The tenant default for hidden prices, resolved as routers/portal does.
    tenant_id = _tenant_id(request) or str(estimate.company_id or "")
    hide_default = get_features(tenant_id).hide_line_prices
    # No customer, terms, attachments or deposit: none of ours is carried.
    estimate_payload = _estimate_payload(
        estimate, None, "", deposit_pct=0, hide_line_prices_default=hide_default, db=db
    )
    built = service.build_snapshot(estimate_payload, markup)
    snapshot = built["snapshot"]
    snapshot["terms"] = profile.terms_text or ""

    quote = ResaleQuote(
        customer_id=principal.customer_id,
        estimate_id=estimate.id,
        created_by=principal.user_id,
        reference=payload.reference or service.next_reference(db, principal.customer_id),
        end_customer_name=payload.end_customer_name,
        end_customer_address=payload.end_customer_address,
        notes=payload.notes,
        markup_pct=markup,
        base_subtotal=built["base_subtotal"],
        resale_subtotal=built["resale_subtotal"],
        lines_snapshot=snapshot,
        hide_line_prices=built["hide_line_prices"],
        created_at=datetime.now(UTC),
    )
    db.add(quote)
    db.flush()
    _audit(db, request, principal, "resale_quote_created", "resale_quote", quote.id,
           {"customer_id": str(principal.customer_id), "estimate_id": str(estimate.id)})
    db.commit()
    db.refresh(quote)
    return service.tracking_row(quote, estimate.estimate_number)


@portal_router.get("/resale-quotes", response_model=None)
def portal_list_resale_quotes(
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """The reseller's own quotes, newest first: our price, theirs, the markup."""
    _require_disclaimer(db, principal)
    quotes = db.execute(
        select(ResaleQuote)
        .where(ResaleQuote.customer_id == principal.customer_id, ResaleQuote.deleted_at.is_(None))
        .order_by(ResaleQuote.created_at.desc())
    ).scalars().all()
    numbers = dict(
        db.execute(
            select(Estimate.id, Estimate.estimate_number).where(Estimate.id.in_({q.estimate_id for q in quotes}))
        ).all()
    ) if quotes else {}
    return [service.tracking_row(q, numbers.get(q.estimate_id)) for q in quotes]


@portal_router.get("/resale-quotes/{quote_id}/pdf", response_model=None)
def portal_resale_quote_pdf(
    quote_id: UUID,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(get_db),
) -> Response:
    """The branded PDF, as a download."""
    from gdx_dispatch.core.pdf_generator import generate_resale_quote_pdf

    profile = _require_disclaimer(db, principal)
    quote = _own_quote_or_404(db, quote_id, principal)
    zone = resolve_zone(shop_tz_name_from_settings(db))
    created = quote.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    data, branding = service.resale_pdf_data(
        quote, profile, quote_date=created.astimezone(zone).date().isoformat()
    )
    pdf = generate_resale_quote_pdf(data, branding)
    # ASCII only: a header is latin-1, and str.isalnum() is true for "Ω".
    safe = "".join(c for c in quote.reference if (c.isascii() and c.isalnum()) or c in "-_") or "quote"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="quote-{safe}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )


@portal_router.delete("/resale-quotes/{quote_id}", response_model=None)
def portal_delete_resale_quote(
    quote_id: UUID,
    request: Request,
    principal: PortalPrincipal = Depends(get_current_portal_customer),
    db: Session = Depends(audit_ready_db),
) -> dict[str, Any]:
    """Soft delete: gone from the list, the row kept (invariant #2)."""
    _require_eligible(db, principal)
    quote = _own_quote_or_404(db, quote_id, principal)
    quote.deleted_at = datetime.now(UTC)
    db.flush()
    _audit(db, request, principal, "resale_quote_deleted", "resale_quote", quote.id,
           {"customer_id": str(principal.customer_id), "estimate_id": str(quote.estimate_id)})
    db.commit()
    return {"id": str(quote.id), "deleted": True}
