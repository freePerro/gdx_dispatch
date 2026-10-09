"""Contractor resale quotes: eligibility, the disclaimer, markup math, the logo
and the branded PDF's data.

The markup math is pure (`build_snapshot`). It reads the payload the estimate
PDF already builds (`routers/pdf._estimate_payload`) and returns what the
branded PDF prints. Each unit price is scaled by (1 + m) and rounded to the
cent; a line that multiplies out on our estimate is rebuilt as quantity × the
marked unit, any other line is scaled itself. The subtotal is the sum of the
printed lines, so the table sums to its Total. Our tax, deposit, terms, notes, description, jobsite, signature and
attachments are never carried over: the reseller's document holds the
reseller's words, and none of our identity (`resale_pdf_data`).
"""
from __future__ import annotations

import hashlib
import os
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.branding_logo import RESELLER_LOGO_SUBDIR, reseller_logo_file
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.door_listings import service as _listing_service
from gdx_dispatch.modules.door_listings.service import PhotoProcessingError
from gdx_dispatch.modules.reseller.models import ResaleQuote, ResellerProfile

__all__ = [
    "ALLOWED_LOGO_MIME_TYPES",
    "DISCLAIMER_TEXT",
    "DISCLAIMER_VERSION",
    "ELIGIBLE_CUSTOMER_TYPES",
    "MAX_LOGO_BYTES",
    "MAX_MARKUP_PCT",
    "PhotoProcessingError",
    "build_snapshot",
]

#: Who may resell: every portal account whose customer type is Contractor or
#: Wholesale (Doug, 2026-10-09). Not `pricing_class`: on production the trade
#: accounts carry the type and leave pricing_class blank, so the first version,
#: which read pricing_class, showed the feature to none of them.
ELIGIBLE_CUSTOMER_TYPES = frozenset({"contractor", "wholesale"})

#: Shown before first use. The version is a hash of the words, so rewording it
#: makes every earlier acceptance stale and the reseller agrees again — the
#: plugin-consent fingerprint idea (core/plugin_consent.py).
DISCLAIMER_TEXT = (
    "Your branding, markup and resale quotes are private to your account. We don't "
    "use or review them in the course of business, and they never change what we "
    "charge you. They are stored on our servers, so they may be seen by people "
    "maintaining the system, for example during updates, backups, troubleshooting "
    "or support. By continuing you agree to this."
)
DISCLAIMER_VERSION = hashlib.sha256(DISCLAIMER_TEXT.encode()).hexdigest()[:16]

MAX_MARKUP_PCT = Decimal("500")
#: SVG is excluded on purpose: it is a script container, and WeasyPrint
#: fetches what an SVG references.
ALLOWED_LOGO_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_LOGO_BYTES = 5 * 1024 * 1024

#: The `lines_snapshot` shape this code writes.
SNAPSHOT_VERSION = 1

_CENT = Decimal("0.01")


# ── Eligibility and the disclaimer ───────────────────────────────────────────


def is_eligible(db: Session, customer_id: Any) -> bool:
    customer_type = db.execute(
        select(Customer.customer_type).where(Customer.id == customer_id, Customer.deleted_at.is_(None))
    ).scalar_one_or_none()
    # Free text: the form writes "Wholesale", QuickBooks imports may not.
    return (customer_type or "").strip().lower() in ELIGIBLE_CUSTOMER_TYPES


def get_profile(db: Session, customer_id: Any) -> ResellerProfile | None:
    return db.execute(
        select(ResellerProfile).where(ResellerProfile.customer_id == customer_id)
    ).scalar_one_or_none()


def disclaimer_current(profile: ResellerProfile | None) -> bool:
    return profile is not None and profile.disclaimer_version == DISCLAIMER_VERSION


# ── Input shapes ─────────────────────────────────────────────────────────────


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class ProfileIn(BaseModel):
    """PUT /portal/reseller/profile. Omitted fields are left as they are."""

    company_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=500)
    website: str | None = Field(default=None, max_length=200)
    license_no: str | None = Field(default=None, max_length=100)
    terms_text: str | None = Field(default=None, max_length=10000)
    default_markup_pct: Decimal | None = Field(default=None, ge=0, le=MAX_MARKUP_PCT, decimal_places=2)
    #: Agreeing to the disclaimer: the version the reseller was shown.
    accept_disclaimer_version: str | None = Field(default=None, max_length=32)

    _blank = field_validator(
        "company_name", "phone", "email", "address", "website", "license_no", "terms_text", mode="before"
    )(_blank_to_none)


