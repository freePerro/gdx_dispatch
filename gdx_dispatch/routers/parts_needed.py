"""Parts Needed — track parts to order for jobs.

Phase 1.3 (sprint_tech_mobile) extensions:

* C1 — modal-friendly create accepts ``sku`` (canonical SKU, optional),
  ``photo_url`` (URL the tech captured), and stamps ``requested_by_user_id``
  so multi-tech jobs preserve attribution.
* C3 — tech-side edit endpoint accepts edits ONLY while ``status='needed'``;
  once the office flips to ``ordered`` / ``received`` the request is
  immutable from the tech.
* C6 — dispatcher PATCH accepts an optional ``eta_at`` so the tech sees
  the office's promised arrival on their card.
* C7 — SKU autocomplete pulls tenant-scoped from ``inventory.parts`` (the
  tenant's own catalog) AND ``chi_door_catalog`` (door-line catalog).
  Free-text fallback: when the catalog has no match, the modal still
  posts the tech's typed name. ``sku`` stays NULL in that case.
* C8 — permission/role gates:
    - create + tech-side edit: ``inventory.write`` (technician role default).
    - status / ETA changes: ``inventory.write`` AND dispatcher / admin /
      owner role (techs cannot self-flip ``ordered`` / ``received``).
    - read views: ``inventory.read``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.permissions import is_dispatch_manager
from gdx_dispatch.models.tenant_models import JobPartNeeded
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api",
    tags=["parts-needed"],
    dependencies=[Depends(require_module("jobs"))],
)


_TECH_EDITABLE_STATUS = "needed"


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


def _require_dispatch_role(user: dict) -> None:
    # Shared dispatch-manager predicate (core/roles via core/permissions):
    # owner/admin/dispatcher/manager/superadmin, variant-aware so the legacy
    # 'dispatch' spelling stored in users.role is honored too.
    if not is_dispatch_manager(user):
        raise HTTPException(
            status_code=403,
            detail="status / ETA changes require dispatcher, admin, or owner role",
        )


class PartNeededIn(BaseModel):
    part_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1, le=999)
    supplier: str = Field(default="", max_length=200)
    urgency: str = Field(default="normal", pattern="^(normal|urgent|critical)$")
    notes: str = Field(default="", max_length=1000)
    # Phase 1.3 C1 — optional catalog-resolved SKU + tech-captured photo URL.
    sku: str | None = Field(default=None, max_length=255)
    photo_url: str | None = Field(default=None, max_length=2000)
    # Catalog-picker intake — carries the catalog SELL price so a part queued
    # from a catalog reaches the invoice-create checklist pre-priced (the
    # LineItemEditor pull prefills the invoice line's unit_price from this).
    # NULL = office prices it on the invoice, matching the free-text flow.
    unit_price: float | None = Field(default=None, ge=0, le=999999.99)


class PartNeededTechUpdate(BaseModel):
    """Tech-side edit while status='needed'. None = leave field unchanged."""

    part_name: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: int | None = Field(default=None, ge=1, le=999)
    supplier: str | None = Field(default=None, max_length=200)
    urgency: str | None = Field(default=None, pattern="^(normal|urgent|critical)$")
    notes: str | None = Field(default=None, max_length=1000)
    # 255 to match the column and the catalogs it's picked from — see the
    # create schema above and migration 028.
    sku: str | None = Field(default=None, max_length=255)
    photo_url: str | None = Field(default=None, max_length=2000)


class PartStatusUpdate(BaseModel):
    """Dispatcher-side status (and optional ETA) change. ``status`` is
    required to keep the legacy single-purpose contract; ``eta_at`` is the
    Phase 1.3 C6 addition.

    PR4-billing-capture adds ``wont_bill`` — the office's dismiss verb for
    the "parts used, never billed" review card (warranty part, goodwill,
    already covered by a flat price). Without a dismiss path the leak card
    floods and becomes wallpaper (audit round 2). ``wont_bill`` rows leave
    every billing checklist/report but keep their audit trail."""

    status: str = Field(pattern="^(needed|ordered|received|used|wont_bill)$")
    eta_at: datetime | None = None


def _serialize(p: JobPartNeeded) -> dict[str, Any]:
    return {
        "id": p.id,
        "job_id": p.job_id,
        "part_name": p.part_name,
        "quantity": p.quantity,
        "supplier": p.supplier,
        "urgency": p.urgency,
        "status": p.status,
        "notes": p.notes,
        "sku": p.sku,
        "photo_url": p.photo_url,
        "requested_by_user_id": p.requested_by_user_id,
        "eta_at": p.eta_at.isoformat() if p.eta_at else None,
        # S122 — surface billing linkage so the invoice-create checklist can
        # render an "already billed on INV-…" badge instead of just hiding the
        # row. Frontend uses this for the badge; server still enforces the
        # unbilled filter for the picker default.
        "billed_invoice_id": str(p.billed_invoice_id) if p.billed_invoice_id else None,
        # PR4-billing-capture — capture provenance + suggested sell price.
        # source: request | closeout | mobile | van (checklist badge);
        # unit_price: catalog sell price at capture, NULL = office prices it.
        "source": getattr(p, "source", None) or "request",
        "unit_price": float(p.unit_price) if getattr(p, "unit_price", None) is not None else None,
        "created_at": str(p.created_at) if p.created_at else None,
        "updated_at": str(p.updated_at) if p.updated_at else None,
    }


@router.post(
    "/jobs/{job_id}/parts-needed",
    status_code=201,
    dependencies=[Depends(require_permission("inventory.write"))],
)
def add_part_needed(
    job_id: str,
    request: Request,
    payload: PartNeededIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    part_id = str(uuid4())
    now = datetime.now(timezone.utc)
    part = JobPartNeeded(
        id=part_id,
        company_id=tid,
        job_id=job_id,
        part_name=payload.part_name,
        quantity=payload.quantity,
        supplier=payload.supplier,
        urgency=payload.urgency,
        status="needed",
        notes=payload.notes,
        sku=payload.sku or None,
        photo_url=payload.photo_url or None,
        unit_price=payload.unit_price,
        requested_by_user_id=uid,
        created_at=now,
        updated_at=now,
    )
    db.add(part)
    db.commit()
    log_audit_event_sync(
        db,
        tenant_id=tid,
        user_id=uid,
        action="create",
        entity_type="part_needed",
        entity_id=part_id,
        details={
            "job_id": job_id,
            "part": payload.part_name,
            "qty": payload.quantity,
            "sku": payload.sku,
            "urgency": payload.urgency,
        },
        request=request,
    )

    # Phase 1.5 — C5 push upgrade. Critical-urgency part on create →
    # fan-out push to every dispatcher / admin / owner with an active
    # subscription. PartsToOrderView's 30s poll + WebAudio ping (C5
    # in-app fallback) is still the path for office users without push.
    try:
        if payload.urgency == "critical":
            from gdx_dispatch.core.push_subscriptions import send_push

            # Resolve dispatcher-class user IDs via the tenant User table.
            # User.role is canonical (set by tenant admin via /role-permissions);
            # role-permission overrides on tenant role rows aren't checked here
            # because critical-part fan-out is intentionally broad — anyone
            # who can act on it should hear about it.
            recipients = db.execute(
                text(
                    "SELECT DISTINCT id FROM users "
                    "WHERE role IN ('dispatcher', 'dispatch', 'admin', 'owner') "
                    "  AND (deleted_at IS NULL) "
                    "  AND (active IS NULL OR active = :t)"
                ),
                {"t": True},
            ).scalars().all()
            for rid in recipients:
                send_push(
                    db,
                    user_id=str(rid),
                    title="Critical part flagged",
                    body=f"{payload.part_name} (×{payload.quantity}) — needs attention.",
                    url="/parts-to-order",
                    data={"job_id": job_id, "part_id": part_id, "urgency": "critical"},
                )
            db.commit()
    except Exception:
        log.exception("critical_part_push_send_failed")

    return _serialize(part)


@router.get(
    "/jobs/{job_id}/parts-needed",
    dependencies=[Depends(require_permission("inventory.read"))],
)
def list_job_parts(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = None,
    unbilled: bool = False,
) -> list[dict[str, Any]]:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    # S122 — optional status + unbilled filters so the invoice-create checklist
    # can ask narrowly ("ordered+received and not yet billed").
    stmt = select(JobPartNeeded).where(JobPartNeeded.job_id == job_id)
    if status:
        wanted = {s.strip() for s in status.split(",") if s.strip()}
        if wanted:
            stmt = stmt.where(JobPartNeeded.status.in_(wanted))
    if unbilled:
        stmt = stmt.where(JobPartNeeded.billed_invoice_id.is_(None))
    parts = db.execute(stmt.order_by(JobPartNeeded.created_at)).scalars().all()
    return [_serialize(p) for p in parts]


@router.patch(
    "/parts-needed/{part_id}",
    dependencies=[Depends(require_permission("inventory.write"))],
)
def tech_edit_part(
    part_id: str,
    request: Request,
    payload: PartNeededTechUpdate,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """C3 — tech edits while status='needed'.

    Once dispatch flips ``ordered`` / ``received`` the row is locked from
    the tech side; only the dispatcher status endpoint can touch it.
    Dispatcher / admin / owner can still edit at any status (so they can
    correct typos for the tech).
    """
    tid = _tid(request)
    uid = _uid(user)
    part = db.execute(
        select(JobPartNeeded).where(JobPartNeeded.id == part_id)
    ).scalar_one_or_none()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")

    is_dispatch = is_dispatch_manager(user)
    if not is_dispatch and part.status != _TECH_EDITABLE_STATUS:
        raise HTTPException(
            status_code=409,
            detail=f"Part is {part.status}; tech edits are locked once dispatch acts on it.",
        )

    changes: dict[str, Any] = {}
    for field in ("part_name", "quantity", "supplier", "urgency", "notes", "sku", "photo_url"):
        new_val = getattr(payload, field)
        if new_val is None:
            continue
        old_val = getattr(part, field)
        if new_val != old_val:
            setattr(part, field, new_val)
            changes[field] = {"from": old_val, "to": new_val}
    if not changes:
        return _serialize(part)

    part.updated_at = datetime.now(timezone.utc)
    db.commit()
    log_audit_event_sync(
        db,
        tenant_id=tid,
        user_id=uid,
        action="update",
        entity_type="part_needed",
        entity_id=part_id,
        details={"job_id": part.job_id, "changes": changes},
        request=request,
    )
    return _serialize(part)


@router.patch(
    "/parts-needed/{part_id}/status",
    dependencies=[Depends(require_permission("inventory.write"))],
)
def update_part_status(
    part_id: str,
    request: Request,
    payload: PartStatusUpdate,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """C6 + C8 — dispatch flips status and (optionally) sets the ETA the
    tech sees on their card. Restricted to dispatcher / admin / owner."""
    _require_dispatch_role(user)
    tid = _tid(request)
    part = db.execute(
        select(JobPartNeeded).where(JobPartNeeded.id == part_id)
    ).scalar_one_or_none()
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    job_id = str(part.job_id)

    now = datetime.now(timezone.utc)
    old_status = part.status
    old_eta = part.eta_at
    part.status = payload.status
    if payload.eta_at is not None:
        part.eta_at = payload.eta_at
    part.updated_at = now
    db.commit()

    log_audit_event_sync(
        db,
        tenant_id=tid,
        user_id=_uid(user),
        action="update",
        entity_type="part_needed",
        entity_id=part_id,
        details={
            "status": {"from": old_status, "to": payload.status},
            "eta_at": (
                {
                    "from": old_eta.isoformat() if old_eta else None,
                    "to": payload.eta_at.isoformat() if payload.eta_at else None,
                }
                if payload.eta_at is not None
                else None
            ),
            "job_id": job_id,
        },
        request=request,
    )

    # Phase 1.5 — C4 push upgrade. If status flipped to ordered/received
    # AND the requesting tech has subscribed to push, send the
    # notification. The MobileTodayView in-app badge fallback (C4)
    # remains the path for techs without push permission. Failures are
    # logged + swallowed; never break the dispatcher's status flip on a
    # downstream push problem.
    try:
        if old_status != payload.status and payload.status in ("ordered", "received"):
            from gdx_dispatch.core.push_subscriptions import send_push

            recipient = part.requested_by_user_id
            if recipient:
                send_push(
                    db,
                    user_id=recipient,
                    title=f"Part {payload.status}",
                    body=f"{part.part_name} is {payload.status}.",
                    url="/mobile",
                    data={"job_id": job_id, "part_id": part_id, "status": payload.status},
                )
                db.commit()
    except Exception:
        log.exception("part_status_push_send_failed")

    return _serialize(part)


@router.get(
    "/parts-needed/pending",
    dependencies=[Depends(require_permission("inventory.read"))],
)
def pending_parts(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """All pending parts across all jobs — the office's order list."""
    tid = _tid(request)
    # Multi-table join with CASE ordering — kept as text() per policy
    rows = db.execute(
        text(
            """SELECT p.id, p.part_name, p.quantity, p.supplier, p.urgency, p.status,
                       p.notes, p.sku, p.photo_url, p.eta_at, p.requested_by_user_id,
                       p.job_id, p.created_at,
                       j.title as job_title, c.name as customer_name
                FROM job_parts_needed p
                LEFT JOIN jobs j ON CAST(p.job_id AS TEXT) = CAST(j.id AS TEXT)
                LEFT JOIN customers c ON CAST(j.customer_id AS TEXT) = CAST(c.id AS TEXT)
                WHERE p.company_id = :tid AND p.status IN ('needed', 'ordered')
                ORDER BY
                    CASE p.urgency WHEN 'critical' THEN 1 WHEN 'urgent' THEN 2 ELSE 3 END,
                    p.created_at"""
        ),
        {"tid": tid},
    ).mappings().all()
    return [dict(r) for r in rows]


