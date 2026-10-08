"""Quote requests — validation, photo storage, the customer-facing status.

Photos reuse the door-listing pipeline (`door_listings.service.compress_for_web`):
re-encode that applies orientation and strips EXIF, including the GPS tag a
phone stamps on a photo taken in someone's driveway.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from gdx_dispatch.modules.door_listings.service import (
    ALLOWED_PHOTO_MIME_TYPES,
    MAX_PHOTO_BYTES,
    PhotoProcessingError,
    compress_for_web,
)
from gdx_dispatch.modules.quote_requests.models import QuoteRequest, QuoteRequestPhoto

__all__ = [
    "ALLOWED_PHOTO_MIME_TYPES",
    "MAX_DOORS",
    "MAX_PHOTOS_PER_DOOR",
    "MAX_PHOTO_BYTES",
    "PhotoProcessingError",
]

MAX_DOORS = 10
MAX_PHOTOS_PER_DOOR = 4

Placement = Literal["replace", "new_opening", "not_sure"]
Material = Literal["steel", "wood", "wood_composite", "aluminum_glass", "not_sure"]
Style = Literal["traditional", "carriage_house", "modern", "not_sure"]
Insulation = Literal["none", "insulated", "best", "not_sure"]
Windows = Literal["none", "top_row", "full_view", "not_sure"]
Opener = Literal["yes", "no", "not_sure"]


def _blank_to_none(v: Any) -> Any:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class DoorIn(BaseModel):
    """One door, in the words a homeowner can answer. Size is the only
    required answer; "approximate is fine" on the form."""

    quantity: int = Field(default=1, ge=1, le=10)
    width_ft: int = Field(ge=4, le=30)
    width_in: int = Field(default=0, ge=0, le=11)
    height_ft: int = Field(ge=5, le=20)
    height_in: int = Field(default=0, ge=0, le=11)
    placement: Placement = "not_sure"
    material: Material = "not_sure"
    style: Style = "not_sure"
    insulation: Insulation = "not_sure"
    windows: Windows = "not_sure"
    color: str | None = Field(default=None, max_length=60)
    opener: Opener = "not_sure"
    notes: str | None = Field(default=None, max_length=1000)

    _blank = field_validator("color", "notes", mode="before")(_blank_to_none)


class QuoteRequestIn(BaseModel):
    job_name: str = Field(min_length=1, max_length=200)
    site_address: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=5000)
    doors: list[DoorIn] = Field(min_length=1, max_length=MAX_DOORS)

    _blank = field_validator("site_address", "notes", mode="before")(_blank_to_none)

    @field_validator("job_name", mode="before")
    @classmethod
    def _strip_job_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class DoorEdit(DoorIn):
    """A door on an edited request. `source_index` is the door's position in
    the request as it stood, so its photos follow it; None is a new door."""

    source_index: int | None = Field(default=None, ge=0, lt=MAX_DOORS)


class QuoteRequestEdit(QuoteRequestIn):
    doors: list[DoorEdit] = Field(min_length=1, max_length=MAX_DOORS)

    @field_validator("doors")
    @classmethod
    def _one_door_per_source(cls, doors: list[DoorEdit]) -> list[DoorEdit]:
        seen = [d.source_index for d in doors if d.source_index is not None]
        if len(seen) != len(set(seen)):
            raise ValueError("Each existing door can appear only once")
        return doors


def size_label(door: dict[str, Any]) -> str:
    """16' 0" x 7' 0" — width first, the trade's order."""
    return (
        f"{door.get('width_ft')}' {door.get('width_in') or 0}\" x "
        f"{door.get('height_ft')}' {door.get('height_in') or 0}\""
    )


def door_count(req: QuoteRequest) -> int:
    """Doors asked for, counting quantity. DoorIn holds quantity to 1..10, so a
    stored door always carries one; read it as stored, never default it."""
    return sum(int(d["quantity"]) for d in req.doors or [])


def lead_notes(req: QuoteRequest) -> str:
    """The lead names the job and points at the request; it does not restate
    the doors (the request is their only copy)."""
    count = door_count(req)
    noun = "door" if count == 1 else "doors"
    return f"Portal quote request: {req.job_name} ({count} {noun}). The doors and photos are on the request."


def origin_ref(req: QuoteRequest) -> str:
    return f"quote_request:{req.id}"


# ── Customer-facing status ───────────────────────────────────────────────────
# The customer may change or withdraw a request until a quote has been sent.
# Once one is out, the quote itself is what they accept or decline, on the
# Estimates tab: a withdraw here would leave that quote live and acceptable.
EDITABLE_STATUSES = frozenset({"Received", "Being priced"})
WITHDRAWABLE_STATUSES = EDITABLE_STATUSES
WITHDRAWN = "Withdrawn"

# Derived from the lead's estimates by the Leads page's own progress helper,
# because Start Estimate and sending a quote never write the lead's `stage`
# (plan audit finding 1). The stage still counts where staff set it by hand:
# a lead marked won or quoted over the phone has no estimate to say so.

_PROGRESS_LABELS = {
    "estimate_started": "Being priced",
    "quoted": "Quote ready",
    "declined": "Declined",
    "expired": "Expired",
}


