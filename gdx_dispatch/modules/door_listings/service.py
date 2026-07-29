"""Door-listing helpers: the customer-submission gate, slugs, photos, ordering.

Everything here is shared by the office router, the tech-mobile submit route,
the portal submit route, and the public API — so the rules live in exactly one
place and the four callers cannot drift.
"""
from __future__ import annotations

import io
import logging
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import AppSettings, Customer
from gdx_dispatch.modules.door_listings.models import DoorListing, DoorListingPhoto

log = logging.getLogger(__name__)

try:
    from PIL import Image as _PILImage

    _HAS_PILLOW = True
except ImportError:  # pragma: no cover - Pillow is a hard dep in the image
    _HAS_PILLOW = False
    log.warning("Pillow not installed — door-listing photos stored unprocessed")

#: Marketing-grid sizing. The 2048px in routers/uploads.py is sized for job
#: evidence a tech zooms into; a public card grid wants a fraction of that, and
#: page weight is a ranking factor on the very searches this page targets.
MAX_WEB_DIMENSION = 1400
WEB_JPEG_QUALITY = 82

#: Untrusted images (tech phones, customer uploads) reaching a public page.
ALLOWED_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTOS_PER_LISTING = 8


class PhotoProcessingError(Exception):
    """Raised when an upload cannot be safely re-encoded. Callers turn this into
    a 422 — never a stored file."""


# ── The customer-submission gate ─────────────────────────────────────────────


def company_listings_enabled(db: Session) -> bool:
    """Company-wide master switch. Missing settings row → off."""
    row = db.execute(select(AppSettings)).scalars().first()
    return bool(getattr(row, "customer_listings_enabled", False)) if row else False


def customer_may_submit(db: Session, customer_id: UUID | str | None) -> bool:
    """Both switches must be on. Company-off wins over any per-customer grant.

    This is the ONLY place the two flags are combined — portal routes call it
    for the 404 decision and the portal UI calls it (via /context) to decide
    whether to show the entry point at all. Hiding a button is not access
    control, so both paths ask the same function.
    """
    if not company_listings_enabled(db):
        return False
    if customer_id is None:
        return False
    # customers.id is a UUID column; a str would blow up the bind processor.
    try:
        cid = customer_id if isinstance(customer_id, UUID) else UUID(str(customer_id))
    except (ValueError, TypeError, AttributeError):
        return False
    customer = db.execute(
        select(Customer).where(Customer.id == cid)
    ).scalars().first()
    return bool(customer and getattr(customer, "can_submit_listings", False))


# ── Slugs ────────────────────────────────────────────────────────────────────


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:180] or "door"


def unique_slug(db: Session, title: str, *, exclude_id: UUID | None = None) -> str:
    """A slug the public per-door URL can rely on staying unique."""
    base = slugify(title)
    candidate = base
    suffix = 2
    while True:
        stmt = select(DoorListing).where(DoorListing.slug == candidate)
        if exclude_id is not None:
            stmt = stmt.where(DoorListing.id != exclude_id)
        if db.execute(stmt).scalars().first() is None:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


# ── Photos ───────────────────────────────────────────────────────────────────


def _upload_root() -> Path:
    """The persisted volume (docker_gdx_uploads), never MOBILE_UPLOAD_DIR.

    routers/mobile.py writes job photos to MOBILE_UPLOAD_DIR, which defaults to
    /tmp/gdx_mobile_uploads — not a Docker volume. A listing photo stored there
    would vanish on container restart while the public site still linked it.
    """
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def photo_path(tenant_id: str, listing_id: UUID | str, filename: str) -> Path:
    """Resolve + fence a listing-photo path against the upload root.

    `filename` always comes from the DB (never the request), but resolve and
    fence anyway — realpath + startswith is the form CodeQL's py/path-injection
    recognizes as a barrier, and the trailing os.sep stops a sibling directory
    like "<root>-evil" from passing.
    """
    base = os.path.realpath(_upload_root())
    candidate = os.path.realpath(
        os.path.join(base, str(tenant_id), "door_listings", str(listing_id), os.path.basename(filename))
    )
    if not candidate.startswith(base + os.sep):
        raise ValueError("Invalid listing photo path")
    return Path(candidate)


