from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy import text as _text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.job_display_state import derive_job_display_state
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobAssignment,
    JobCloseout,
    JobDependency,
    TimeEntry,
)
from gdx_dispatch.modules.dispatch_settings import require_tech_for_scheduled_job
from gdx_dispatch.modules.numbering import next_job_number

try:
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
    _HAS_ESTIMATE_ORM = True
except ImportError:
    logging.getLogger(__name__).warning("jobs_estimate_orm_import_failed")
    _HAS_ESTIMATE_ORM = False

log = logging.getLogger(__name__)

try:
    from gdx_dispatch.routers.auth import get_current_user
except ImportError:
    log.exception("jobs_auth_import_failed_using_fallback")
    async def get_current_user() -> dict[str, Any]: return {}

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_module("jobs"))])

class JobCreate(BaseModel):
    # title: free-form, bounded against DoS. 200 chars fits the DB column.
    # status/job_type/priority are enum-ish values — tight upper bound.
    title: str | None = Field(default=None, max_length=200)
    customer_id: str | None = Field(default=None, max_length=36)
    scheduled_at: datetime | None = None
    status: str = Field(default="Scheduled", min_length=1, max_length=50)
    job_type: str = Field(default="Service", min_length=1, max_length=50)
    priority: str = Field(default="Normal", min_length=1, max_length=50)
    assigned_tech_id: str | None = Field(default=None, max_length=36)
    assigned_to: str | None = Field(default=None, max_length=36)
    # Multi-tech crew dispatch (Phase 1.4 D1). Either pass the singular
    # ``assigned_tech_id`` (legacy) or this list. The first id (or
    # ``lead_tech_id`` if explicitly set) becomes the lead.
    assigned_tech_ids: list[str] | None = Field(default=None, max_length=20)
    lead_tech_id: str | None = Field(default=None, max_length=36)
    # Optional explicit dispatch routing. create_job reads this to honor
    # callers that pre-assign a holding area; absent it, service calls
    # auto-route to "Ready to Schedule". Mirrors JobUpdate.holding_area_id.
    # Added 2026-05-19: 2e41cc45 wired payload.holding_area_id into
    # create_job but only added the field to JobUpdate, 500-ing every
    # POST /api/jobs since 2026-05-13.
    holding_area_id: str | None = Field(default=None, max_length=36)
    # Sprint dispatch-capacity (2026-05-20) — scheduler's expected duration
    # in decimal hours. Distinct from the estimate-derived duration; the
    # dispatch board prefers this when set, falls back to estimate calc.
    scheduled_duration_hours: Decimal | None = Field(default=None, ge=0, le=240)
    # Sprint customer-multi-location (2026-05-21) — optional pick of which
    # customer_locations row this job is at. NULL → JobDetailView falls
    # back to the customer's primary location (existing behavior).
    location_id: str | None = Field(default=None, max_length=36)


class JobUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    customer_id: str | None = None
    scheduled_at: datetime | None = None
    status: str | None = None
    lifecycle_stage: str | None = None
    job_type: str | None = None
    priority: str | None = None
    notes: str | None = None
    assigned_tech_id: str | None = None
    assigned_to: str | None = None
    assigned_tech_ids: list[str] | None = Field(default=None, max_length=20)
    lead_tech_id: str | None = Field(default=None, max_length=36)
    holding_area_id: str | None = None
    scheduled_duration_hours: Decimal | None = Field(default=None, ge=0, le=240)
    location_id: str | None = Field(default=None, max_length=36)

def jsonable_response(content: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))

# Canonical job-status display labels. The source of truth is the
# `jobs.lifecycle_stage` PG enum (lead/estimate/scheduled/in_progress/
# completed/cancelled). Phase D audit 2026-04-27: legacy entries for
# "Sold" and "Invoiced" were filtered to but never produced — Sold isn't
# in the enum at all, and "Invoiced" lives in `billing_status`, not
# lifecycle. Both are removed to keep one canonical taxonomy.
_STATUS_CANON = {
    # "lead" is retained as a graceful display fallback for any row that
    # predates the 2026-05-13 migration; the system no longer emits it.
    "lead": "Lead",
    "service_call": "Service Call",
    "service call": "Service Call",
    "estimate": "Estimate",
    "scheduled": "Scheduled",
    "in progress": "In Progress",
    "in_progress": "In Progress",
    "complete": "Complete",
    "completed": "Complete",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
}


def _canon_status(*candidates: Any) -> str:
    for c in candidates:
        if c:
            s = str(c).strip().lower()
            if s in _STATUS_CANON:
                return _STATUS_CANON[s]
            if s:
                return str(c).strip().title()
    return "Unknown"


def _user_id(current_user: dict | None) -> str:
    u = current_user or {}
    return str(u.get("sub") or u.get("user_id") or "system")


def _validate_location_for_customer(
    db: Any, location_id: str | None, customer_id: str | None
) -> tuple[bool, str | None]:
    """Confirm location_id belongs to customer_id (or is None).

    Returns (ok, detail). Sprint customer-multi-location (2026-05-21): the
    UI restricts the picker to the chosen customer's locations, but a
    crafted POST could still attach a peer-customer's location, leaking
    the relationship across customer rows. Same shape as the holding-area
    phantom-lane guard in create_job. Empty/NULL location_id is allowed
    (the JobDetailView fallback path handles it).
    """
    if not location_id:
        return True, None
    if not customer_id:
        return False, "location_id requires a customer_id"
    row = db.execute(
        _text(
            "SELECT 1 FROM customer_locations "
            "WHERE id = :lid AND customer_id = :cid AND deleted_at IS NULL"
        ),
        {"lid": str(location_id), "cid": str(customer_id)},
    ).first()
    if not row:
        return False, f"location_id {location_id!r} does not belong to customer"
    return True, None


def _holding_area_id_by_name(db: Any, name: str) -> str | None:
    """Resolve a holding-area row by name. Returns id (str) or None if missing.

    Used by create_job (auto-route service-call jobs into 'Ready to
    Schedule') and the estimate accept path (auto-route accepted estimates
    into 'Order Doors'). Per the 2026-05-13 directive these two routes are
    automatic; if the area row is missing on this tenant (the migration
    script is the source of truth) we return None and let the job be
    created without a holding area rather than failing the request.
    """
    try:
        row = db.execute(
            _text("SELECT id FROM holding_areas WHERE name = :n LIMIT 1"),
            {"n": name},
        ).first()
        return str(row[0]) if row else None
    except Exception:
        log.exception("holding_area_lookup_failed name=%s", name)
        return None


def _holding_area_exists(db: Any, holding_area_id: str) -> bool:
    """True iff a non-deleted holding area with this id exists on this
    tenant. The tenant-plane connection IS the isolation boundary — no
    ``company_id`` filter (it would be redundant *and* trip
    ``tenant_plane_redundant_filter_scan``): a row only resolves inside
    this tenant's own DB, so "exists here" == "belongs to this tenant".
    Soft-deleted areas (``deleted_at IS NOT NULL``) count as nonexistent
    so a job can't be routed into a retired lane and silently vanish from
    dispatch boards. A genuine DB error is intentionally NOT swallowed —
    it propagates to create_job's ``SQLAlchemyError`` handler rather than
    masquerading as a misleading "holding area not found" 400.
    """
    row = db.execute(
        _text(
            "SELECT 1 FROM holding_areas "
            "WHERE id = :hid AND deleted_at IS NULL LIMIT 1"
        ),
        {"hid": str(holding_area_id)},
    ).first()
    return row is not None


def _normalize_tech_id_list(
    payload_ids: list[str] | None,
    legacy_singular: str | None,
) -> list[str]:
    # Accept both ``assigned_tech_ids: [...]`` (multi-tech) and
    # ``assigned_tech_id: "..."`` (legacy single). Dedupe preserving order.
    raw: list[str] = []
    if payload_ids:
        raw.extend(payload_ids)
    if legacy_singular:
        raw.append(legacy_singular)
    seen: set[str] = set()
    out: list[str] = []
    for tid in raw:
        s = (tid or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _set_job_assignments(
    db: Session,
    *,
    job_id: str,
    tech_ids: list[str],
    lead_tech_id: str | None,
    user_id: str,
) -> str | None:
    # Diff-apply the desired (job, tech) edges. Soft-delete assignments
    # that fell out of the list, insert new ones, reconcile ``is_lead``,
    # and mirror ``Job.assigned_to`` to (lead | first | NULL) so legacy
    # single-tech reads stay coherent. Returns the resolved primary tech id.
    now = datetime.now(UTC)
    existing = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job_id),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()
    by_tech = {row.tech_id: row for row in existing}
    desired = set(tech_ids)

    for tech, row in by_tech.items():
        if tech not in desired:
            row.deleted_at = now

    for tech in tech_ids:
        if tech in by_tech:
            continue
        db.add(
            JobAssignment(
                id=str(uuid.uuid4()),
                job_id=str(job_id),
                tech_id=tech,
                user_id=None,
                is_lead=False,
                assigned_at=now,
                assigned_by=user_id,
            )
        )

    db.flush()

    resolved_lead: str | None = None
    if tech_ids:
        if lead_tech_id and lead_tech_id in desired:
            resolved_lead = lead_tech_id
        else:
            resolved_lead = tech_ids[0]

    if resolved_lead is None:
        db.execute(
            _text(
                "UPDATE job_assignments SET is_lead = :f "
                "WHERE job_id = :j AND deleted_at IS NULL"
            ),
            {"j": str(job_id), "f": False},
        )
    else:
        db.execute(
            _text(
                "UPDATE job_assignments SET is_lead = (tech_id = :t) "
                "WHERE job_id = :j AND deleted_at IS NULL"
            ),
            {"t": resolved_lead, "j": str(job_id)},
        )
    # ORM update so the UUID column gets typed correctly across PG / SQLite.
    # Coerce job_id to UUID for the WHERE clause; raw ``WHERE id = :j`` with
    # str(uuid) silently misses on SQLite (CHAR(32) hex without dashes).
    try:
        job_uuid: Any = uuid.UUID(str(job_id))
    except (ValueError, AttributeError):
        job_uuid = job_id
    db.execute(
        update(Job)
        .where(Job.id == job_uuid, Job.deleted_at.is_(None))
        .values(assigned_to=resolved_lead)
    )
    db.expire_all()
    return resolved_lead


