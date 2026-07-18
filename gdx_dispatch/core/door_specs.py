"""Read captured CHI door specs off a job's estimate lines.

When an operator drafts an estimate line from a captured CHI door, the CHI
pricing plugin (gdx_plugin_chi_pricing.router._estimate_line_draft) stuffs the
FULL captured spec into EstimateLine.line_metadata, tagged {"source":
"chi_hubx"}. That JSON is the durable, decoupled carrier of the door build spec:
it rides estimate -> Job (via Estimate.job_id) and is readable by every
downstream consumer — the mobile installer view, the office install sheet, and
PO receiving — WITHOUT any dependency on the (optional, tenant-installed) CHI
plugin or its plug_chipricing_* tables. This module is that read path.

The field split mirrors gdx_plugin_chi_pricing.views.role_views: identity
(who/what/how-much) + installer (the build detail) + receiving (what should
arrive / how heavy) + windows (the Sections/glass rows). Nothing captured is
dropped — every non-internal field lands in exactly one group.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

log = logging.getLogger(__name__)

# The line_metadata.source tag the CHI plugin's estimate-line draft SETS IN
# SOURCE — but the DEPLOYED plugin does not reliably persist it (every captured
# door on prod carries source=null). So the tag is a hint, not the gate; see
# _is_captured_door, which falls back to the capture's signature keys.
CHI_SOURCE = "chi_hubx"

# CHI-capture signature: fields present on every real captured door and absent
# from ordinary line_metadata (a manually-typed line's sku/vendor/color). A
# "Model" plus any one of these is a captured door — this is what makes the
# feature work on real data, not just chi_hubx-tagged rows.
_DOOR_SIGNATURE = ("Spring", "Track", "Cyclage", "Sections", "Sprung Weight")

# Ordered identity fields — who/what/how-much, shown first on every surface.
IDENTITY_FIELDS = (
    "Number", "Cart Name", "Model", "Description",
    "Size", "Width", "Height", "Color", "Price",
)
# What receiving cares about: what should arrive + how heavy.
RECEIVING_FIELDS = ("Load Information", "Sprung Weight", "Shipping Weight")
# The windows / glass section rows (a list of section dicts).
WINDOWS_KEY = "Sections"


def _is_internal(key: str) -> bool:
    """Keys the capture carries for its own bookkeeping, never shown downstream:
    the ``source`` tag and any ``_``-prefixed key (``_raw`` page text, ``_url``,
    ``_image`` — the photo rides as a separate estimate Document, not here)."""
    return key.startswith("_") or key == "source"


def _present(value: Any) -> bool:
    return value not in (None, "")


def _is_captured_door(md: Any) -> bool:
    """True if this estimate line's line_metadata is a captured CHI door.

    Prefer the explicit source tag, but fall back to the capture's signature
    keys because the deployed plugin leaves source=null on real captures — the
    full spec (Model, Spring, Track, Sections, weights) is all there, just
    untagged. Ordinary line_metadata (sku/vendor/color from a manual line) has
    no Model + build field, so it's excluded.
    """
    if not isinstance(md, dict):
        return False
    if md.get("source") == CHI_SOURCE:
        return True
    return "Model" in md and any(k in md for k in _DOOR_SIGNATURE)


def _door_from_metadata(md: dict, line: EstimateLine) -> dict:
    """Split one captured spec dict into identity / installer / receiving /
    windows, carrying the estimate line's id + quantity for the caller."""
    identity = {k: md[k] for k in IDENTITY_FIELDS if k in md and _present(md[k])}
    receiving = {k: md[k] for k in RECEIVING_FIELDS if k in md and _present(md[k])}
    windows = md.get(WINDOWS_KEY) or []
    if not isinstance(windows, list):
        windows = [windows]
    claimed = set(IDENTITY_FIELDS) | set(RECEIVING_FIELDS) | {WINDOWS_KEY}
    # Everything left over is the build detail — Spring, Track, Cyclage, Rollers,
    # Drum, Wire/Cable, Shaft, Spring Turns … including any field CHI adds later.
    installer = {
        k: v for k, v in md.items()
        if k not in claimed and not _is_internal(k) and _present(v)
    }
    label = md.get("Number") or md.get("Cart Name") or getattr(line, "label", None)
    return {
        "line_id": str(getattr(line, "id", "") or ""),
        "label": label,
        "description": getattr(line, "description", None),
        "quantity": int(getattr(line, "quantity", 1) or 1),
        "identity": identity,
        "installer": installer,
        "receiving": receiving,
        "windows": windows,
    }


def door_specs_for_job(db: Session, job_id: Any) -> list[dict]:
    """Captured CHI doors attached to this job, via its latest estimate.

    Returns [] when the job has no linked estimate, no CHI-sourced lines, or an
    unparseable job_id. One dict per captured door line, role-split. The estimate
    is resolved exactly as the office install sheet resolves it: the newest
    non-deleted Estimate whose job_id points at this job.
    """
    try:
        job_uuid = job_id if isinstance(job_id, UUID) else UUID(str(job_id))
    except (ValueError, AttributeError, TypeError):
        return []

    estimate = db.execute(
        select(Estimate)
        .where(Estimate.job_id == job_uuid, Estimate.deleted_at.is_(None))
        .order_by(Estimate.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if estimate is None:
        return []

    lines = db.execute(
        select(EstimateLine)
        .where(EstimateLine.estimate_id == estimate.id)
        .order_by(EstimateLine.id)
    ).scalars().all()

    doors: list[dict] = []
    for line in lines:
        if _is_captured_door(line.line_metadata):
            doors.append(_door_from_metadata(line.line_metadata, line))
    return doors


def receiving_view(door: dict) -> dict:
    """The receiving slice of a captured door: what should arrive + how heavy.

    Drops the installer build detail (spring/track/rollers) — the person checking
    in the delivery cares about identity + weights + quantity, not how it gets
    hung. Used by the PO receive flow to show what a door PO should bring in."""
    return {
        "line_id": door.get("line_id"),
        "label": door.get("label"),
        "quantity": door.get("quantity", 1),
        "identity": door.get("identity") or {},
        "receiving": door.get("receiving") or {},
        "window_count": len(door.get("windows") or []),
    }


def _stringify(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items())
    if isinstance(value, list):
        return "; ".join(_stringify(x) for x in value)
    return str(value)


def flatten_door_spec(door: dict) -> dict:
    """A flat ``{label: str}`` view for the legacy key/value grid (the office
    install tab renders ``door_specs`` as a plain dict). Nested Load Information
    and window rows collapse to readable strings so nothing renders as
    ``[object Object]``."""
    flat: dict[str, str] = {}
    for group in ("identity", "installer", "receiving"):
        for k, v in (door.get(group) or {}).items():
            flat[k] = _stringify(v)
    windows = door.get("windows") or []
    if windows:
        flat["Windows"] = f"{len(windows)} section(s)"
    return flat