def customer_status(lead: Any, progress: dict[str, Any] | None, *, withdrawn: bool = False) -> str:
    if withdrawn:
        return WITHDRAWN
    lead_stage = lead.stage if lead is not None else None
    stage = progress.get("stage") if progress else None
    # A cancelled or written-off job: its lead still reads won, because every
    # accept path marks it won (mark_lead_won_for_estimate), so this goes first.
    if progress and stage not in _PROGRESS_LABELS and progress.get("type") == "lost":
        return "Closed"
    if lead_stage == "won":
        return "Accepted"
    if progress:
        if stage in _PROGRESS_LABELS:
            label = _PROGRESS_LABELS[stage]
            if label == "Being priced" and lead_stage in ("quoted", "lost"):
                return "Quote ready" if lead_stage == "quoted" else "Closed"
            return label
        # "sold", or any live state of the job the accepted estimate became.
        return "Accepted"
    if lead_stage == "quoted":
        return "Quote ready"
    if lead_stage == "lost":
        return "Closed"
    return "Received"


# ── Photos ───────────────────────────────────────────────────────────────────


def _upload_root() -> Path:
    """The persisted volume — the door-listing rule (never MOBILE_UPLOAD_DIR)."""
    return Path(os.getenv("UPLOAD_DIR", "/app/uploads"))


def photo_path(tenant_id: str, request_id: UUID | str, filename: str) -> Path:
    """Resolve and fence a request-photo path against the upload root.

    realpath + startswith is the form CodeQL's py/path-injection recognizes;
    the trailing os.sep stops a sibling like "<root>-evil" from passing.
    """
    base = os.path.realpath(_upload_root())
    candidate = os.path.realpath(
        os.path.join(base, str(tenant_id), "quote_requests", str(request_id), os.path.basename(filename))
    )
    if not candidate.startswith(base + os.sep):
        raise ValueError("Invalid quote request photo path")
    return Path(candidate)


def live_photos(req: QuoteRequest) -> list[QuoteRequestPhoto]:
    return [p for p in req.photos if p.deleted_at is None]


def photos_for_door(req: QuoteRequest, door_index: int) -> list[QuoteRequestPhoto]:
    return [p for p in live_photos(req) if p.door_index == door_index]


def store_photo(
    db: Session,
    *,
    tenant_id: str,
    req: QuoteRequest,
    door_index: int,
    data: bytes,
    content_type: str,
) -> QuoteRequestPhoto:
    """Re-encode, write to the persisted volume, and record the row."""
    processed, final_type = compress_for_web(data, content_type)
    ext = "png" if final_type == "image/png" else "jpg"
    filename = f"{uuid4().hex}.{ext}"

    target = photo_path(tenant_id, req.id, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(processed)

    next_order = max((p.sort_order for p in req.photos), default=-1) + 1
    photo = QuoteRequestPhoto(
        quote_request_id=req.id,
        door_index=door_index,
        filename=filename,
        content_type=final_type,
        sort_order=next_order,
    )
    db.add(photo)
    req.photos.append(photo)
    return photo


# ── Edit ─────────────────────────────────────────────────────────────────────


def apply_edit(req: QuoteRequest, payload: QuoteRequestEdit, *, now: datetime) -> dict[str, Any]:
    """Write the customer's changes onto the request and return what changed,
    as {field: {"from": old, "to": new}}, for the audit row and the office alert.

    A photo follows its door to the door's new position; a photo whose door
    was removed is soft-deleted (`removed_photo_ids`), never unlinked.
    """
    old_count = len(req.doors or [])
    if any(d.source_index is not None and d.source_index >= old_count for d in payload.doors):
        raise ValueError("No such door on this request")

    new_doors = [d.model_dump(exclude={"source_index"}) for d in payload.doors]
    changes: dict[str, Any] = {}
    for field in ("job_name", "site_address", "notes"):
        old, new = getattr(req, field), getattr(payload, field)
        if old != new:
            changes[field] = {"from": old, "to": new}
            setattr(req, field, new)
    if new_doors != list(req.doors or []):
        changes["doors"] = {"from": list(req.doors or []), "to": new_doors}

    moved = {d.source_index: i for i, d in enumerate(payload.doors) if d.source_index is not None}
    removed: list[str] = []
    for photo in live_photos(req):
        if photo.door_index in moved:
            photo.door_index = moved[photo.door_index]
        else:
            photo.deleted_at = now
            removed.append(str(photo.id))
    if removed:
        changes["removed_photo_ids"] = removed
    if moved != {i: i for i in range(old_count)} and "doors" not in changes:
        # Same doors in a new order still changes which photo shows which door.
        changes["doors"] = {"from": list(req.doors or []), "to": new_doors}

    req.doors = new_doors
    return changes


# ── Serialization ────────────────────────────────────────────────────────────


def serialize(req: QuoteRequest, *, status: str, lead_id_visible: bool = False) -> dict[str, Any]:
    doors = []
    for i, door in enumerate(req.doors or []):
        doors.append({
            **door,
            "index": i,
            "size_label": size_label(door),
            "photo_ids": [str(p.id) for p in photos_for_door(req, i)],
        })
    out: dict[str, Any] = {
        "id": str(req.id),
        "job_name": req.job_name,
        "site_address": req.site_address,
        "notes": req.notes,
        "doors": doors,
        "doors_version": req.doors_version,
        "status": status,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "edited_at": req.edited_at.isoformat() if req.edited_at else None,
        "withdrawn_at": req.withdrawn_at.isoformat() if req.withdrawn_at else None,
        "can_edit": status in EDITABLE_STATUSES,
        "can_withdraw": status in WITHDRAWABLE_STATUSES,
    }
    if lead_id_visible:
        out["lead_id"] = str(req.lead_id) if req.lead_id else None
        out["customer_id"] = str(req.customer_id)
    return out