def _sync_job_appointment(
    db: Session,
    job: Job,
    tenant_id: str,
    user: Any,
    customer_name: str | None = None,
) -> None:
    # Mirror a scheduled job into the `appointments` table. One appointment
    # per (job, tech) — JobAssignment is the source of truth; if the job is
    # a single-tech legacy row with only ``Job.assigned_to`` set we fall
    # back to writing one appointment using that. Idempotent: existing
    # rows update in place, removed techs get their appointment soft-deleted.
    if not job.scheduled_at:
        # If a job lost its date, retire any open appointments so the
        # calendar doesn't keep showing a phantom slot.
        db.execute(
            _text(
                "UPDATE appointments SET deleted_at = :now, updated_at = :now "
                "WHERE job_id = :jid AND deleted_at IS NULL"
            ),
            {"now": datetime.now(UTC), "jid": str(job.id)},
        )
        return

    assignments = db.execute(
        select(JobAssignment).where(
            JobAssignment.job_id == str(job.id),
            JobAssignment.deleted_at.is_(None),
        )
    ).scalars().all()
    desired_techs: list[str | None] = [a.tech_id for a in assignments]
    if not desired_techs:
        # Pre-multi-tech jobs only have Job.assigned_to set. Honor that so
        # legacy data still gets one appointment row.
        desired_techs = [str(job.assigned_to) if job.assigned_to else None]

    from gdx_dispatch.routers.appointments import compute_man_hour_duration_minutes
    duration = compute_man_hour_duration_minutes(db, job.id) or 60
    start_at = job.scheduled_at
    end_at = start_at + timedelta(minutes=duration)
    title = (job.title or "Job")[:300]

    if customer_name is None and job.customer_id:
        cust_row = db.execute(
            _text("SELECT name FROM customers WHERE id = :cid"),
            {"cid": str(job.customer_id)},
        ).first()
        if cust_row:
            customer_name = cust_row[0]

    existing_appts = db.execute(
        select(Appointment).where(
            Appointment.job_id == job.id,
            Appointment.deleted_at.is_(None),
        )
    ).scalars().all()
    by_tech: dict[str | None, Appointment] = {}
    now = datetime.now(UTC)
    for a in existing_appts:
        # Defensive: if duplicates exist (e.g., a tech was removed and
        # re-added in a prior incarnation), keep one and retire the rest.
        if a.tech_id in by_tech:
            a.deleted_at = now
            continue
        by_tech[a.tech_id] = a

    desired_set = set(desired_techs)
    for tech_id, appt in by_tech.items():
        if tech_id not in desired_set:
            appt.deleted_at = now

    for tech_id in desired_techs:
        appt = by_tech.get(tech_id)
        if appt is not None and appt.deleted_at is None:
            appt.title = title
            appt.start_at = start_at
            appt.end_at = end_at
            appt.duration_minutes = duration
            appt.customer_id = job.customer_id
            appt.customer_name = customer_name
            appt.updated_at = now
            continue
        db.add(
            Appointment(
                company_id=tenant_id,
                job_id=job.id,
                customer_id=job.customer_id,
                tech_id=tech_id,
                title=title,
                start_at=start_at,
                end_at=end_at,
                duration_minutes=duration,
                status="scheduled",
                customer_name=customer_name,
                created_by=_user_id(user),
            )
        )


def _job_to_dict(job: Job, customer: Customer | None = None) -> dict[str, Any]:
    """Serialize a Job ORM object to a dict, optionally including customer info."""
    d: dict[str, Any] = {
        "id": job.id,
        "job_number": job.job_number,
        "title": job.title,
        "description": job.description,
        "status": job.status,
        "lifecycle_stage": job.lifecycle_stage,
        "dispatch_status": job.dispatch_status,
        "billing_status": job.billing_status,
        "scheduled_at": job.scheduled_at,
        "completed_at": job.completed_at,
        "priority": job.priority,
        "job_type": job.job_type,
        "customer_id": job.customer_id,
        "assigned_to": job.assigned_to,
        "holding_area_id": job.holding_area_id,
        "location_id": job.location_id,
        "scheduled_duration_hours": job.scheduled_duration_hours,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "is_demo": job.is_demo,
        "is_return_visit": job.is_return_visit,
        "parent_job_id": job.parent_job_id,
        "source": job.source,
        "company_id": job.company_id,
    }
    if customer is not None:
        d["customer_name"] = customer.name
        d["customer_phone"] = customer.phone
    return d


def _display_state_for_jobs(
    db: Session, jobs: list[tuple[Any, Any]]
) -> dict[str, dict[str, object]]:
    """Batched ``{job_id: display_state}`` for ``(job_id, raw_lifecycle)``
    pairs — the single source of truth (Slice 4 Wave 0a).

    One query per related table (mirrors the ``crew_by_job`` precedent —
    no N+1). The pure ``derive_job_display_state`` does the logic; this
    only assembles its inputs (linked invoices + originating estimate
    status). Strictly additive: any failure degrades to an empty map so
    the jobs list never breaks over a display field. Pass the RAW
    ``lifecycle_stage`` — never the ``_canon_status``-normalized form.
    """
    # job.id is a Uuid(as_uuid=True) column — its bind processor expects
    # UUID objects, so coerce (inputs may be str from the raw-SQL list
    # path or UUID from the ORM path). Keep str forms for dict keys.
    uuid_ids: list[uuid.UUID] = []
    for j in jobs:
        if not j[0]:
            continue
        try:
            uuid_ids.append(uuid.UUID(str(j[0])))
        except (ValueError, AttributeError, TypeError):
            continue
    if not uuid_ids:
        return {}
    inv_by_job: dict[str, list[dict[str, Any]]] = {}
    est_by_job: dict[str, str] = {}
    try:
        for row in db.execute(
            select(
                Invoice.job_id,
                Invoice.status,
                Invoice.balance_due,
                Invoice.amount_paid,
            ).where(Invoice.job_id.in_(uuid_ids), Invoice.deleted_at.is_(None))
        ).all():
            if row.job_id is None:
                continue
            inv_by_job.setdefault(str(row.job_id), []).append(
                {
                    "status": row.status,
                    "balance_due": row.balance_due,
                    "amount_paid": row.amount_paid,
                }
            )
        if _HAS_ESTIMATE_ORM:
            for row in db.execute(
                select(Estimate.job_id, Estimate.status).where(
                    Estimate.job_id.in_(uuid_ids),
                    Estimate.deleted_at.is_(None),
                )
            ).all():
                if row.job_id is not None:
                    # Last-seen wins — a job's most-recent estimate status.
                    # Multi-estimate jobs are a display nicety, not a
                    # correctness boundary, in Wave 0a.
                    est_by_job[str(row.job_id)] = row.status
    except SQLAlchemyError:
        log.exception("display_state_enrichment_failed")
        return {}
    out: dict[str, dict[str, object]] = {}
    for jid, lc in jobs:
        if not jid:
            continue
        sjid = str(jid)
        out[sjid] = derive_job_display_state(
            lifecycle_stage=lc,
            estimate_status=est_by_job.get(sjid),
            invoices=inv_by_job.get(sjid),
        ).as_dict()
    return out


