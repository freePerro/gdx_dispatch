"""Door listings — office CRUD, the approval gate, and public photo bytes.

Three ways a listing is born (office, a technician, a customer in the portal)
and exactly one way it reaches the public website: an office approval.

NOTE: technician submission is supported by this API (any holder of
`inventory.write` can POST, and the row is forced to `pending_review`), but the
tech-mobile console has NO listing UI yet — a tech reaches this through the
office web app. The field-capture screen is still to build.
That invariant is enforced here, in `_approve`, and nowhere else — no PATCH on
`status`, no create-with-status.

Permission model (see docs/design/door-listings-website-plan.md §4): submitting
needs `inventory.write`, which technicians already hold. Approving needs
`listings.publish` — a real authz key, NOT a nav key.

An earlier draft gated approval on `inventory.write` + `nav.office`. That was
wrong: `core/permissions.py` documents nav.* as "nav-visibility ONLY (no API
route enforces these)", and those keys are editable per-role in the Roles UI.
An admin adding office navigation to the technician role — a reasonable, purely
cosmetic-looking change — would have silently granted every technician the
ability to publish to the public internet. The boundary now rests on a key that
exists for exactly this purpose and that nothing else hands out.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.modules.door_listings import service
from gdx_dispatch.modules.door_listings.models import (
    LISTING_CONDITIONS,
    LISTING_STATUSES,
    LISTING_TYPES,
    PRICE_DISPLAYS,
    SUBMITTER_EDITABLE_STATUSES,
    DoorListing,
    DoorListingPhoto,
)
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/door-listings",
    tags=["door-listings"],
    dependencies=[Depends(require_module("inventory"))],
)

#: Unauthenticated, and deliberately OUTSIDE /api — see the route docstring.
public_router = APIRouter(tags=["door-listings-public"])

_CAN_SUBMIT = Depends(require_permission("inventory.write"))
# admin/owner hold this via live BUILTIN_ROLES; every other role must be granted
# it deliberately. Fail-closed is right for "can put this on the internet".
_CAN_APPROVE = Depends(require_permission("listings.publish"))
_CAN_READ = Depends(require_permission("inventory.read"))


# ── helpers ──────────────────────────────────────────────────────────────────


def _tenant_id(request: Request) -> str:
    return str(getattr(request.state, "tenant", {}).get("id", "") or "")


def _user_id(user: dict[str, Any]) -> str:
    return str(user.get("user_id") or user.get("sub") or "system")


def _now() -> datetime:
    return datetime.now(UTC)


def _get_or_404(db: Session, listing_id: UUID) -> DoorListing:
    listing = db.execute(
        select(DoorListing).where(
            DoorListing.id == listing_id, DoorListing.deleted_at.is_(None)
        )
    ).scalars().first()
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


def _audit(
    db: Session,
    request: Request,
    user: dict[str, Any],
    action: str,
    listing: DoorListing,
    details: dict[str, Any] | None = None,
) -> None:
    log_audit_event_sync(
        db=db,
        tenant_id=_tenant_id(request),
        user_id=_user_id(user),
        action=action,
        entity_type="door_listing",
        entity_id=str(listing.id),
        details=details or {},
        ip_address=(request.client.host if request.client else None),
        request=request,
    )


# ── schemas ──────────────────────────────────────────────────────────────────


class ListingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    listing_type: str = Field(default="used")
    description: str | None = Field(default=None, max_length=5000)
    condition: str | None = None
    width_in: float | None = Field(default=None, ge=0, le=99999)
    height_in: float | None = Field(default=None, ge=0, le=99999)
    material: str | None = Field(default=None, max_length=60)
    color: str | None = Field(default=None, max_length=60)
    insulation_r: float | None = Field(default=None, ge=0, le=999)
    has_windows: bool = False
    brand: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=80)
    qty: int = Field(default=1, ge=0, le=9999)
    price: float | None = Field(default=None, ge=0)
    compare_at_price: float | None = Field(default=None, ge=0)
    price_display: str = Field(default="fixed")
    featured: bool = False
    source_job_id: UUID | None = None

    def validated(self) -> ListingIn:
        if self.listing_type not in LISTING_TYPES:
            raise HTTPException(
                status_code=422, detail=f"listing_type must be one of {list(LISTING_TYPES)}"
            )
        if self.condition is not None and self.condition not in LISTING_CONDITIONS:
            raise HTTPException(
                status_code=422, detail=f"condition must be one of {list(LISTING_CONDITIONS)}"
            )
        if self.price_display not in PRICE_DISPLAYS:
            raise HTTPException(
                status_code=422, detail=f"price_display must be one of {list(PRICE_DISPLAYS)}"
            )
        return self


class ListingPatch(ListingIn):
    title: str | None = Field(default=None, min_length=1, max_length=200)

    def validated(self) -> ListingPatch:
        if self.listing_type not in LISTING_TYPES:
            raise HTTPException(
                status_code=422, detail=f"listing_type must be one of {list(LISTING_TYPES)}"
            )
        if self.condition is not None and self.condition not in LISTING_CONDITIONS:
            raise HTTPException(
                status_code=422, detail=f"condition must be one of {list(LISTING_CONDITIONS)}"
            )
        if self.price_display not in PRICE_DISPLAYS:
            raise HTTPException(
                status_code=422, detail=f"price_display must be one of {list(PRICE_DISPLAYS)}"
            )
        return self


class RejectIn(BaseModel):
    # Required, not optional: a rejection with no reason is how you train the
    # field to stop submitting.
    reason: str = Field(min_length=1, max_length=1000)


# ── writes ───────────────────────────────────────────────────────────────────


def _apply(listing: DoorListing, payload: ListingIn, db: Session, *, partial: bool = False) -> None:
    """Copy payload onto the row.

    `partial=True` (PATCH) applies ONLY fields the client actually sent. Without
    it, every unsent field would be written back as its Pydantic default and a
    one-word title fix would blank the brand, model, material, R-value and qty —
    including data a portal customer typed.
    """
    sent = payload.model_fields_set if partial else None

    def _should(field: str) -> bool:
        return sent is None or field in sent

    if payload.title is not None and _should("title"):
        if payload.title != listing.title:
            listing.slug = service.unique_slug(db, payload.title, exclude_id=listing.id)
        listing.title = payload.title
    for field in (
        "listing_type", "description", "condition", "width_in", "height_in",
        "material", "color", "insulation_r", "has_windows", "brand", "model",
        "qty", "price", "compare_at_price", "price_display", "source_job_id",
    ):
        if _should(field):
            setattr(listing, field, getattr(payload, field))
    listing.updated_at = _now()


@router.post("", status_code=201)
def create_listing(
    payload: ListingIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_SUBMIT,
) -> dict[str, Any]:
    """Create a listing.

    Office staff get a `draft` they can then publish. Anyone who can submit but
    NOT approve (i.e. a technician) gets `pending_review` — forced server-side,
    never read from the client.
    """
    payload.validated()
    is_office = _has_office(request, db, user)

    listing = DoorListing(
        title=payload.title,
        slug=service.unique_slug(db, payload.title),
        status="draft" if is_office else "pending_review",
        source="office" if is_office else "tech",
        submitted_by_user_id=_as_uuid(_user_id(user)),
        submitted_at=_now(),
        featured=bool(payload.featured) if is_office else False,
    )
    _apply(listing, payload, db)
    db.add(listing)
    db.commit()
    db.refresh(listing)

    _audit(db, request, user, "door_listing_created", listing,
           {"status": listing.status, "source": listing.source})
    db.commit()
    return service.serialize(listing)


@router.patch("/{listing_id}")
def update_listing(
    listing_id: UUID,
    payload: ListingPatch,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_SUBMIT,
) -> dict[str, Any]:
    """Edit a listing.

    Office edits anything. A submitter may edit only their OWN row and only
    while it is still `pending_review` or `rejected` — once published, the
    listing belongs to the office.
    """
    payload.validated()
    listing = _get_or_404(db, listing_id)

    if not _has_office(request, db, user):
        own = str(listing.submitted_by_user_id or "") == _user_id(user)
        if not own or listing.status not in SUBMITTER_EDITABLE_STATUSES:
            raise HTTPException(
                status_code=403,
                detail="You can only edit your own listings before they are approved",
            )

    _apply(listing, payload, db, partial=True)
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_updated", listing)
    db.commit()
    return service.serialize(listing)


@router.post("/{listing_id}/publish")
def publish_listing(
    listing_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    """THE gate. The only path to `published`, and it needs office rights."""
    listing = _get_or_404(db, listing_id)
    if not listing.photos:
        raise HTTPException(
            status_code=422,
            detail="Add at least one photo before publishing — a door with no picture doesn't sell",
        )
    # A sold door has to be deliberately revived, not re-published by a stray
    # click — re-listing something already sold is the fastest way to make the
    # whole page untrustworthy.
    if listing.status == "sold":
        raise HTTPException(
            status_code=409,
            detail="This door is marked sold. Clear the sale before listing it again.",
        )
    if listing.qty is not None and listing.qty < 1:
        raise HTTPException(status_code=422, detail="Quantity is 0 — nothing to sell")
    if listing.price_display == "fixed" and listing.price is None:
        raise HTTPException(
            status_code=422,
            detail="Set a price, or switch the listing to 'Call for price'",
        )
    listing.status = "published"
    listing.published_at = listing.published_at or _now()
    listing.reviewed_by_user_id = _as_uuid(_user_id(user))
    listing.reviewed_at = _now()
    listing.rejection_reason = None
    listing.updated_at = _now()
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_published", listing)
    db.commit()
    return service.serialize(listing)


@router.post("/{listing_id}/unpublish")
def unpublish_listing(
    listing_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)
    listing.status = "archived"
    listing.updated_at = _now()
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_unpublished", listing)
    db.commit()
    return service.serialize(listing)


@router.post("/{listing_id}/reject")
def reject_listing(
    listing_id: UUID,
    payload: RejectIn,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)
    listing.status = "rejected"
    listing.rejection_reason = payload.reason
    listing.reviewed_by_user_id = _as_uuid(_user_id(user))
    listing.reviewed_at = _now()
    listing.updated_at = _now()
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_rejected", listing, {"reason": payload.reason})
    db.commit()
    return service.serialize(listing)


@router.post("/{listing_id}/sold")
def mark_sold(
    listing_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    """One click from the grid. A sold door left published is worse than no page."""
    listing = _get_or_404(db, listing_id)
    listing.status = "sold"
    listing.sold_at = _now()
    listing.updated_at = _now()
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_sold", listing)
    db.commit()
    return service.serialize(listing)


@router.post("/{listing_id}/feature")
def set_featured(
    listing_id: UUID,
    request: Request,
    featured: bool = Query(default=True),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)
    listing.featured = featured
    listing.updated_at = _now()
    db.commit()
    db.refresh(listing)
    _audit(db, request, user, "door_listing_featured", listing, {"featured": featured})
    db.commit()
    return service.serialize(listing)


# Archive, never hard-delete: useDestructiveConfirm currently auto-accepts
# without rendering (issue #215), so a hard-delete button would delete on a
# misclick with no dialog. The row and its history stay.
@router.delete("/{listing_id}")
def archive_listing(
    listing_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_APPROVE,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)
    listing.status = "archived"
    listing.deleted_at = _now()
    db.commit()
    _audit(db, request, user, "door_listing_archived", listing)
    db.commit()
    return {"ok": True, "id": str(listing_id)}


# ── photos ───────────────────────────────────────────────────────────────────


@router.post("/{listing_id}/photos", status_code=201)
async def upload_photo(
    listing_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_SUBMIT,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)

    if not _has_office(request, db, user):
        own = str(listing.submitted_by_user_id or "") == _user_id(user)
        if not own or listing.status not in SUBMITTER_EDITABLE_STATUSES:
            raise HTTPException(status_code=403, detail="Not your listing to edit")

    if len(listing.photos) >= service.MAX_PHOTOS_PER_LISTING:
        raise HTTPException(
            status_code=422,
            detail=f"At most {service.MAX_PHOTOS_PER_LISTING} photos per listing",
        )

    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in service.ALLOWED_PHOTO_MIME_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported image type. Allowed: {sorted(service.ALLOWED_PHOTO_MIME_TYPES)}",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > service.MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Image exceeds {service.MAX_PHOTO_BYTES // (1024 * 1024)}MB",
        )

    try:
        photo = service.store_photo(
            db, tenant_id=_tenant_id(request), listing=listing, data=data, content_type=content_type
        )
    except service.PhotoProcessingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    db.refresh(photo)
    _audit(db, request, user, "door_listing_photo_added", listing, {"photo_id": str(photo.id)})
    db.commit()
    return {"id": str(photo.id), "sort_order": photo.sort_order}


@router.delete("/{listing_id}/photos/{photo_id}")
def delete_photo(
    listing_id: UUID,
    photo_id: UUID,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_SUBMIT,
) -> dict[str, Any]:
    listing = _get_or_404(db, listing_id)
    photo = db.execute(
        select(DoorListingPhoto).where(
            DoorListingPhoto.id == photo_id, DoorListingPhoto.listing_id == listing_id
        )
    ).scalars().first()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")

    if not _has_office(request, db, user):
        own = str(listing.submitted_by_user_id or "") == _user_id(user)
        if not own or listing.status not in SUBMITTER_EDITABLE_STATUSES:
            raise HTTPException(status_code=403, detail="Not your listing to edit")

    try:
        service.photo_path(_tenant_id(request), listing_id, photo.filename).unlink(missing_ok=True)
    except (OSError, ValueError):
        log.exception("door-listing photo file removal failed for %s", photo_id)

    db.delete(photo)
    db.commit()
    _audit(db, request, user, "door_listing_photo_deleted", listing, {"photo_id": str(photo_id)})
    db.commit()
    return {"ok": True}


# ── reads ────────────────────────────────────────────────────────────────────


@router.get("")
def list_listings(
    request: Request,
    status: str = Query(default=""),
    listing_type: str = Query(default=""),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_READ,
) -> dict[str, Any]:
    stmt = select(DoorListing).where(DoorListing.deleted_at.is_(None))
    if status:
        if status not in LISTING_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown status '{status}'")
        stmt = stmt.where(DoorListing.status == status)
    if listing_type:
        stmt = stmt.where(DoorListing.listing_type == listing_type)

    rows = db.execute(stmt.order_by(*service.public_ordering())).scalars().all()
    pending = db.execute(
        select(DoorListing).where(
            DoorListing.status == "pending_review", DoorListing.deleted_at.is_(None)
        )
    ).scalars().all()
    return {
        "listings": [service.serialize(r) for r in rows],
        "pending_count": len(pending),
    }


@router.get("/{listing_id}")
def get_listing(
    listing_id: UUID,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    _p: None = _CAN_READ,
) -> dict[str, Any]:
    return service.serialize(_get_or_404(db, listing_id))


# ── public photo bytes ───────────────────────────────────────────────────────


@public_router.get("/public/door-listings/{photo_id}.jpg")
def public_listing_photo(photo_id: UUID, db: Session = Depends(get_db)) -> FileResponse:
    """Image bytes for a PUBLISHED listing. No auth, and deliberately not /api.

    Why unauthenticated: `core/api_keys.py` caps each API key at 60 req/min. A
    page of listings is N image requests per uncached visitor, so serving these
    through the keyed API would trip the cap and hand an `<img>` tag a 429 JSON
    body. Keeping photos off the key means a key problem degrades to "no doors
    listed" instead of "every image broken".

    Why that's safe: the row must join to a `published` listing (a draft or
    pending photo 404s), the route accepts a UUID only and reads the filename
    from the DB — no request-supplied path component — and the content is an
    advertisement we are actively trying to show the public, with EXIF (incl.
    phone GPS) already stripped at upload.
    """
    row = db.execute(
        select(DoorListingPhoto, DoorListing)
        .join(DoorListing, DoorListingPhoto.listing_id == DoorListing.id)
        .where(
            DoorListingPhoto.id == photo_id,
            DoorListing.status == "published",
            DoorListing.deleted_at.is_(None),
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")

    photo, listing = row
    # Tenant dir: single tenant per DB, so the connection already identifies it.
    # Resolve across tenant dirs rather than trusting a header on a public route.
    import os
    from pathlib import Path

    base = Path(os.getenv("UPLOAD_DIR", "/app/uploads"))
    for tenant_dir in sorted(base.glob("*")):
        candidate = tenant_dir / "door_listings" / str(listing.id) / photo.filename
        if candidate.is_file():
            return FileResponse(
                candidate,
                media_type=photo.content_type or "image/jpeg",
                headers={"Cache-Control": "public, max-age=86400, immutable"},
            )
    raise HTTPException(status_code=404, detail="Not found")


# ── permission probe ─────────────────────────────────────────────────────────


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _has_office(request: Request, db: Session, user: dict[str, Any]) -> bool:
    """True when the caller could pass `_CAN_APPROVE`.

    Reuses the resolver behind `require_permission` so "who is office" has one
    definition. Never trust the JWT `role` claim — role demotions must take
    effect immediately, which is why the resolver reads the tenant DB.
    """
    from gdx_dispatch.core.modules import _load_user_permissions, _resolve_request_user
    from gdx_dispatch.core.permissions import WILDCARD

    try:
        cached = getattr(request.state, "user_permissions", None)
        if cached is None:
            cached = _load_user_permissions(db, request, _resolve_request_user(request))
            request.state.user_permissions = cached
        perms = set(cached or [])
    except Exception:
        log.exception("door-listings: permission resolution failed; denying office rights")
        return False
    if WILDCARD in perms:
        return True
    return "listings.publish" in perms