def compress_for_web(data: bytes, content_type: str) -> tuple[bytes, str]:
    """Downscale to MAX_WEB_DIMENSION and re-encode.

    The re-encode is load-bearing, not incidental: it strips EXIF — including
    the GPS tag a phone stamps on a photo taken in someone's driveway — and it
    neutralizes polyglot files that are a valid image AND a valid script. These
    images come from tech phones and customer uploads and land on a public
    page, so both matter.
    """
    if not _HAS_PILLOW:
        return data, content_type
    try:
        with _PILImage.open(io.BytesIO(data)) as img:
            has_alpha = img.mode in ("RGBA", "LA", "P")
            w, h = img.size
            if w > MAX_WEB_DIMENSION or h > MAX_WEB_DIMENSION:
                ratio = min(MAX_WEB_DIMENSION / w, MAX_WEB_DIMENSION / h)
                img = img.resize((int(w * ratio), int(h * ratio)), _PILImage.LANCZOS)

            out = io.BytesIO()
            if has_alpha:
                img.convert("RGBA").save(out, format="PNG", optimize=True)
                return out.getvalue(), "image/png"
            img.convert("RGB").save(
                out, format="JPEG", quality=WEB_JPEG_QUALITY, optimize=True
            )
            return out.getvalue(), "image/jpeg"
    except Exception as exc:
        # Fail CLOSED. Storing the original on a processing failure would ship a
        # file Pillow could not parse straight to a public page — with its EXIF
        # (including the GPS tag a phone stamps on a photo taken in someone's
        # driveway) intact, which is precisely what the re-encode exists to strip.
        log.exception("door-listing photo could not be processed; rejecting upload")
        raise PhotoProcessingError(
            "That image couldn't be processed. Try a standard JPEG or PNG."
        ) from exc


def store_photo(
    db: Session,
    *,
    tenant_id: str,
    listing: DoorListing,
    data: bytes,
    content_type: str,
) -> DoorListingPhoto:
    """Compress, write to the persisted volume, and record the row."""
    processed, final_type = compress_for_web(data, content_type)
    ext = "png" if final_type == "image/png" else "jpg"
    filename = f"{uuid4().hex}.{ext}"

    target = photo_path(tenant_id, listing.id, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(processed)

    next_order = max((p.sort_order for p in listing.photos), default=-1) + 1
    photo = DoorListingPhoto(
        listing_id=listing.id,
        filename=filename,
        content_type=final_type,
        sort_order=next_order,
    )
    db.add(photo)
    return photo


# ── Serialization ────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def serialize(listing: DoorListing, *, public: bool = False) -> dict[str, Any]:
    """One listing as JSON.

    `public=True` drops everything the outside world has no business seeing:
    internal provenance (who submitted it, which job it came off), the
    moderation trail, and — when `price_display` is `call_for_price` — the
    price itself, which must not leak in a field the website merely declines
    to render.
    """
    data: dict[str, Any] = {
        "id": str(listing.id),
        "listing_type": listing.listing_type,
        "title": listing.title,
        "slug": listing.slug,
        "description": listing.description,
        "condition": listing.condition,
        "width_in": _num(listing.width_in),
        "height_in": _num(listing.height_in),
        "material": listing.material,
        "color": listing.color,
        "insulation_r": _num(listing.insulation_r),
        "has_windows": bool(listing.has_windows),
        "brand": listing.brand,
        "model": listing.model,
        "qty": listing.qty,
        "featured": bool(listing.featured),
        "price_display": listing.price_display,
        "photos": [
            {"id": str(p.id), "sort_order": p.sort_order}
            for p in sorted(listing.photos, key=lambda p: p.sort_order)
        ],
    }

    show_price = listing.price_display != "call_for_price"
    data["price"] = _num(listing.price) if show_price else None
    data["compare_at_price"] = _num(listing.compare_at_price) if show_price else None

    if public:
        data["gdx_owned"] = listing.is_gdx_owned
        data["published_at"] = (
            listing.published_at.isoformat() if listing.published_at else None
        )
        return data

    data.update(
        {
            "status": listing.status,
            "source": listing.source,
            "gdx_owned": listing.is_gdx_owned,
            "submitted_by_user_id": str(listing.submitted_by_user_id)
            if listing.submitted_by_user_id
            else None,
            "submitted_by_customer_id": str(listing.submitted_by_customer_id)
            if listing.submitted_by_customer_id
            else None,
            "submitted_at": listing.submitted_at.isoformat() if listing.submitted_at else None,
            "reviewed_by_user_id": str(listing.reviewed_by_user_id)
            if listing.reviewed_by_user_id
            else None,
            "reviewed_at": listing.reviewed_at.isoformat() if listing.reviewed_at else None,
            "rejection_reason": listing.rejection_reason,
            "source_job_id": str(listing.source_job_id) if listing.source_job_id else None,
            "published_at": listing.published_at.isoformat() if listing.published_at else None,
            "sold_at": listing.sold_at.isoformat() if listing.sold_at else None,
            "created_at": listing.created_at.isoformat() if listing.created_at else None,
        }
    )
    return data


def public_ordering() -> list[Any]:
    """GDX stock first, then pins, then freshest, then a stable tiebreak.

    Computed here rather than in the Astro template so the rule lives in one
    place and the website cannot drift from it. Applied AFTER filtering, so
    filtering to "used" still shows GDX used doors above customer used doors.
    """
    from sqlalchemy import case

    return [
        case((DoorListing.source == "customer", 1), else_=0).asc(),
        DoorListing.featured.desc(),
        DoorListing.published_at.desc().nulls_last(),
        DoorListing.id.asc(),
    ]