@router.get("", response_model=None)
def list_jobs(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 50,
    per_page: int | None = None,
    search: str | None = None,
    status: str | None = None,
    customer_id: str | None = None,
):
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    page = max(1, page)
    # Accept ``per_page`` as an alias for ``page_size`` — older clients (and the
    # public REST consumers) use ``per_page``; the server name is ``page_size``.
    # P1-4 fix 2026-04-27.
    if per_page is not None:
        page_size = per_page
    page_size = max(1, min(page_size, 500))
    offset = (page - 1) * page_size

    # Dynamic WHERE — kept as raw SQL because of ILIKE, CAST on PG enum, and
    # conditional clauses that are hard to express portably with the ORM.
    where = ["j.company_id = :tenant_id", "j.deleted_at IS NULL"]
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if search:
        where.append("(j.title ILIKE :search OR c.name ILIKE :search)")
        params["search"] = f"%{search}%"
    if status:
        # Match against both normalized and raw forms (supports 'Scheduled' / 'scheduled' / lifecycle_stage)
        # lifecycle_stage is a PG enum — cast to text so COALESCE can mix with varchar status
        where.append(
            "(LOWER(COALESCE(CAST(j.lifecycle_stage AS text), j.status, '')) = LOWER(:status) "
            "OR LOWER(COALESCE(j.status, '')) = LOWER(:status))"
        )
        params["status"] = status
    if customer_id:
        where.append("j.customer_id = :customer_id")
        params["customer_id"] = customer_id
    where_sql = " AND ".join(where)

    try:
        total = db.execute(
            _text(
                f"SELECT COUNT(*) FROM jobs j "
                f"LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"WHERE {where_sql}"
            ),
            params,
        ).scalar() or 0
        # Aggregate status counts across the ENTIRE filtered set (not just the current page)
        # so the frontend stat cards and status tabs can show global totals.
        count_rows = db.execute(
            _text(
                "SELECT COALESCE(CAST(j.lifecycle_stage AS text), j.status) AS st, COUNT(*) AS n "
                "FROM jobs j LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"WHERE {where_sql} "
                "GROUP BY COALESCE(CAST(j.lifecycle_stage AS text), j.status)"
            ),
            params,
        ).mappings().all()
        status_counts: dict[str, int] = {}
        for cr in count_rows:
            key = _canon_status(cr.get("st"))
            status_counts[key] = status_counts.get(key, 0) + int(cr.get("n") or 0)
        rows = db.execute(
            _text(
                "SELECT j.id, j.job_number, j.title, j.description, j.status, j.lifecycle_stage, "
                "j.dispatch_status, j.billing_status, j.scheduled_at, j.completed_at, "
                "j.priority, j.job_type, j.customer_id, j.assigned_to, j.holding_area_id, "
                "j.scheduled_duration_hours, j.location_id, "
                "j.created_at, j.updated_at, "
                "c.name AS customer_name, c.phone AS customer_phone, "
                "t.name AS tech_name, "
                "cl.label AS location_label, cl.address AS location_address "
                f"FROM jobs j LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"LEFT JOIN technicians t ON CAST(t.id AS TEXT) = CAST(j.assigned_to AS TEXT) AND t.deleted_at IS NULL "
                "LEFT JOIN customer_locations cl ON cl.id = j.location_id AND cl.deleted_at IS NULL "
                f"WHERE {where_sql} "
                "ORDER BY j.scheduled_at DESC NULLS LAST, j.created_at DESC "
                "LIMIT :page_size OFFSET :offset"
            ),
            {**params, "page_size": page_size, "offset": offset},
        ).mappings().all()
    except SQLAlchemyError as exc:
        log.exception("list_jobs_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)

    # Batched fetch of multi-tech assignments for the page's jobs — single
    # query, attached to each item below. The Dispatch board needs every
    # assigned tech, not just the primary, so a crew job's card lands under
    # every tech column it belongs to.
    page_job_ids = [str(r.get("id")) for r in rows if r.get("id")]
    crew_by_job: dict[str, list[dict[str, Any]]] = {}
    if page_job_ids:
        crew_rows = db.execute(
            select(
                JobAssignment.job_id,
                JobAssignment.tech_id,
                JobAssignment.is_lead,
            )
            .where(
                JobAssignment.deleted_at.is_(None),
                JobAssignment.job_id.in_(page_job_ids),
            )
            .order_by(JobAssignment.is_lead.desc(), JobAssignment.assigned_at.asc())
        ).all()
        for cr in crew_rows:
            crew_by_job.setdefault(str(cr.job_id), []).append(
                {"tech_id": cr.tech_id, "is_lead": bool(cr.is_lead)}
            )

    # Canonical display state — computed from the RAW rows (before the
    # _canon_status overwrite below) so the derivation sees the true
    # lifecycle_stage. Additive: existing fields are untouched.
    _ds_map = _display_state_for_jobs(
        db, [(r.get("id"), r.get("lifecycle_stage")) for r in rows]
    )

    # Sprint dispatch-capacity (2026-05-20) — effective duration on the
    # LIST endpoint is the scheduler-entered hours only. We DELIBERATELY
    # do NOT call compute_man_hour_duration_minutes here — that helper
    # runs 2-3 queries per job and the list endpoint is the dispatch
    # board's hot path. Estimate-derived fallback lives on the per-job
    # detail endpoint (`/api/jobs/{id}/duration`) when a single job's
    # value is needed. "?h" on the board is correct: it means "scheduler
    # hasn't put a number on this yet", which is the signal the drop
    # prompt + create dialog are built to capture. /audit 2026-05-21 — N+1.

    items = []
    for r in rows:
        d = dict(r)
        d["status_raw"] = d.get("status")
        d["lifecycle_stage_raw"] = d.get("lifecycle_stage")
        canon = _canon_status(d.get("lifecycle_stage"), d.get("status"))
        d["status"] = canon
        # Also overwrite lifecycle_stage so any consumer preferring it gets the normalized form.
        d["lifecycle_stage"] = canon
        d["display_state"] = _ds_map.get(str(d.get("id")))
        sched_hours = d.get("scheduled_duration_hours")
        d["effective_duration_hours"] = float(sched_hours) if sched_hours is not None else None
        d["customer"] = (
            {
                "id": d["customer_id"],
                "name": d.get("customer_name"),
                "phone": d.get("customer_phone"),
            }
            if d.get("customer_id")
            else None
        )
        crew = crew_by_job.get(str(d.get("id")), [])
        d["assigned_tech_ids"] = [m["tech_id"] for m in crew]
        d["lead_tech_id"] = next((m["tech_id"] for m in crew if m["is_lead"]), None)
        # Pre-multi-tech jobs have no JobAssignment row yet — keep a useful
        # single-tech list so Dispatch's filter still hits something.
        if not d["assigned_tech_ids"] and d.get("assigned_to"):
            d["assigned_tech_ids"] = [str(d["assigned_to"])]
        items.append(d)
    return jsonable_response({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_counts": status_counts,
    })

@router.post("", response_model=None)
def create_job(payload: JobCreate, request: Request, current_user: Any = Depends(get_current_user), db: Session = Depends(get_db)):
    _ = current_user
    if not (payload.title or "").strip(): return jsonable_response({"detail": "title is required"}, 400)  # noqa: E701,E702
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Hard gate: tenants can require a tech on every scheduled job. The soft
    # gate is UI-only (confirm dialog) and enforced in JobsView.submitForm.
    tech_ids = _normalize_tech_id_list(
        payload.assigned_tech_ids,
        payload.assigned_tech_id or payload.assigned_to,
    )
    require_tech_for_scheduled_job(
        tenant_id, payload.scheduled_at, tech_ids[0] if tech_ids else None
    )
    try:
        # Validate an explicit holding_area_id BEFORE anything irreversible.
        # next_job_number (below) commits a job number on the control plane
        # and cannot be rolled back, so rejecting a doomed request here
        # avoids burning a number and leaving a non-monotonic gap in the
        # tenant's job/invoice sequence (QB reconciliation defect). The
        # column has no FK either, so an unknown/soft-deleted id would
        # otherwise route the job into a phantom lane and vanish from
        # dispatch — the failure 2e41cc45 set out to kill. /audit 2026-05-19.
        if payload.holding_area_id and not _holding_area_exists(db, payload.holding_area_id):
            return jsonable_response(
                {"detail": f"holding_area_id {payload.holding_area_id!r} does not exist"},
                400,
            )
        ok, detail = _validate_location_for_customer(
            db, payload.location_id, payload.customer_id
        )
        if not ok:
            return jsonable_response({"detail": detail}, 400)
        now = datetime.now(UTC)
        customer_name: str | None = None
        if payload.customer_id:
            cust = db.execute(
                _text("SELECT name FROM customers WHERE id = :cid"),
                {"cid": str(payload.customer_id)},
            ).first()
            if cust:
                customer_name = cust[0]
        # Allocate the next job number on the control plane (atomic, FOR UPDATE).
        # Wrapped in try so a numbering hiccup never blocks job creation.
        assigned_number: str | None = None
        try:
            with SessionLocal() as cdb:
                assigned_number = next_job_number(cdb, tenant_id, customer_name=customer_name)
                cdb.commit()
        except Exception:
            log.exception("job_number_allocation_failed", extra={"tenant_id": tenant_id})
        # Lead tech becomes the legacy ``Job.assigned_to`` for single-tech
        # readers (dashboard, /api/jobs list). Multi-tech crew rows live in
        # ``job_assignments`` and are written below after the job exists.
        explicit_lead = payload.lead_tech_id if payload.lead_tech_id in tech_ids else None
        assigned_to_value = explicit_lead or (tech_ids[0] if tech_ids else None)
        # Derive lifecycle / display status from the actual data instead of
        # hard-coding "scheduled" — a job with no scheduled_at is a service
        # call awaiting dispatcher review. 2026-05-13 directive: jobs no
        # longer carry a "lead" stage; that taxonomy belongs to the
        # web-prospect `leads` table.
        derived_lifecycle = "scheduled" if payload.scheduled_at else "service_call"
        derived_status = "Scheduled" if payload.scheduled_at else "Service Call"
        # Service calls without a date land in "Ready to Schedule" so a
        # dispatcher reviews before they hit the tech list. Already-scheduled
        # and explicitly-routed (holding_area_id in payload) jobs are
        # respected as-is.
        derived_holding_area = (
            payload.holding_area_id
            or (_holding_area_id_by_name(db, "Ready to Schedule") if not payload.scheduled_at else None)
        )
        job = Job(
            id=uuid.uuid4(),
            title=payload.title.strip()[:500],
            customer_id=uuid.UUID(payload.customer_id) if payload.customer_id else None,
            scheduled_at=payload.scheduled_at,
            status=payload.status or derived_status,
            priority=payload.priority or "Normal",
            job_type=payload.job_type or "Service",
            company_id=tenant_id,
            job_number=assigned_number,
            created_at=now,
            updated_at=now,
            is_demo=False,
            lifecycle_stage=derived_lifecycle,
            assigned_to=assigned_to_value,
            dispatch_status="assigned" if assigned_to_value else "unassigned",
            billing_status="unbilled",
            is_return_visit=False,
            holding_area_id=derived_holding_area,
            scheduled_duration_hours=payload.scheduled_duration_hours,
            location_id=payload.location_id or None,
        )
        db.add(job)
        db.flush()
        if tech_ids:
            _set_job_assignments(
                db,
                job_id=str(job.id),
                tech_ids=tech_ids,
                lead_tech_id=payload.lead_tech_id,
                user_id=_user_id(current_user),
            )
        _sync_job_appointment(db, job, tenant_id, current_user, customer_name=customer_name)
        db.commit()
        result = {"id": job.id, "title": job.title, "status": job.status, "customer_id": job.customer_id, "scheduled_at": job.scheduled_at, "created_at": job.created_at, "job_number": job.job_number}
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_created",
            entity_type="job",
            entity_id=str(job.id),
            details={"title": job.title, "status": job.status, "customer_id": str(job.customer_id) if job.customer_id else None},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("job_created", extra={"tenant_id": tenant_id, "job_id": str(job.id)})
        return jsonable_response(result, 201)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("create_job_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)

# Canonical PG enum values for jobs.lifecycle_stage. Anything else is
# rejected on write so we don't silently accept "Sold" or "Invoiced"
# (both were in the old write-allow list but neither exists in the enum,
# producing a Postgres CAST error or — worse — a silent type coercion).
_VALID_LIFECYCLE_STAGES = {
    "lead", "service_call", "estimate", "scheduled", "in_progress", "completed", "cancelled",
}


def _lifecycle_stage_for_write(status: str | None) -> str | None:
    """Map a human-facing status label to the lifecycle_stage PG enum literal."""
    if not status:
        return None
    s = str(status).strip().lower()
    # Common display-form → enum-literal aliases
    mapping = {
        "service call": "service_call",  # display "Service Call" → enum "service_call"
        "in progress": "in_progress",
        "complete": "completed",  # display "Complete" → enum "completed"
        "canceled": "cancelled",
    }
    s = mapping.get(s, s)
    return s if s in _VALID_LIFECYCLE_STAGES else None


@router.patch("/{job_id}", response_model=None)
def update_job(
    job_id: str,
    payload: JobUpdate,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Partial update. Accepts any subset of JobUpdate fields; tenant-scoped; audit logged."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("update_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))

    updates: dict[str, Any] = {}
    data = payload.model_dump(exclude_unset=True) if hasattr(payload, "model_dump") else payload.dict(exclude_unset=True)

    # Map frontend field names to DB columns
    if "title" in data:
        updates["title"] = (data["title"] or "").strip()[:500] or None
    if "description" in data:
        updates["description"] = data["description"]
    if "customer_id" in data:
        updates["customer_id"] = data["customer_id"] or None
    if "scheduled_at" in data:
        updates["scheduled_at"] = data["scheduled_at"]
    if "job_type" in data:
        updates["job_type"] = data["job_type"]
    if "priority" in data:
        updates["priority"] = data["priority"]
    if "notes" in data:
        updates["notes"] = data["notes"]
    # Tech assignment. Three input shapes:
    #   - assigned_tech_ids: ["uuid", ...]  (multi-tech crew, post-S109)
    #   - assigned_tech_id: "uuid"          (legacy single-tech form)
    #   - assigned_to: "uuid"               (oldest legacy callers)
    # Whichever the caller sends, we resolve to a desired list and a
    # desired lead, then apply via _set_job_assignments after the patch.
    apply_assignments = False
    desired_tech_ids: list[str] = []
    desired_lead: str | None = None
    if "assigned_tech_ids" in data:
        desired_tech_ids = _normalize_tech_id_list(
            data.get("assigned_tech_ids"),
            data.get("assigned_tech_id") or data.get("assigned_to"),
        )
        desired_lead = data.get("lead_tech_id")
        apply_assignments = True
    elif "assigned_tech_id" in data or "assigned_to" in data:
        singular = data.get("assigned_tech_id") if "assigned_tech_id" in data else data.get("assigned_to")
        desired_tech_ids = _normalize_tech_id_list(None, singular)
        desired_lead = data.get("lead_tech_id")
        apply_assignments = True
    if apply_assignments:
        updates["assigned_to"] = desired_tech_ids[0] if desired_tech_ids else None
    if "holding_area_id" in data:
        hid = data["holding_area_id"] or None
        # Same phantom-lane guard as create_job: a PATCH must not write an
        # unknown/soft-deleted holding_area_id either. /audit 2026-05-19.
        if hid and not _holding_area_exists(db, hid):
            return jsonable_response(
                {"detail": f"holding_area_id {hid!r} does not exist"},
                400,
            )
        updates["holding_area_id"] = hid
    if "scheduled_duration_hours" in data:
        updates["scheduled_duration_hours"] = data["scheduled_duration_hours"]
    # Sprint customer-multi-location: validate the location belongs to the
    # job's (possibly updated) customer. Same pattern as the holding-area
    # phantom-lane guard above.
    if "location_id" in data:
        updates["location_id"] = data["location_id"] or None
    # Status: write to both status (varchar) and lifecycle_stage (enum) in sync
    raw_status = data.get("status") or data.get("lifecycle_stage")
    if raw_status is not None:
        updates["status"] = str(raw_status).strip().title() or None
        ls = _lifecycle_stage_for_write(raw_status)
        if ls:
            updates["lifecycle_stage"] = ls

    if not updates:
        return jsonable_response({"detail": "no fields to update"}, 400)

    now = datetime.now(UTC)
    updates["updated_at"] = now

    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        job = db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        # Sprint customer-multi-location: validate against the resolved
        # customer_id (whichever the patch ends with, not just the payload).
        if "location_id" in updates:
            resolved_customer = updates.get("customer_id", job.customer_id)
            resolved_customer_str = (
                str(resolved_customer) if resolved_customer else None
            )
            ok, detail = _validate_location_for_customer(
                db, updates["location_id"], resolved_customer_str
            )
            if not ok:
                return jsonable_response({"detail": detail}, 400)

        # Hard gate (2026-05-01): if the patch moves the job into a
        # "scheduled but no tech" state, refuse. Only trips when the patch
        # itself touches scheduled_at or assigned_to — an unrelated edit
        # (e.g., changing a title on an already-bad job) is not blocked.
        touches_gate_field = "scheduled_at" in updates or "assigned_to" in updates
        if touches_gate_field:
            new_scheduled = updates.get("scheduled_at", job.scheduled_at)
            new_assigned = updates.get("assigned_to", job.assigned_to)
            require_tech_for_scheduled_job(tenant_id, new_scheduled, new_assigned)

        # Keep lifecycle/status in sync with scheduled_at: clearing the
        # date drops the job back to a service call unless the caller is also
        # transitioning the lifecycle explicitly (e.g., to cancelled). Legacy
        # "lead" rows are still recognized so a pre-migration row clearing
        # its date doesn't crash; we just rewrite them as service_call.
        if "scheduled_at" in updates and "lifecycle_stage" not in updates and "status" not in data:
            new_scheduled = updates["scheduled_at"]
            current_stage = (job.lifecycle_stage or "").lower()
            if current_stage in ("scheduled", "service_call", "lead", ""):
                if new_scheduled and current_stage != "scheduled":
                    updates["lifecycle_stage"] = "scheduled"
                    updates["status"] = "Scheduled"
                elif not new_scheduled and current_stage != "service_call":
                    updates["lifecycle_stage"] = "service_call"
                    updates["status"] = "Service Call"

        # Apply updates via ORM — lifecycle_stage needs CAST on PG enum,
        # so we use raw SQL for that single column if present.
        ls_value = updates.pop("lifecycle_stage", None)
        for col, val in updates.items():
            setattr(job, col, val)

        if ls_value is not None:
            # PG enum requires explicit CAST; use raw SQL for this one column
            db.execute(
                _text("UPDATE jobs SET lifecycle_stage = CAST(:ls AS job_lifecycle_stage) WHERE id = :jid"),
                {"ls": ls_value, "jid": job_id},
            )

        db.flush()
        if apply_assignments:
            _set_job_assignments(
                db,
                job_id=str(job.id),
                tech_ids=desired_tech_ids,
                lead_tech_id=desired_lead,
                user_id=_user_id(current_user),
            )
        db.commit()
        # Re-read to get the final state including lifecycle_stage and the
        # primary recomputed by _set_job_assignments.
        db.refresh(job)
        # Mirror any schedule/assignment/title change into the appointments
        # table so the Appointments page and unconfirmed-arrivals list stay
        # in sync with the canonical jobs row.
        if apply_assignments or any(k in updates for k in ("scheduled_at", "title", "customer_id")):
            _sync_job_appointment(db, job, tenant_id, current_user)
            db.commit()
        result = {
            "id": job.id, "title": job.title, "status": job.status,
            "lifecycle_stage": job.lifecycle_stage, "customer_id": job.customer_id,
            "scheduled_at": job.scheduled_at, "priority": job.priority,
            "job_type": job.job_type, "assigned_to": job.assigned_to,
            "location_id": job.location_id,
            "updated_at": job.updated_at,
        }
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_updated",
            entity_type="job",
            entity_id=str(job.id),
            details={"fields": list({**updates, **({"lifecycle_stage": ls_value} if ls_value else {})}.keys())},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("job_updated", extra={"tenant_id": tenant_id, "job_id": job_id, "fields": list(updates.keys())})
        return jsonable_response(result)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("update_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


@router.delete("/{job_id}", response_model=None)
def delete_job(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft delete — sets deleted_at. Tenant-scoped; audit logged."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("delete_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        job = db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        job.deleted_at = now
        job.updated_at = now
        # Cascade soft-delete to the mirrored appointment so it disappears
        # from the Appointments page alongside the job.
        db.execute(
            _text(
                "UPDATE appointments SET deleted_at = :now, updated_at = :now "
                "WHERE job_id = :jid AND deleted_at IS NULL"
            ),
            {"now": now, "jid": job_id},
        )
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_deleted",
            entity_type="job",
            entity_id=str(job.id),
            details={"title": job.title},
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("job_deleted", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"ok": True, "id": str(job.id)})
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("delete_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


# --- Job lifecycle: start / complete (UX audit F-8 / 2026-04-29) ---
# Defaults: stamp started_at + auto-assign current user on Start.
# Optional behaviors (lock schedule, post arrival event, SMS arrival,
# require parts/hours/signature on complete) are tenant-toggleable
# flags read from TenantSettings; the no-op fallback is Just Works.

def _load_workflow_flags(tenant_id: str) -> dict[str, bool]:
    """Read the per-tenant workflow toggles. Always returns a dict (defaults
    everywhere false), so callers never branch on 'flag not present'."""
    defaults = {
        "lock_schedule_on_start": False,
        "post_arrival_event": False,
        "sms_arrival_notify": False,
        "require_parts_on_complete": False,
        "require_hours_on_complete": False,
        "require_signature_on_complete": False,
    }
    try:
        with SessionLocal() as cdb:
            row = cdb.execute(
                _text(
                    "SELECT workflow_lock_schedule_on_start, workflow_post_arrival_event, "
                    "workflow_sms_arrival_notify, workflow_require_parts_on_complete, "
                    "workflow_require_hours_on_complete, workflow_require_signature_on_complete "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            ).first()
            if row:
                return {
                    "lock_schedule_on_start": bool(row[0]),
                    "post_arrival_event": bool(row[1]),
                    "sms_arrival_notify": bool(row[2]),
                    "require_parts_on_complete": bool(row[3]),
                    "require_hours_on_complete": bool(row[4]),
                    "require_signature_on_complete": bool(row[5]),
                }
    except Exception:
        log.exception("workflow_flags_read_failed", extra={"tenant_id": tenant_id})
    return defaults


@router.post("/{job_id}/start", response_model=None)
def start_job(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Tech taps Start Job. Default: flip lifecycle to in_progress, stamp
    started_at, auto-assign current user if unset. Optional toggles fan out."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        # Idempotent — re-starting a started job is a no-op (don't restamp).
        if not job.started_at:
            job.started_at = now
        if not job.assigned_to:
            job.assigned_to = _user_id(current_user)
        job.lifecycle_stage = "in_progress"
        job.status = "In Progress"
        if job.dispatch_status == "unassigned":
            job.dispatch_status = "assigned"
        job.updated_at = now
        db.flush()
        db.commit()

        # Optional: post arrival event to customer timeline. Best-effort —
        # failure here doesn't roll back the start.
        if flags["post_arrival_event"] and job.customer_id:
            try:
                db.execute(
                    _text(
                        "INSERT INTO job_notes "
                        "(id, company_id, job_id, author_id, body, visibility, created_at, updated_at) "
                        "VALUES (:id, :cid, :jid, :uid, :body, 'internal', :ts, :ts)"
                    ),
                    {
                        "id": str(uuid.uuid4()),
                        "cid": tenant_id,
                        "jid": str(job.id),
                        "body": f"Tech arrived on site at {now.strftime('%I:%M %p')}",
                        "ts": now,
                        "uid": _user_id(current_user),
                    },
                )
                db.commit()
            except Exception:
                log.exception("arrival_event_write_failed")
                db.rollback()

        # Optional: SMS arrival notify — gated on phone.com integration. We
        # log the intent today; the wire-up to PhoneCom send_sms is a follow-up.
        if flags["sms_arrival_notify"]:
            log.info("workflow_sms_arrival_intent", extra={"tenant_id": tenant_id, "job_id": job_id})

        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_started", entity_type="job", entity_id=str(job.id),
            details={"flags": flags, "schedule_locked": flags["lock_schedule_on_start"]},
            ip_address=request.client.host if request.client else None, request=request,
        )
        db.commit()
        return jsonable_response({
            "ok": True,
            "id": str(job.id),
            "started_at": job.started_at,
            "assigned_to": job.assigned_to,
            "lifecycle_stage": job.lifecycle_stage,
            "schedule_locked": flags["lock_schedule_on_start"],
        })
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("start_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


class JobCompletePayload(BaseModel):
    hours: float | None = None
    notes: str | None = None
    # Frontend may signal that the required side-channel artifacts are present
    # so the server doesn't have to re-query (e.g. signature_data is on the
    # row already; parts_count summarizes the JobPartNeeded rows).
    parts_count: int | None = None
    signature_present: bool | None = None


@router.post("/{job_id}/complete", response_model=None)
def complete_job(
    payload: JobCompletePayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark job complete. Validates per-tenant required fields if any flags
    are on. Returns 422 with `missing` list when validation fails so the
    frontend can highlight what to fill in."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        missing: list[str] = []
        if flags["require_parts_on_complete"]:
            parts = payload.parts_count
            if parts is None:
                # Fall back to live count.
                parts = db.execute(
                    _text("SELECT COUNT(*) FROM job_parts_needed WHERE job_id = :jid"),
                    {"jid": str(job.id)},
                ).scalar() or 0
            if int(parts or 0) <= 0:
                missing.append("parts")
        if flags["require_hours_on_complete"]:
            if payload.hours is None or float(payload.hours) <= 0:
                missing.append("hours")
        if flags["require_signature_on_complete"]:
            sig = payload.signature_present
            if sig is None:
                sig = bool(job.signature_data)
            if not sig:
                missing.append("signature")
        if missing:
            return jsonable_response(
                {"detail": "completion requirements unmet", "missing": missing},
                422,
            )

        job.lifecycle_stage = "completed"
        job.status = "Completed"
        job.completed_at = now
        job.dispatch_status = "done"
        if payload.notes:
            job.notes = (job.notes + "\n\n" if job.notes else "") + payload.notes.strip()
        job.updated_at = now
        db.flush()
        db.commit()

        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_completed", entity_type="job", entity_id=str(job.id),
            details={"hours": payload.hours, "flags_evaluated": flags},
            ip_address=request.client.host if request.client else None, request=request,
        )
        db.commit()
        return jsonable_response({
            "ok": True, "id": str(job.id),
            "completed_at": job.completed_at, "lifecycle_stage": job.lifecycle_stage,
        })
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("complete_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


# --- Phase 2 closeout sheet ---
# Doug 2026-05-10: Phase 1 routed dispatch's "Complete" through the gated
# /complete endpoint. Phase 2 promotes completion from a status flip to a
# closeout transaction — parts used, hours, signature, notes get captured
# in one POST and written to JobCloseout for audit/billing.
#
# Single-transaction semantics: parts inserts + time-entry attachment +
# closeout snapshot + lifecycle flip all commit or roll back together.
# Failure leaves the job uncompleted (idempotent re-submit is safe).

class CloseoutPart(BaseModel):
    part_id: str | None = Field(default=None, max_length=36)
    sku: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    qty: int = Field(ge=1, le=999)
    unit_cost: float | None = Field(default=None, ge=0)


class CloseoutPayload(BaseModel):
    parts: list[CloseoutPart] = Field(default_factory=list, max_length=100)
    hours: float = Field(ge=0, le=99)
    signature_data: str | None = Field(default=None, max_length=200_000)
    signed_by: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)


@router.post("/{job_id}/closeout", response_model=None, status_code=201)
def closeout_job(
    payload: CloseoutPayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Close out a job — single-transaction submission of parts used + hours
    + signature + notes. On success: writes JobPart rows (existing inventory
    schema), attaches the calling tech's open work time_entry to this job
    (or creates a synthetic entry if none open + hours > 0), writes a
    JobCloseout snapshot, flips lifecycle to 'completed', stamps signature
    on the job for backwards compatibility with Phase 1 readers.

    422 with {missing: [...]} on tenant-gate failure (same shape as
    /complete — Phase 1 client toasts already handle this).
    """
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    user_id = _user_id(current_user)

    # Pull job
    job = db.execute(
        select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    # Same gate vocabulary as /complete so frontend toasts are uniform.
    missing: list[str] = []
    if flags["require_parts_on_complete"] and not payload.parts:
        missing.append("parts")
    if flags["require_hours_on_complete"] and (payload.hours or 0) <= 0:
        missing.append("hours")
    if flags["require_signature_on_complete"]:
        sig = (payload.signature_data or "").strip() or (job.signature_data or "").strip()
        if not sig:
            missing.append("signature")
    if missing:
        return jsonable_response(
            {"detail": "completion requirements unmet", "missing": missing},
            422,
        )

    # Lazy-import JobPart — same lazy pattern as parts_needed.py to avoid
    # coupling jobs.py to the inventory module's import side-effects.
    try:
        from gdx_dispatch.modules.inventory.models import JobPart, Part
    except Exception:  # noqa: BLE001
        log.exception("closeout_job_partmodel_import_failed")
        return jsonable_response({"detail": "inventory module unavailable"}, 500)

    try:
        # 1) Insert one JobPart row per closeout part WHEN the part is in
        #    inventory (has a real parts.id). Free-text closeout lines
        #    (tech wrote "Torsion spring 200x2.0" without picking from
        #    catalog) live ONLY in the JobCloseout snapshot — job_parts
        #    has a FK to parts.id and rejects synthetic UUIDs.
        #    Stock-shortage non-blocking: if the tenant tracks inventory
        #    and the part is tracked, allow qty_on_hand to go negative.
        #    Doug 2026-05-10: blocking completion on a stock count is
        #    a worse UX — the tech is on-site, the part is in the door.
        part_lines: list[dict] = []
        for p in payload.parts:
            unit_cost = float(p.unit_cost or 0)
            line_total = unit_cost * int(p.qty)
            part_uuid: uuid.UUID | None = None
            if p.part_id:
                try:
                    part_uuid = uuid.UUID(p.part_id)
                except (ValueError, AttributeError):
                    part_uuid = None
            # Verify the inventory part exists before inserting a JobPart row.
            # If the SKU or name doesn't match a real Part, skip the
            # job_parts insert and let the closeout snapshot carry the data.
            part_exists = False
            if part_uuid is not None:
                part_exists = db.execute(
                    select(Part.id).where(
                        Part.id == part_uuid,
                        Part.deleted_at.is_(None),
                    ),
                ).scalar_one_or_none() is not None
            if part_exists and part_uuid is not None:
                jp = JobPart(
                    id=uuid.uuid4(),
                    job_id=uuid.UUID(job_id),
                    part_id=part_uuid,
                    qty_used=int(p.qty),
                    unit_cost_at_time=unit_cost,
                    created_at=now,
                )
                db.add(jp)
            part_lines.append({
                "part_id": str(part_uuid) if part_exists and part_uuid else None,
                "sku": p.sku,
                "name": p.name,
                "qty": int(p.qty),
                "unit_cost": unit_cost,
                "line_total": line_total,
                # Mark whether the row was reflected in inventory or
                # snapshot-only — useful for the office's review (RFB).
                "in_inventory": part_exists,
            })

        # 2) Time-entry attachment — find the calling tech's open work
        #    entry. If found and unattached, attach it to this job. If
        #    none open and hours > 0, write a synthetic entry to keep the
        #    labor-cost trail honest (closer != calling tech is expected
        #    when a dispatcher closes for a forgetful tech).
        if (payload.hours or 0) > 0:
            open_entry = db.execute(
                select(TimeEntry).where(
                    TimeEntry.tech_id == user_id,
                    TimeEntry.clock_out.is_(None),
                    TimeEntry.deleted_at.is_(None),
                ).order_by(TimeEntry.clock_in.desc()).limit(1)
            ).scalar_one_or_none()
            if open_entry and not open_entry.job_id:
                open_entry.job_id = uuid.UUID(job_id)
                open_entry.updated_at = now
            elif not open_entry:
                # Synthetic entry — closer attests `hours` worth of work.
                clock_in_at = now - timedelta(hours=float(payload.hours or 0))
                synthetic = TimeEntry(
                    id=uuid.uuid4(),
                    company_id=tenant_id,
                    job_id=uuid.UUID(job_id),
                    tech_id=user_id,
                    clock_in=clock_in_at,
                    clock_out=now,
                    duration_minutes=int(round(float(payload.hours or 0) * 60)),
                    entry_type="work",
                    notes="Closeout-attached",
                    created_at=now,
                    updated_at=now,
                )
                db.add(synthetic)

        # 3) JobCloseout snapshot row.
        closeout = JobCloseout(
            id=uuid.uuid4(),
            job_id=uuid.UUID(job_id),
            parts_used=part_lines,
            hours_worked=float(payload.hours or 0),
            signature_data=payload.signature_data or None,
            signed_by=payload.signed_by or None,
            signed_at=now if (payload.signature_data or "").strip() else None,
            notes=(payload.notes or "").strip() or None,
            closed_by_user_id=user_id,
            closed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(closeout)

        # 4) Flip the job to completed. Stamp signature for back-compat
        #    with the Phase 1 /complete reader path that checks job.signature_data.
        job.lifecycle_stage = "completed"
        job.status = "Completed"
        job.completed_at = now
        job.dispatch_status = "done"
        if payload.signature_data:
            job.signature_data = payload.signature_data
        if payload.notes:
            job.notes = (job.notes + "\n\n" if job.notes else "") + payload.notes.strip()
        job.updated_at = now

        db.flush()
        db.commit()

        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=user_id,
            action="job_closeout",
            entity_type="job",
            entity_id=str(job.id),
            details={
                "closeout_id": str(closeout.id),
                "parts_count": len(part_lines),
                "hours": float(payload.hours or 0),
                "signature_present": bool(payload.signature_data),
            },
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()

        return jsonable_response({
            "ok": True,
            "closeout_id": str(closeout.id),
            "job_id": str(job.id),
            "completed_at": job.completed_at,
            "parts_count": len(part_lines),
        }, 201)
    except SQLAlchemyError as exc:
        db.rollback()
        log.exception("closeout_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


@router.get("/ready-for-billing", response_model=None, dependencies=[Depends(require_permission("invoices.read_all"))])
def ready_for_billing(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Jobs completed but not yet invoiced — ready for billing."""
    try:
        # ORM: LEFT JOIN invoices — rows where Invoice.id IS NULL are uninvoiced.
        # Filter on lifecycle_stage (canonical post-D99) rather than the legacy
        # `status` varchar — QB-imported jobs have NULL status but
        # lifecycle_stage='completed', so the old `Job.status IN (Complete...)`
        # filter silently undercounted by ~50% on GDX prod (S114 reconcile:
        # /api/invoices/summary returned 8, this endpoint returned 4).
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        results = db.execute(
            select(Job, Customer, Invoice.id.label("invoice_id"))
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .outerjoin(
                Invoice,
                (Invoice.job_id == Job.id) & Invoice.deleted_at.is_(None),
            )
            .where(
                Job.lifecycle_stage == "completed",
                Job.deleted_at.is_(None),
                Invoice.id.is_(None),
            )
            .order_by(Job.created_at.desc())
            .limit(100)
        ).all()
        return [
            {
                "id": str(job.id),
                "title": job.title or job.description or "",
                "customer_name": customer.name if customer else "",
                "customer_id": str(job.customer_id) if job.customer_id else None,
                "status": job.status,
                "created_at": str(job.created_at) if job.created_at else None,
            }
            for job, customer, _inv_id in results
        ]
    except Exception:
        log.exception("ready_for_billing_failed")
        with contextlib.suppress(Exception):
            db.rollback()
        return []


@router.post("/{job_id}/create-invoice", response_model=None)
def create_invoice_from_job(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One-click invoice creation from a completed job."""
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now_dt = datetime.now(timezone.utc)
    now_dt.isoformat()

    # Get job via ORM
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    job = db.execute(
        select(Job).where(
            Job.id == job_id,
            Job.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if not job:
        return jsonable_response({"detail": "Job not found"}, 404)
    if not job.customer_id:
        # 2026-05-11 — invoices.customer_id is NOT NULL. Refuse with a 400
        # so the caller sees a clear "fix the job first" message rather than
        # a 500 from the NOT NULL violation at db.flush().
        return jsonable_response(
            {"detail": "Job has no customer assigned — set a customer before invoicing"},
            400,
        )

    # Get estimate line items if available via ORM
    lines = []
    est_obj = None
    try:
        if _HAS_ESTIMATE_ORM:
            _job_uuid = uuid.UUID(job_id)
            est_obj = db.execute(
                select(Estimate).where(
                    Estimate.job_id == _job_uuid,
                    Estimate.deleted_at.is_(None),
                ).order_by(Estimate.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            if est_obj:
                est_lines = db.execute(
                    select(EstimateLine)
                    .where(EstimateLine.estimate_id == est_obj.id)
                    .order_by(EstimateLine.sort_order)
                ).scalars().all()
                lines = [
                    {
                        "description": el.description,
                        "quantity": el.quantity,
                        "unit_price": float(el.unit_price or 0),
                        "line_total": float(el.line_total or 0),
                    }
                    for el in est_lines
                ]
    except Exception:
        log.exception("create_invoice_from_job_estimate_lines_failed")
        with contextlib.suppress(Exception):
            db.rollback()

    if not lines:
        lines = [{"description": job.title or "Service", "quantity": 1, "unit_price": 0, "line_total": 0}]

    subtotal_value = sum(float(l.get("line_total", 0) or 0) for l in lines)

    # Tax: if a related estimate exists, use its totals (preserves the
    # quoted price). Otherwise resolve the tenant-default rate via the
    # canonical service (honors customer exemption).
    if est_obj is not None:
        from gdx_dispatch.modules.proposals.totals import compute_estimate_totals
        _t = compute_estimate_totals(est_obj, db)
        subtotal_value = _t["subtotal"]
        tax_amount_value = _t["tax"]
        total_value = _t["total"]
    else:
        _D = Decimal
        from gdx_dispatch.modules.tax.service import resolve_rate as _resolve_tax_rate
        try:
            _rate = float(_resolve_tax_rate(db, job.customer_id))
        except Exception:
            _rate = 0.0
        tax_amount_value = round(subtotal_value * _rate, 2)
        total_value = round(subtotal_value + tax_amount_value, 2)

    invoice_id = uuid.uuid4()
    due_date = now_dt + timedelta(days=30)
    inv_num = f"INV-{now_dt.strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"

    try:
        # Get next sequence number via ORM
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        seq = db.execute(
            select(func.coalesce(func.max(Invoice.sequence_number), 0) + 1)
        ).scalar() or 1

        invoice = Invoice(
            id=invoice_id,
            company_id=tenant_id,
            customer_id=job.customer_id,
            job_id=job.id,
            invoice_number=inv_num,
            billing_type="standard",
            sequence_number=seq,
            subtotal=subtotal_value,
            tax_amount=tax_amount_value,
            total=total_value,
            balance_due=total_value,
            status="draft",
            locked=False,
            public_token=str(uuid.uuid4()),
            invoice_date=now_dt.date(),
            due_date=due_date.date(),
            created_at=now_dt,
        )
        db.add(invoice)
        db.flush()

        # Create invoice line items via ORM
        for idx, li in enumerate(lines):
            line = InvoiceLine(
                id=uuid.uuid4(),
                company_id=tenant_id,
                invoice_id=invoice_id,
                description=li.get("description", ""),
                quantity=li.get("quantity", 1),
                unit_price=float(li.get("unit_price", 0) or 0),
                line_total=float(li.get("line_total", 0) or 0),
                sort_order=idx,
                created_at=now_dt,
            )
            db.add(line)
        db.commit()
    except Exception as exc:
        db.rollback()
        log.exception("create_invoice_from_job_failed")
        return jsonable_response({"detail": f"Failed to create invoice: {exc}"}, 500)

    log_audit_event_sync(
        db, tenant_id=tenant_id,
        user_id=str(current_user.get("sub", "system")),
        action="create", entity_type="invoice", entity_id=str(invoice_id),
        details={"job_id": job_id, "invoice_number": inv_num, "total": total_value},
        request=request,
    )
    db.commit()

    return {"invoice_id": str(invoice_id), "invoice_number": inv_num, "total": total_value}


@router.get("/{job_id}/activity", response_model=None)
def get_job_activity(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Audit-log-backed activity feed for a single job (create, update, delete, status changes)."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("get_job_activity_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    limit = max(1, min(limit, 200))
    try:
        # audit_log table — no ORM model, keep as raw SQL.
        # LEFT JOIN users so the frontend can render a human-readable
        # name instead of the raw user_id UUID (S110 D-S110-job-activity-raw-uuids).
        # COALESCE chooses full_name → name → username → email so any
        # populated field wins; user_id is preserved on the row in case
        # legacy clients want it. CAST keeps Postgres happy if user_id
        # is stored as varchar in audit_logs but uuid in users.
        rows = db.execute(
            _text(
                "SELECT a.id, a.action, a.user_id, a.details, a.created_at, "
                "       COALESCE(u.full_name, u.name, u.username, u.email) AS user_name "
                "FROM audit_logs a "
                "LEFT JOIN users u ON CAST(a.user_id AS text) = CAST(u.id AS text) "
                "WHERE a.tenant_id = :tenant_id "
                "  AND a.entity_type = 'job' "
                "  AND a.entity_id = :job_id "
                "ORDER BY a.created_at DESC LIMIT :limit"
            ),
            {"tenant_id": tenant_id, "job_id": job_id, "limit": limit},
        ).mappings().all()
    except SQLAlchemyError:
        log.exception("get_job_activity_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        # audit_log may not exist in this tenant DB yet — degrade to empty list rather than 500
        return jsonable_response({"items": [], "total": 0, "note": "audit_log unavailable"})
    items = [dict(r) for r in rows]
    return jsonable_response({"items": items, "total": len(items)})


@router.get("/{job_id}", response_model=None)
def get_job(job_id: str, request: Request, current_user: Any = Depends(get_current_user), db: Session = Depends(get_db)):
    _ = current_user
    # Validate UUID format before querying to avoid DataError on non-UUID paths like "new"
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("get_job caught exception")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        result = db.execute(
            select(Job, Customer).outerjoin(Customer, Job.customer_id == Customer.id).where(
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
        ).first()
        if not result:
            return jsonable_response({"detail": "job not found"}, 404)
        job, customer = result
        d = _job_to_dict(job, customer)
        # Sprint customer-multi-location: surface the bound location's
        # label + address so JobDetailView can render the site without a
        # second round trip. NULL location_id stays NULL — fallback path
        # (customer's primary location) is handled client-side.
        if job.location_id:
            loc_row = db.execute(
                _text(
                    "SELECT label, address FROM customer_locations "
                    "WHERE id = :lid AND deleted_at IS NULL"
                ),
                {"lid": str(job.location_id)},
            ).first()
            if loc_row:
                d["location_label"] = loc_row[0]
                d["location_address"] = loc_row[1]
            else:
                d["location_label"] = None
                d["location_address"] = None
        else:
            d["location_label"] = None
            d["location_address"] = None
        # Canonical display state (Slice 4 Wave 0a) — same source of truth
        # as the list endpoint, computed from the RAW lifecycle_stage.
        d["display_state"] = _display_state_for_jobs(
            db, [(job.id, job.lifecycle_stage)]
        ).get(str(job.id))
        # 2026-04-29: detail endpoint must canonicalize status the same way the
        # list endpoint does (see line 222–228). Without this, /jobs/:id returns
        # status="Estimate" while /jobs returns status="Lead" for the same job —
        # the JobDetailView header rendered "Estimate" while the Lifecycle Stage
        # panel two rows below it rendered "Lead". Same job, two labels.
        d["status_raw"] = d.get("status")
        d["lifecycle_stage_raw"] = d.get("lifecycle_stage")
        canon = _canon_status(d.get("lifecycle_stage"), d.get("status"))
        d["status"] = canon
        d["lifecycle_stage"] = canon
        # S5-A4: callback detection. A job is a callback if it has a parent
        # job and that parent completed within the last 90 days. Different P&L
        # treatment downstream (warranty cost vs new revenue).
        d["is_callback"] = False
        d["callback_window_days"] = 90
        if job.parent_job_id:
            try:
                parent_completed = db.execute(
                    _text(
                        "SELECT completed_at FROM jobs "
                        "WHERE id = :pid AND deleted_at IS NULL"
                    ),
                    {"pid": str(job.parent_job_id)},
                ).scalar()
                if parent_completed:
                    ref = job.scheduled_at or job.created_at or datetime.now(UTC)
                    if hasattr(parent_completed, "tzinfo") and parent_completed.tzinfo is None:
                        parent_completed = parent_completed.replace(tzinfo=UTC)
                    if hasattr(ref, "tzinfo") and ref.tzinfo is None:
                        ref = ref.replace(tzinfo=UTC)
                    delta = (ref - parent_completed).days
                    if 0 <= delta <= 90:
                        d["is_callback"] = True
            except SQLAlchemyError:
                log.exception("callback_detection_failed", extra={"job_id": job_id})
        return jsonable_response(d)
    except SQLAlchemyError as exc:
        log.exception("get_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": f"Database error: {exc}"}, 500)


# ---------------------------------------------------------------------------
# Job Duration Tracking (#209) — actual vs estimated time
# ---------------------------------------------------------------------------

@router.get("/{job_id}/duration", response_model=None)
def get_job_duration(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get actual vs estimated duration for a job from time entries."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("get_job_duration_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Get time entries for this job — kept as raw SQL because TimeEntry
        # has no company_id column in the ORM model but the DB table might.
        entries = db.execute(
            _text(
                """
                SELECT COALESCE(SUM(duration_minutes), 0) AS actual_minutes,
                       COUNT(*) AS entry_count
                FROM time_entries
                WHERE job_id = :job_id AND company_id = :tenant_id
                """
            ),
            {"job_id": job_id, "tenant_id": tenant_id},
        ).mappings().first()

        actual_min = int(entries["actual_minutes"]) if entries else 0
        actual_hours = round(actual_min / 60, 2)

        # Get estimated hours from job_type average — involves cross-table
        # aggregate with subquery; cleaner as raw SQL.
        avg = db.execute(
            _text(
                """
                SELECT j.job_type,
                       AVG(te.duration_minutes) AS avg_minutes,
                       COUNT(DISTINCT te.job_id) AS sample_size
                FROM jobs j
                JOIN time_entries te ON te.job_id = j.id AND te.company_id = j.company_id
                WHERE j.job_type = (SELECT job_type FROM jobs WHERE id = :job_id LIMIT 1)
                  AND j.company_id = :tenant_id
                  AND j.status IN ('Completed', 'completed')
                GROUP BY j.job_type
                """
            ),
            {"job_id": job_id, "tenant_id": tenant_id},
        ).mappings().first()

        estimated_hours = round(float(avg["avg_minutes"] or 0) / 60, 2) if avg else None
        variance = round(actual_hours - estimated_hours, 2) if estimated_hours else None

        return jsonable_response({
            "job_id": job_id,
            "actual_hours": actual_hours,
            "actual_minutes": actual_min,
            "estimated_hours": estimated_hours,
            "variance_hours": variance,
            "sample_size": int(avg["sample_size"]) if avg else 0,
        })
    except SQLAlchemyError:
        log.exception("job_duration_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to calculate duration"}, 500)


# ---------------------------------------------------------------------------
# Job Costing (#210) — real cost vs what customer paid
# ---------------------------------------------------------------------------

@router.get("/{job_id}/costing", response_model=None)
def get_job_costing(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Calculate real cost (labor + parts) vs customer revenue for a job."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("get_job_costing_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # F-82 / 2026-04-29 — labor cost is now resolved per time-entry
        # by gdx_dispatch.modules.payroll.effective_labor_cost, which prefers the
        # *true* rate (gross_pay / hours_paid in payroll_entries) over
        # the *estimated* rate (technicians.hourly_rate). Returns both
        # numbers so the UI can show variance.
        from gdx_dispatch.modules.payroll import effective_labor_cost
        time_rows = db.execute(
            _text(
                "SELECT user_id, duration_minutes, hourly_rate, started_at "
                "FROM time_entries "
                "WHERE job_id = :job_id AND company_id = :tenant_id"
            ),
            {"job_id": job_id, "tenant_id": tenant_id},
        ).mappings().all()
        true_total = 0.0
        est_total = 0.0
        labor_minutes = 0
        sources_seen: set[str] = set()
        for tr in time_rows:
            mins = int(tr.get("duration_minutes") or 0)
            labor_minutes += mins
            hours = mins / 60.0
            when_val = tr.get("started_at")
            lc = effective_labor_cost(
                db,
                tech_user_id=tr.get("user_id"),
                hours=hours,
                when=when_val,
            )
            sources_seen.add(lc.source)
            if lc.true_cost is not None:
                true_total += float(lc.true_cost)
            if lc.estimated_cost is not None:
                est_total += float(lc.estimated_cost)
            elif lc.source == "none" and tr.get("hourly_rate"):
                # Legacy time-entry-stamped rate fallback so we don't
                # silently drop pre-payroll-module data.
                est_total += hours * float(tr["hourly_rate"])

        # Backwards-compat field — best available number, preferring true.
        labor_cost = round(true_total if true_total > 0 else est_total, 2)
        labor_cost_true = round(true_total, 2)
        labor_cost_estimated = round(est_total, 2)
        labor_cost_variance = round(true_total - est_total, 2) if (true_total > 0 and est_total > 0) else None
        labor_cost_source = (
            "true" if true_total > 0 and "true" in sources_seen
            else "estimated" if est_total > 0
            else "none"
        )

        # Revenue from invoices via ORM
        revenue = db.execute(
            select(
                func.coalesce(func.sum(Invoice.total), 0).label("total_revenue"),
                func.coalesce(func.sum(Invoice.amount_paid), 0).label("total_paid"),
            ).where(
                Invoice.job_id == job_id,
                Invoice.company_id == tenant_id,
                Invoice.deleted_at.is_(None),
            )
        ).mappings().first()

        total_revenue = round(float(revenue["total_revenue"]) if revenue else 0, 2)
        total_paid = round(float(revenue["total_paid"]) if revenue else 0, 2)

        # Parts cost (estimate line items at cost)
        parts_cost = 0.0  # Would need cost column on estimate lines

        total_cost = round(labor_cost + parts_cost, 2)
        profit = round(total_revenue - total_cost, 2)
        margin_pct = round(profit / max(total_revenue, 0.01) * 100, 1)

        return jsonable_response({
            "job_id": job_id,
            "labor_cost": labor_cost,
            "labor_cost_true": labor_cost_true,
            "labor_cost_estimated": labor_cost_estimated,
            "labor_cost_variance": labor_cost_variance,
            "labor_cost_source": labor_cost_source,
            "labor_minutes": labor_minutes,
            "parts_cost": parts_cost,
            "total_cost": total_cost,
            "total_revenue": total_revenue,
            "total_paid": total_paid,
            "profit": profit,
            "margin_pct": margin_pct,
        })
    except SQLAlchemyError:
        log.exception("job_costing_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to calculate costing"}, 500)


# ---------------------------------------------------------------------------
# Job Dependencies (#207) — block jobs until prerequisites complete
# ---------------------------------------------------------------------------

class DependencyIn(BaseModel):
    depends_on_job_id: str = Field(min_length=1, max_length=36)


@router.post("/{job_id}/dependencies", response_model=None, status_code=201)
def add_job_dependency(
    job_id: str,
    payload: DependencyIn,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("add_job_dependency_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    dep_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    try:
        dep = JobDependency(
            id=dep_id,
            tenant_id=tenant_id,
            job_id=job_id,
            depends_on_job_id=payload.depends_on_job_id,
            created_at=now.isoformat(),
        )
        db.add(dep)
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_dependency_added", entity_type="job_dependency", entity_id=dep_id,
            details={"job_id": job_id, "depends_on": payload.depends_on_job_id},
            request=request,
        )
        db.commit()
        return jsonable_response({"id": dep_id, "job_id": job_id, "depends_on_job_id": payload.depends_on_job_id}, 201)
    except SQLAlchemyError:
        db.rollback()
        log.exception("add_job_dependency_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to add dependency"}, 500)


@router.get("/{job_id}/dependencies", response_model=None)
def list_job_dependencies(
    job_id: str, request: Request, current_user: Any = Depends(get_current_user), db: Session = Depends(get_db),
):
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("list_job_dependencies_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        deps = db.execute(
            select(JobDependency).where(
                JobDependency.tenant_id == tenant_id,
                JobDependency.job_id == job_id,
            )
        ).scalars().all()
        return jsonable_response([
            {"id": d.id, "job_id": d.job_id, "depends_on_job_id": d.depends_on_job_id, "created_at": d.created_at}
            for d in deps
        ])
    except SQLAlchemyError:
        log.exception("list_job_dependencies_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to list dependencies"}, 500)


@router.get("/{job_id}/can-start", response_model=None)
def can_start_job(
    job_id: str, request: Request, current_user: Any = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Check if all dependency jobs are completed."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("can_start_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Raw SQL because JobDependency uses Text columns and needs CAST to uuid
        # for the JOIN against jobs.id (UUID).
        row = db.execute(
            _text("""
                SELECT COUNT(*) AS blocking
                FROM job_dependencies d
                LEFT JOIN jobs j ON j.id = CAST(d.depends_on_job_id AS uuid) AND j.company_id = :tid
                WHERE d.tenant_id = :tid AND d.job_id = :jid
                  AND (j.status IS NULL OR j.status NOT IN ('Completed','completed'))
            """),
            {"tid": tenant_id, "jid": job_id},
        ).mappings().first()
        blocking = int(row["blocking"]) if row else 0
        return jsonable_response({"can_start": blocking == 0, "blocking_count": blocking})
    except SQLAlchemyError:
        log.exception("can_start_job_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to check dependencies"}, 500)


# ---------------------------------------------------------------------------
# Follow-up Jobs (#211) — auto-create return visit
# ---------------------------------------------------------------------------

@router.post("/{job_id}/follow-up", response_model=None, status_code=201)
def create_follow_up_job(
    job_id: str, request: Request, current_user: Any = Depends(get_current_user), db: Session = Depends(get_db),
):
    """Create a follow-up job linked to the original via parent_job_id."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("create_follow_up_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        original = db.execute(
            select(Job).where(
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not original:
            return jsonable_response({"detail": "Original job not found"}, 404)

        new_id = uuid.uuid4()
        now = datetime.now(UTC)
        # Follow-up has no scheduled_at on creation — it's a service call
        # waiting for the dispatcher to slot. Don't fake-flag it as scheduled.
        follow_up = Job(
            id=new_id,
            title=f"Follow-up: {original.title}"[:200],
            customer_id=original.customer_id,
            job_type=original.job_type or "Service",
            status="Service Call",
            company_id=tenant_id,
            parent_job_id=original.id,
            created_at=now,
            updated_at=now,
            is_demo=False,
            lifecycle_stage="service_call",
            dispatch_status="unassigned",
            billing_status="unbilled",
            priority="Normal",
            is_return_visit=True,
        )
        db.add(follow_up)
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="follow_up_job_created", entity_type="job", entity_id=str(new_id),
            details={"original_job_id": job_id, "title": f"Follow-up: {original.title}"},
            request=request,
        )
        db.commit()
        return jsonable_response({"id": str(new_id), "parent_job_id": job_id, "title": f"Follow-up: {original.title}"}, 201)
    except SQLAlchemyError:
        db.rollback()
        log.exception("create_follow_up_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to create follow-up"}, 500)


# ---------------------------------------------------------------------------
# F-32 / 2026-04-29 — Job state-override flows.
#
# When a tech / dispatcher tries to put a completed or cancelled job back
# on the board, the frontend opens a modal with three named paths:
#
#   1. "Add warranty / callback" → spawn-return-visit. Original stays
#      completed (and so does its invoice + audit). A new child job is
#      created with parent_job_id, is_return_visit=true, optionally
#      pre-scheduled and pre-assigned. This is the default / one-click
#      path because it's by far the most common reason in the field.
#   2. "Un-complete (mistake)" → uncomplete. Reverts the original job
#      to in_progress and clears completed_at. Requires a reason note.
#   3. "Other reason" → override-move (or reactivate for cancelled).
#      Anyone can do it, BUT the reason note is mandatory — Doug
#      2026-04-29: "otherwise people will find work arounds for it.
#      and we want the real data."
#
# All three log an audit row containing the reason verbatim so reporting
# can later answer "how many warranty visits did we do this month?" and
# "how often does the team un-complete jobs?" without guessing.
# ---------------------------------------------------------------------------

class SpawnReturnVisitPayload(BaseModel):
    reason: str | None = None  # warranty doesn't require a reason
    scheduled_at: datetime | None = None
    assigned_to: str | None = None
    title: str | None = None  # override the auto-prefixed title


@router.post("/{job_id}/spawn-return-visit", response_model=None, status_code=201)
def spawn_return_visit(
    payload: SpawnReturnVisitPayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a warranty / callback child job. Original is left untouched."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    try:
        original = db.execute(
            select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not original:
            return jsonable_response({"detail": "Original job not found"}, 404)

        new_id = uuid.uuid4()
        title = (payload.title or f"Return visit: {original.title}")[:200]
        # Allocate a job_number for the child — same atomic counter as create_job.
        assigned_number: str | None = None
        try:
            with SessionLocal() as cdb:
                cust_name = None
                if original.customer_id:
                    cust = db.execute(
                        _text("SELECT name FROM customers WHERE id = :cid"),
                        {"cid": str(original.customer_id)},
                    ).first()
                    if cust:
                        cust_name = cust[0]
                assigned_number = next_job_number(cdb, tenant_id, customer_name=cust_name)
                cdb.commit()
        except Exception:
            log.exception("return_visit_number_alloc_failed")

        # Same derive rule: a return-visit child without a date is a service call.
        derived_lifecycle = "scheduled" if payload.scheduled_at else "service_call"
        derived_status = "Scheduled" if payload.scheduled_at else "Service Call"
        child = Job(
            id=new_id,
            title=title,
            customer_id=original.customer_id,
            job_type=original.job_type or "Service",
            status=derived_status,
            company_id=tenant_id,
            parent_job_id=original.id,
            scheduled_at=payload.scheduled_at,
            assigned_to=(payload.assigned_to or None),
            job_number=assigned_number,
            created_at=now,
            updated_at=now,
            is_demo=False,
            lifecycle_stage=derived_lifecycle,
            dispatch_status="assigned" if payload.assigned_to else "unassigned",
            billing_status="unbilled",
            priority=original.priority or "Normal",
            is_return_visit=True,
        )
        db.add(child)
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="return_visit_spawned", entity_type="job", entity_id=str(new_id),
            details={
                "original_job_id": job_id,
                "reason": (payload.reason or "")[:500],
                "scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
                "assigned_to": payload.assigned_to,
            },
            request=request,
        )
        db.commit()
        return jsonable_response({
            "id": str(new_id),
            "parent_job_id": job_id,
            "title": title,
            "is_return_visit": True,
            "job_number": assigned_number,
            "scheduled_at": child.scheduled_at,
            "assigned_to": child.assigned_to,
        }, 201)
    except SQLAlchemyError:
        db.rollback()
        log.exception("spawn_return_visit_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to spawn return visit"}, 500)


class StateOverridePayload(BaseModel):
    reason: str  # mandatory — Doug 2026-04-29
    scheduled_at: datetime | None = None  # optional new slot for un-complete + reactivate
    assigned_to: str | None = None


def _validate_reason(reason: str | None) -> str | None:
    """Reason note must be present and meaningful — no whitespace-only,
    no single-character escapes. Returns the cleaned value or None if
    invalid (caller renders 422)."""
    if not reason:
        return None
    cleaned = reason.strip()
    if len(cleaned) < 4:
        return None
    return cleaned[:500]


@router.post("/{job_id}/uncomplete", response_model=None)
def uncomplete_job(
    payload: StateOverridePayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revert a completed job back to in_progress. Reason is mandatory."""
    cleaned = _validate_reason(payload.reason)
    if not cleaned:
        return jsonable_response({"detail": "reason is required (≥4 characters)"}, 422)
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        if job.lifecycle_stage != "completed":
            return jsonable_response({"detail": "only completed jobs can be un-completed"}, 409)

        prior_completed_at = job.completed_at
        job.lifecycle_stage = "in_progress"
        job.status = "In Progress"
        job.completed_at = None
        if payload.scheduled_at:
            job.scheduled_at = payload.scheduled_at
        if payload.assigned_to:
            job.assigned_to = payload.assigned_to
        job.updated_at = now
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_uncompleted", entity_type="job", entity_id=str(job.id),
            details={
                "reason": cleaned,
                "prior_completed_at": prior_completed_at.isoformat() if prior_completed_at else None,
                "new_scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
            },
            request=request,
        )
        db.commit()
        return jsonable_response({
            "ok": True, "id": str(job.id),
            "lifecycle_stage": job.lifecycle_stage,
            "scheduled_at": job.scheduled_at,
        })
    except SQLAlchemyError:
        db.rollback()
        log.exception("uncomplete_job_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to un-complete job"}, 500)


@router.post("/{job_id}/reactivate", response_model=None)
def reactivate_job(
    payload: StateOverridePayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revert a cancelled job back to scheduled. Reason is mandatory."""
    cleaned = _validate_reason(payload.reason)
    if not cleaned:
        return jsonable_response({"detail": "reason is required (≥4 characters)"}, 422)
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    now = datetime.now(UTC)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_id, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        if job.lifecycle_stage != "cancelled":
            return jsonable_response({"detail": "only cancelled jobs can be reactivated"}, 409)

        # Reactivate to "scheduled" only if the row actually has a date —
        # otherwise drop back to lead so it shows up in "New Jobs to
        # Schedule" instead of misleadingly appearing scheduled.
        if payload.scheduled_at:
            job.scheduled_at = payload.scheduled_at
        if payload.assigned_to:
            job.assigned_to = payload.assigned_to
            job.dispatch_status = "assigned"
        if job.scheduled_at:
            job.lifecycle_stage = "scheduled"
            job.status = "Scheduled"
        else:
            job.lifecycle_stage = "service_call"
            job.status = "Service Call"
        job.updated_at = now
        db.flush()
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_reactivated", entity_type="job", entity_id=str(job.id),
            details={
                "reason": cleaned,
                "new_scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
            },
            request=request,
        )
        db.commit()
        return jsonable_response({
            "ok": True, "id": str(job.id),
            "lifecycle_stage": job.lifecycle_stage,
            "scheduled_at": job.scheduled_at,
        })
    except SQLAlchemyError:
        db.rollback()
        log.exception("reactivate_job_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to reactivate job"}, 500)