PROFILE_FIELDS = ("company_name", "phone", "email", "address", "website", "license_no", "terms_text",
                  "default_markup_pct")


class ResaleQuoteIn(BaseModel):
    """POST /portal/estimates/{id}/resale."""

    markup_pct: Decimal | None = Field(default=None, ge=0, le=MAX_MARKUP_PCT, decimal_places=2)
    reference: str | None = Field(default=None, max_length=60)
    end_customer_name: str | None = Field(default=None, max_length=200)
    end_customer_address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)

    _blank = field_validator(
        "reference", "end_customer_name", "end_customer_address", "notes", mode="before"
    )(_blank_to_none)


# ── Markup math ──────────────────────────────────────────────────────────────


def _cents(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _mark(value: Any, factor: Decimal) -> Decimal:
    return (Decimal(str(value or 0)) * factor).quantize(_CENT, rounding=ROUND_HALF_UP)


def _qty(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _mark_rows(
    rows: list[dict[str, Any]], factor: Decimal, base_subtotal: Decimal, hide_prices: bool
) -> tuple[list[dict[str, Any]], bool]:
    """(printed rows, whether they carry prices).

    Prices are dropped — not hidden in a template, absent from the snapshot —
    when the estimate hides line prices: marked lines divided by (1 + m) would
    hand back the very prices we chose not to show. They are also dropped when
    our own lines do not add up to our subtotal (a hand-set total), because a
    marked-up table that does not sum to its own total is worse than none.
    """
    priced = not hide_prices and sum((_cents(r.get("line_total")) for r in rows), Decimal("0")) == base_subtotal
    out = []
    for r in rows:
        row: dict[str, Any] = {
            "description": r.get("description") or "",
            "quantity": r.get("quantity"),
        }
        if priced:
            unit = _mark(r.get("unit_price"), factor)
            line = _mark(r.get("line_total"), factor)
            qty = _qty(r.get("quantity"))
            # Rounding the unit and the line separately breaks qty × unit
            # (6 × 1.03 at 50%: 1.55 each, 9.27 a line). Where our row
            # multiplies out, theirs is built to as well; a row of ours that
            # does not is printed the way our own PDF prints it.
            if qty is not None and (_cents(r.get("unit_price")) * qty).quantize(_CENT, rounding=ROUND_HALF_UP) == _cents(r.get("line_total")):
                line = (unit * qty).quantize(_CENT, rounding=ROUND_HALF_UP)
            row["unit_price"] = str(unit)
            row["line_total"] = str(line)
        out.append(row)
    return out, priced


def _resale_lines_total(rows: list[dict[str, Any]]) -> Decimal:
    return sum((Decimal(r["line_total"]) for r in rows), Decimal("0"))


def build_snapshot(payload: dict[str, Any], markup_pct: Decimal) -> dict[str, Any]:
    """Mark up an estimate payload. Returns the frozen snapshot plus the two
    tracking numbers (`base_subtotal`, `resale_subtotal`, both None for an
    open Good/Better/Best proposal, which has a price per option instead)."""
    factor = Decimal("1") + Decimal(str(markup_pct)) / Decimal("100")
    hide = bool(payload.get("hide_line_prices"))

    if payload.get("tier_options"):
        # Like our own PDF of an open proposal: each option at its own price,
        # no discount or single total (routers/pdf._estimate_payload).
        options = []
        for opt in payload["tier_options"]:
            base_price = _cents(opt.get("price"))
            rows, priced = _mark_rows(list(opt.get("lines") or []), factor, base_price, hide)
            resale_price = _resale_lines_total(rows) if priced else _mark(base_price, factor)
            options.append({
                "name": opt.get("name") or "",
                "description": opt.get("description") or "",
                "warranty_months": int(opt.get("warranty_months") or 0),
                "base_price": str(base_price),
                "price": str(resale_price),
                "lines": rows,
                "lines_priced": priced,
            })
        return {
            "snapshot": {"version": SNAPSHOT_VERSION, "kind": "options", "options": options},
            "base_subtotal": None,
            "resale_subtotal": None,
            "hide_line_prices": hide,
        }

    subtotal = _cents(payload.get("subtotal"))
    discount = _cents(payload.get("discount"))
    rows, priced = _mark_rows(list(payload.get("lines") or []), factor, subtotal, hide)
    resale_lines = _resale_lines_total(rows) if priced else _mark(subtotal, factor)
    # A discount larger than the subtotal floors the total at 0, as ours does
    # (modules/proposals/totals.py): never a negative price.
    discount = min(discount, subtotal)
    if discount:
        # Their total is ours after discount × (1 + m), and their discount is
        # what closes the gap to their lines. Scaling the discount on its own
        # drifts from lines rounded per unit: 100 × 1.00 less 50 at 0.4% came
        # to 49.80, under what they pay us. If their lines round below that
        # target (a tiny markup on cheap units), no discount prints and the
        # total is their lines, which are still at least what they pay us.
        target = _mark(subtotal - discount, factor)
        resale_discount = min(max(resale_lines - target, Decimal("0.00")), resale_lines)
    else:
        resale_discount = Decimal("0.00")
    resale_subtotal = resale_lines - resale_discount
    accepted = payload.get("accepted_tier") or None
    return {
        "snapshot": {
            "version": SNAPSHOT_VERSION,
            "kind": "lines",
            "heading": accepted.get("name") if accepted else None,
            "lines": rows,
            "lines_priced": priced,
            "subtotal": str(resale_lines),
            "discount": str(resale_discount),
            "total": str(resale_subtotal),
        },
        "base_subtotal": subtotal - discount,
        "resale_subtotal": resale_subtotal,
        "hide_line_prices": hide,
    }


def tracking_row(quote: ResaleQuote, estimate_number: str | None) -> dict[str, Any]:
    """One row of the reseller's own tracking list."""
    snap = quote.lines_snapshot or {}
    base = quote.base_subtotal
    resale = quote.resale_subtotal
    row: dict[str, Any] = {
        "id": str(quote.id),
        "estimate_id": str(quote.estimate_id),
        "estimate_number": estimate_number,
        "reference": quote.reference,
        "end_customer_name": quote.end_customer_name,
        "end_customer_address": quote.end_customer_address,
        "notes": quote.notes,
        "markup_pct": float(quote.markup_pct),
        "base_subtotal": float(base) if base is not None else None,
        "resale_subtotal": float(resale) if resale is not None else None,
        "markup_amount": float(resale - base) if base is not None and resale is not None else None,
        "hide_line_prices": bool(quote.hide_line_prices),
        "created_at": quote.created_at.isoformat() if quote.created_at else None,
        "options": None,
    }
    if snap.get("kind") == "options":
        row["options"] = [
            {
                "name": o["name"],
                "base_price": float(o["base_price"]),
                "price": float(o["price"]),
                "markup_amount": float(Decimal(o["price"]) - Decimal(o["base_price"])),
            }
            for o in snap.get("options") or []
        ]
    return row


# ── Profile ──────────────────────────────────────────────────────────────────


def serialize_profile(profile: ResellerProfile | None) -> dict[str, Any]:
    p = profile
    return {
        "company_name": p.company_name if p else None,
        "phone": p.phone if p else None,
        "email": p.email if p else None,
        "address": p.address if p else None,
        "website": p.website if p else None,
        "license_no": p.license_no if p else None,
        "terms_text": p.terms_text if p else None,
        "default_markup_pct": float(p.default_markup_pct) if p and p.default_markup_pct is not None else 0.0,
        "has_logo": bool(p and p.logo_file),
        "disclaimer": {
            "text": DISCLAIMER_TEXT,
            "version": DISCLAIMER_VERSION,
            "accepted": disclaimer_current(p),
            "accepted_at": (
                p.disclaimer_accepted_at.isoformat()
                if p and p.disclaimer_accepted_at and disclaimer_current(p) else None
            ),
        },
    }


# ── Logo ─────────────────────────────────────────────────────────────────────


def logo_dir() -> Path:
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads")) / RESELLER_LOGO_SUBDIR


def store_logo(data: bytes, content_type: str) -> str:
    """Re-encode and write a logo; returns the minted basename.

    Re-encoding through the door-listing pipeline strips EXIF and refuses
    (`PhotoProcessingError`) anything Pillow cannot open — a renamed text file
    is not stored. That pipeline passes bytes through untouched when Pillow is
    absent, so refuse up front rather than store an unprocessed upload.
    """
    if not _listing_service._HAS_PILLOW:
        raise PhotoProcessingError("Logo uploads are unavailable right now.")
    processed, final_type = _listing_service.compress_for_web(data, content_type)
    name = f"reseller-logo-{uuid4().hex}.{'png' if final_type == 'image/png' else 'jpg'}"
    path = reseller_logo_file(name)
    if path is None:  # pragma: no cover - the minted name always matches
        raise PhotoProcessingError("That image couldn't be stored.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(processed)
    return name


def remove_logo_file(name: str | None) -> None:
    """Delete a replaced logo. The bytes are the reseller's private data, so a
    superseded copy is not kept."""
    path = reseller_logo_file(name or "")
    if path is not None and path.is_file():
        path.unlink()


# ── PDF ──────────────────────────────────────────────────────────────────────


def _floats(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        row = dict(r)
        for key in ("unit_price", "line_total"):
            if key in row:
                row[key] = float(row[key])
        out.append(row)
    return out


def resale_pdf_data(
    quote: ResaleQuote, profile: ResellerProfile | None, *, quote_date: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(document data, branding) for `core.pdf_generator.generate_resale_quote_pdf`.

    Built ONLY from the snapshot, the quote and the reseller's own profile.
    Nothing here reads our AppSettings, PDF template, default terms, the
    estimate's number, notes or attachments — the sibling sweep for "our
    identity leaking into a document under someone else's brand" is this
    function's input list.
    """
    snap = quote.lines_snapshot or {}
    data: dict[str, Any] = {
        "reference": quote.reference,
        "quote_date": quote_date,
        "prepared_for": {
            "name": quote.end_customer_name or "",
            "address": quote.end_customer_address or "",
        },
        "notes": quote.notes or "",
        # The reseller's terms as they stood when the quote was made.
        "terms": snap.get("terms") or "",
        "heading": snap.get("heading") or "",
        "lines": [],
        "hide_line_prices": True,
        "tier_options": [],
        "subtotal": 0.0,
        "discount": 0.0,
        "total": 0.0,
    }
    if snap.get("kind") == "options":
        data["tier_options"] = [
            {
                "name": o["name"],
                "description": o["description"],
                "warranty_months": o["warranty_months"],
                "price": float(o["price"]),
                "lines": _floats(o["lines"]),
                "hide_line_prices": not o["lines_priced"],
            }
            for o in snap.get("options") or []
        ]
    else:
        data.update({
            "lines": _floats(snap.get("lines") or []),
            "hide_line_prices": not snap.get("lines_priced"),
            "subtotal": float(snap.get("subtotal") or 0),
            "discount": float(snap.get("discount") or 0),
            "total": float(snap.get("total") or 0),
        })

    logo_uri = ""
    if profile and profile.logo_file:
        path = reseller_logo_file(profile.logo_file)
        if path is not None and path.is_file():
            logo_uri = path.as_uri()
    branding = {
        "company_name": (profile.company_name if profile else None) or "",
        "address": (profile.address if profile else None) or "",
        "phone": (profile.phone if profile else None) or "",
        "email": (profile.email if profile else None) or "",
        "website": (profile.website if profile else None) or "",
        "license_no": (profile.license_no if profile else None) or "",
        "logo": logo_uri,
    }
    return data, branding


def next_reference(db: Session, customer_id: Any) -> str:
    """A default reference when the reseller leaves it blank: Q-0001, Q-0002…
    counted over every quote they ever made, so a deleted one is not reused."""
    n = db.execute(
        select(func.count()).select_from(ResaleQuote).where(ResaleQuote.customer_id == customer_id)
    ).scalar_one()
    return f"Q-{int(n) + 1:04d}"