@router.get(
    "/parts-needed/unbilled-consumed",
    dependencies=[Depends(require_permission("invoices.read_all"))],
)
def unbilled_consumed_parts(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """PR4-billing-capture — 'consumed but not billed': parts recorded as
    used or received on COMPLETED jobs that never reached an invoice,
    grouped per job. Catches the case Ready-to-Bill misses: the invoice
    already went out, THEN the tech logged parts (or the office pulled the
    estimate lines and skipped the parts checklist). Rendered on the
    BillingView review section.
    """
    from uuid import UUID as _UUID

    from gdx_dispatch.models.tenant_models import Customer, Job

    # Two-step on purpose: JobPartNeeded.job_id is String(36) while Job.id is
    # a native Uuid — a direct SQL join needs a cast on PG and breaks on the
    # SQLite test path. Cap 2000 unbilled rows — far above any real shop's
    # leak backlog; if it ever hits, scream in the logs (audit round 2: the
    # earlier comment claimed loudness the code didn't have).
    parts = db.execute(
        select(JobPartNeeded)
        .where(
            JobPartNeeded.billed_invoice_id.is_(None),
            JobPartNeeded.status.in_(("used", "received")),
        )
        .order_by(JobPartNeeded.created_at.asc())
        .limit(2000)
    ).scalars().all()
    if len(parts) >= 2000:
        log.warning(
            "unbilled_consumed_parts_cap_hit rows=2000 — report is TRUNCATED "
            "(oldest-first window); the leak backlog exceeds the cap"
        )
    if not parts:
        return []

    job_uuids = set()
    for p in parts:
        try:
            job_uuids.add(_UUID(str(p.job_id)))
        except (ValueError, AttributeError):
            continue
    jobs = {
        str(j.id): (j, cust)
        for j, cust in db.execute(
            select(Job, Customer)
            .outerjoin(Customer, Customer.id == Job.customer_id)
            .where(
                Job.id.in_(job_uuids),
                Job.lifecycle_stage == "completed",
                Job.deleted_at.is_(None),
            )
        ).all()
    }

    by_job: dict[str, dict[str, Any]] = {}
    for part in parts:
        key = str(part.job_id)
        if key not in jobs:
            continue  # job not completed (or deleted) — not leak-review yet
        job, customer = jobs[key]
        entry = by_job.setdefault(key, {
            "job_id": key,
            "job_title": job.title or "",
            "customer_name": customer.name if customer else "",
            "completed_at": str(job.completed_at) if job.completed_at else None,
            "parts": [],
            "suggested_total": 0.0,
        })
        entry["parts"].append(_serialize(part))
        if part.unit_price is not None:
            entry["suggested_total"] = round(
                entry["suggested_total"] + float(part.unit_price) * int(part.quantity or 1), 2
            )
    return list(by_job.values())


@router.get(
    "/parts-needed/dispatch-config",
    dependencies=[Depends(require_permission("inventory.read"))],
)
def dispatch_config(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """C5 in-app fallback — config for the dispatch parts list.

    Returns the per-tenant ``tech_mobile.critical_part_audible`` flag so
    PartsToOrderView can gate its audible ping. Push-infra-free fallback
    until Sprint 1.5 lands.
    """
    from gdx_dispatch.core.tenant_mobile_settings import get_tenant_mobile_setting

    audible = get_tenant_mobile_setting(
        db, "tech_mobile.critical_part_audible", default=True, request=request
    )
    return {"audible_critical": bool(audible)}


@router.get(
    "/parts-needed/sku-suggest",
    dependencies=[Depends(require_permission("inventory.read"))],
)
def sku_suggest(
    request: Request,
    q: str = Query(default="", min_length=0, max_length=64),
    limit: int = Query(default=10, ge=1, le=50),
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """SKU autocomplete — the same catalog the estimate builder searches.

    Sources, in suggestion priority. **Parts before doors**: a tech mid-job
    wants a spring, not a door, and the door rows carry long marketing
    descriptions that match almost any needle and crowd everything else out
    of ``limit``.

      1. ``inventory.parts`` — tenant bench/truck stock, the only source that
         carries a real ``part_id`` (see ``source`` note below).
      2. ``custom_catalog_items`` (non-door) — the tenant's own catalog, and
         where the real parts actually are: 2,561 ``product_class='parts'``
         rows in production.
      3. ``chi_parts_catalog`` — CHI's parts line (springs, rollers, cable).
      4. ``chi_door_catalog`` — CHI's door line.
      5. ``custom_catalog_items`` (``product_class='door'``) — custom doors.

    Sources 2 and 3 were unreachable before 2026-07-16, which made this
    endpoint incapable of suggesting a part. Source 2 was filtered to
    ``product_class == 'door'`` — a value that matches **zero** rows in
    production — and source 3 was never queried at all. With ``parts`` empty,
    the only source that could return anything was the door catalog: searching
    "spring" returned 8 doors whose marketing copy says "spring" while all 90
    real springs (12 custom + 78 CHI) stayed invisible.

    ``source`` is load-bearing, not a label: MobileJobCloseoutDialog keeps
    ``part_id`` only when ``source == 'parts'``, because ``job_parts.part_id``
    is an FK to ``parts.id`` and rejects an id from any other table. Catalog
    rows therefore ride as ``source='catalog'`` with no ``part_id`` and are
    snapshotted by value.

    No match → caller falls back to free-text typing; the create endpoint
    accepts ``sku=null`` and the request is still saved.
    """
    needle = (q or "").strip()
    if not needle:
        return []
    like = f"%{needle.lower()}%"

    # Lazy import — chi_door_catalog lives next to JobPartNeeded but Part is
    # in the inventory module. Importing at module top would couple the
    # parts router to the inventory module's import side-effects.
    from gdx_dispatch.models.tenant_models import (
        ChiDoorCatalog,
        ChiPartsCatalog,
        CustomCatalogItem,
        DoorSpec,
    )
    from gdx_dispatch.modules.inventory.models import Part

    suggestions: list[dict[str, Any]] = []
    seen_skus: set[str] = set()

    def _add(row: dict[str, Any]) -> None:
        """Append unless a higher-priority source already claimed this sku.

        The same sku legitimately exists in more than one catalog (a CHI part
        the tenant also stocks). First writer wins, which is why source order
        above is priority order.
        """
        sku = (row.get("sku") or "").strip().lower()
        if sku and sku in seen_skus:
            return
        if sku:
            seen_skus.add(sku)
        suggestions.append(row)

    def _name(*candidates: Any) -> str:
        """First non-empty candidate, trimmed to something a phone can show.

        pickSuggestion() assigns this straight to the part name it saves, so an
        untrimmed value is not just ugly — it is what lands in the job record.
        The CHI door rows carry ~900-char marketing paragraphs in
        ``description``, which is how a part came to be named three sentences
        about curb appeal.
        """
        for c in candidates:
            text = str(c).strip() if c is not None else ""
            if text:
                return text if len(text) <= 120 else text[:117].rstrip() + "..."
        return ""

    # Source 1 — parts catalog. Three-plane: tenant connection IS the filter.
    part_rows = (
        db.query(Part)
        .filter(
            Part.deleted_at.is_(None),
            or_(
                Part.sku.ilike(like) if hasattr(Part.sku, "ilike") else Part.sku.like(like),
                Part.name.ilike(like) if hasattr(Part.name, "ilike") else Part.name.like(like),
                Part.vendor_sku.ilike(like)
                if hasattr(Part.vendor_sku, "ilike")
                else Part.vendor_sku.like(like),
            ),
        )
        .order_by(Part.sku.asc())
        .limit(limit)
        .all()
    )
    for p in part_rows:
        _add(
            {
                "source": "parts",
                # Phase 2 / C5 (Doug 2026-05-10): the closeout dialog
                # writes a JobPart row when the picked suggestion is
                # inventory-tracked (source='parts'). The job_parts FK
                # to parts.id rejects synthetic UUIDs, so we pass the
                # real Part.id back here. door_catalog and custom_door
                # rows don't get part_id — they live in the closeout
                # snapshot only.
                "part_id": str(p.id),
                "sku": p.sku,
                "name": p.name,
                "vendor": p.vendor_name,
                "vendor_sku": p.vendor_sku,
                "qty_on_hand": p.qty_on_hand,
            }
        )

    # Source 2 — the tenant's own catalog, everything that is not a door.
    # This is the same table the estimate builder searches via
    # /api/catalogs/all-items, which is Doug's rule for this picker: the tech
    # searches what the estimate searches.
    #
    # No NULL guard on product_class: the column is NOT NULL with a 'parts'
    # server default, verified on prod — a NULL leg here would be dead code.
    remaining = max(0, limit - len(suggestions))
    if remaining:
        custom_part_rows = (
            db.query(CustomCatalogItem)
            .filter(
                CustomCatalogItem.deleted_at.is_(None),
                CustomCatalogItem.active.is_(True),
                CustomCatalogItem.product_class != "door",
                or_(
                    CustomCatalogItem.sku.ilike(like),
                    CustomCatalogItem.name.ilike(like),
                    CustomCatalogItem.description.ilike(like),
                    CustomCatalogItem.category.ilike(like),
                ),
            )
            .order_by(CustomCatalogItem.sku.asc())
            .limit(remaining)
            .all()
        )
        for cci in custom_part_rows:
            _add(
                {
                    # Not 'parts': job_parts.part_id is an FK to parts.id and a
                    # custom_catalog_items id would violate it. 'catalog' keeps
                    # part_id null and snapshots by value.
                    "source": "catalog",
                    "sku": cci.sku,
                    # name first — it is the short human label ("#4 hinge");
                    # description is where the paragraphs live.
                    "name": _name(cci.name, cci.description, cci.sku),
                    "vendor": cci.vendor,
                    "category": cci.category,
                    "qty_on_hand": None,
                }
            )

    # Source 3 — CHI's parts line. Never queried before 2026-07-16; it holds
    # 78 of the 90 springs in the system.
    remaining = max(0, limit - len(suggestions))
    if remaining:
        chi_part_rows = (
            db.query(ChiPartsCatalog)
            .filter(
                ChiPartsCatalog.is_active.is_(True),
                or_(
                    ChiPartsCatalog.sku.ilike(like),
                    ChiPartsCatalog.name.ilike(like),
                    ChiPartsCatalog.description.ilike(like),
                    ChiPartsCatalog.part_type.ilike(like),
                ),
            )
            .order_by(ChiPartsCatalog.sku.asc())
            .limit(remaining)
            .all()
        )
        for cp in chi_part_rows:
            _add(
                {
                    "source": "catalog",
                    "sku": cp.sku,
                    "name": _name(cp.name, cp.description, cp.sku),
                    "vendor": cp.brand or cp.manufacturer,
                    "category": cp.part_type,
                    "qty_on_hand": None,
                }
            )

    # Source 4 — CHI doors. Behind the parts sources now: these descriptions
    # are marketing paragraphs that match nearly any needle, so in front they
    # ate the whole `limit`.
    remaining = max(0, limit - len(suggestions))
    if remaining:
        door_rows = (
            db.query(ChiDoorCatalog)
            .filter(
                ChiDoorCatalog.is_active.is_(True),
                or_(
                    ChiDoorCatalog.sku.ilike(like)
                    if hasattr(ChiDoorCatalog.sku, "ilike")
                    else ChiDoorCatalog.sku.like(like),
                    ChiDoorCatalog.model_number.ilike(like)
                    if hasattr(ChiDoorCatalog.model_number, "ilike")
                    else ChiDoorCatalog.model_number.like(like),
                    ChiDoorCatalog.description.ilike(like)
                    if hasattr(ChiDoorCatalog.description, "ilike")
                    else ChiDoorCatalog.description.like(like),
                ),
            )
            .order_by(ChiDoorCatalog.sku.asc())
            .limit(remaining)
            .all()
        )
        for d in door_rows:
            _add(
                {
                    "source": "door_catalog",
                    "sku": d.sku,
                    # model_number ahead of description: the description is the
                    # marketing paragraph, and pickSuggestion() saves this as
                    # the part name.
                    "name": _name(d.model_number, d.description, d.sku),
                    "vendor": d.brand or d.manufacturer,
                    "model_number": d.model_number,
                    "qty_on_hand": None,
                }
            )

    # Sprint typed-catalogs follow-up — also surface tenant-custom doors
    # (CustomCatalogItem with product_class='door' joined to DoorSpec).
    remaining = max(0, limit - len(suggestions))
    if remaining:
        custom_door_rows = (
            db.query(CustomCatalogItem, DoorSpec)
            .outerjoin(DoorSpec, DoorSpec.catalog_item_id == CustomCatalogItem.id)
            .filter(
                CustomCatalogItem.product_class == "door",
                CustomCatalogItem.deleted_at.is_(None),
                CustomCatalogItem.active.is_(True),
                or_(
                    CustomCatalogItem.sku.ilike(like)
                    if hasattr(CustomCatalogItem.sku, "ilike")
                    else CustomCatalogItem.sku.like(like),
                    CustomCatalogItem.name.ilike(like)
                    if hasattr(CustomCatalogItem.name, "ilike")
                    else CustomCatalogItem.name.like(like),
                    CustomCatalogItem.description.ilike(like)
                    if hasattr(CustomCatalogItem.description, "ilike")
                    else CustomCatalogItem.description.like(like),
                ),
            )
            .order_by(CustomCatalogItem.sku.asc())
            .limit(remaining)
            .all()
        )
        for cci, ds in custom_door_rows:
            _add(
                {
                    "source": "door_catalog",
                    "sku": cci.sku,
                    "name": _name(cci.name, cci.description, cci.sku),
                    "vendor": (ds.manufacturer if ds else None),
                    "model_number": (ds.model_number if ds else None),
                    "qty_on_hand": None,
                }
            )

    return suggestions
