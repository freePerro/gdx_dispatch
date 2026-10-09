from __future__ import annotations

import contextlib
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import TextClause, Uuid, bindparam, func, or_, select, update
from sqlalchemy import text as _text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from gdx_dispatch.core.audit import ensure_audit_table, log_audit_event_sync
from gdx_dispatch.core.database import SessionLocal, contained_read, get_db
from gdx_dispatch.core.holding_areas import holding_area_id_by_name as _holding_area_id_by_name
from gdx_dispatch.core.invoice_paid import paid_amount_sq, paid_to_date_bulk
from gdx_dispatch.core.job_access import can_read_job, job_write_denial
from gdx_dispatch.core.job_display_state import derive_job_display_state
from gdx_dispatch.core.job_site import resolve_job_sites
from gdx_dispatch.core.job_taxonomy import SERVICE_CALL, canonical_job_type
from gdx_dispatch.core.modules import require_module, require_permission
from gdx_dispatch.core.part_pricing import (
    duplicate_capture_groups,
    resolve_sell_price_with_source,
)
from gdx_dispatch.core.roles import is_technician
from gdx_dispatch.core.settings_row import settings_sql, tenant_id_value
from gdx_dispatch.models.tenant_models import (
    Appointment,
    Customer,
    Invoice,
    Job,
    JobAssignment,
    JobCloseout,
    JobDependency,
    JobPartNeeded,
    Payment,
    Technician,
    TimeEntry,
)
from gdx_dispatch.modules.dispatch_settings import require_tech_for_scheduled_job
from gdx_dispatch.modules.numbering import next_job_number
from gdx_dispatch.services.day_close import CANCEL_STOP_SUFFIX as CANCEL_TIMER_NOTE_SUFFIX

try:
    from gdx_dispatch.modules.proposals.models import Estimate
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
    # 2026-07-22: the mobile dialog (and desktop create) always sent
    # description, but pydantic silently dropped it — nothing declared the
    # field, so typed-in descriptions were lost on every create.
    description: str | None = Field(default=None, max_length=20000)
    customer_id: str | None = Field(default=None, max_length=36)
    scheduled_at: datetime | None = None
    # No default: an omitted status must fall through to derived_status in
    # create_job ("Service Call" for date-less jobs). The old
    # default="Scheduled" made that derivation dead code and stamped
    # phantom "Scheduled" on unscheduled service calls.
    status: str | None = Field(default=None, max_length=50)
    # Plan §9: default is the canonical service spelling. The old default
    # "Service" was one of the two dropdown vocabularies that diverged and
    # left prod with four spellings of two work kinds.
    job_type: str = Field(default=SERVICE_CALL, min_length=1, max_length=50)
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
    # 2026-07-29: same class of bug as `description` above — both JobsView and
    # MobileJobNewDialog have always POSTed `notes` (the "Dispatch notes for
    # tech" box), pydantic had no field for it, so every note an operator typed
    # at create time was silently dropped. Declared here AND assigned in
    # create_job; a field without the assignment is the same bug wearing a hat.
    notes: str | None = Field(default=None, max_length=20000)
    # 2026-08-17: "Assign to me" from the mobile dialog. A tech creating a
    # job from the truck is usually standing at the door about to do (or
    # having just done) the work — leaving the job unassigned meant every
    # action on it (en-route/start/notes/closeout) 404'd behind the
    # assignment-only write gate, and the tech read "Could not save — job
    # not found" on a job they had just created. Resolved server-side to
    # the CALLER's technician row only (never a caller-chosen id); ignored
    # when explicit tech assignment fields are present or the caller has
    # no technician record. The job still lands in "Ready to Schedule"
    # (that routing keys off scheduled_at, not assignment), so dispatch
    # review is unchanged.
    assign_to_me: bool = False


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


def _uuid_text(sql: str, *names: str) -> TextClause:
    """`_text(sql)` with each named `:param` typed as the `Uuid` column it is
    compared to; bind a `uuid.UUID`. SQLite stores a Uuid as 32 dashless hex,
    so a dashed-string bind matches nothing there while Postgres casts it
    (GDXA-382). Names absent from ``sql`` are skipped, so a dynamic WHERE can
    name its optional filters unconditionally."""
    present = [n for n in names if re.search(rf":{n}\b", sql)]
    return _text(sql).bindparams(*(bindparam(n, type_=Uuid(as_uuid=True)) for n in present))

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


def _emit_job_event(db, job, event: str, tenant_id: str) -> None:
    """Stage a job.created / job.completed / job.cancelled webhook before the
    caller's commit. Guarded — never fails the job write."""
    from gdx_dispatch.core.webhooks.emit import emit_domain_event

    cid = getattr(job, "customer_id", None)
    emit_domain_event(
        db,
        event,
        str(job.id),
        {
            "job_id": str(job.id),
            "job_number": getattr(job, "job_number", None),
            "title": getattr(job, "title", None),
            "status": getattr(job, "status", None),
            "lifecycle_stage": getattr(job, "lifecycle_stage", None),
            "customer_id": str(cid) if cid else None,
            "company_id": str(tenant_id or ""),
        },
        tenant_id=str(tenant_id or ""),
    )


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


def _role_of(user: Any) -> str:
    """The caller's role claim, whatever shape the auth dependency handed us."""
    if isinstance(user, dict):
        return str(user.get("role") or "")
    return str(getattr(user, "role", "") or "")


def _caller_technician_id(db: Session, user_id: str) -> str | None:
    """The caller's own active technician id — the assign_to_me resolver.

    Mirrors routers/mobile.py:_get_technician_id (active IS NOT FALSE,
    newest row first) so "me" resolves to the same technician on both
    surfaces. Returns None for callers with no technician record (office
    accounts) — assign_to_me is then a no-op, not an error."""
    if not user_id:
        return None
    row = (
        db.query(Technician.id)
        .filter(
            Technician.user_id == user_id,
            Technician.active.isnot(False),
        )
        .order_by(Technician.created_at.desc())
        .first()
    )
    return str(row[0]) if row else None


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
        resolved_lead = lead_tech_id if lead_tech_id and lead_tech_id in desired else tech_ids[0]

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


def _visit_refused(refusal: Any) -> JSONResponse:
    """A refused visit plan (multi-day jobs plan §5.2a): 409, nothing written."""
    return jsonable_response(
        {"detail": refusal.message, "code": refusal.code, **refusal.detail}, 409,
    )


def _apply_visits(db: Session, job: Job, plan: Any, user: Any, reason: str) -> None:
    """Write a visit plan, then keep invariant I (``recompute_job_schedule``)."""
    from gdx_dispatch.services.visit_sync import apply_visit_plan, recompute_job_schedule

    apply_visit_plan(db, job, plan, _user_id(user) or None)
    recompute_job_schedule(db, job, _user_id(user) or None, reason)



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
        # `billing_status` is NOT read from the column — it is a stale cache
        # that stopped advancing in July 2026. get_job fills it from the invoices
        # (display_state.billing_status).
        "scheduled_at": job.scheduled_at,
        "completed_at": job.completed_at,
        # 2026-07-29: the third face of the same bug. `notes` (the "Dispatch
        # notes for tech" box) was dropped by JobCreate, never assigned in
        # create_job, AND never serialized here — so even once it persisted,
        # nothing could read it back. Write-only fields are invisible fields.
        "notes": job.notes,
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
        # 055: RFB dismiss mark — JobDetailView renders the tag + undo from
        # these; NULLs on every normal billable job.
        "not_billable_at": job.not_billable_at,
        "not_billable_reason": job.not_billable_reason,
        # 111 / GDXA-375: set by POST /{job_id}/cancel, cleared by /reactivate.
        "cancelled_at": job.cancelled_at,
        "cancel_reason": job.cancel_reason,
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

    ``contained_read`` (GDXA-158) is what makes "never breaks" true on
    Postgres. Every caller is a GET (``list_jobs``, ``get_job``, the customer
    job list, the dispatch board, the leads list's job progress), so no caller
    holds pending work to lose —
    but they all keep querying after this returns: ``list_jobs`` calls
    ``resolve_job_sites(db, …)`` on the very next statement. A bare swallow
    aborted the transaction, so the degraded ``{}`` bought nothing and the
    whole list 500'd on the following query. Pure reads throughout —
    ``paid_to_date_bulk`` re-raises rather than swallowing, which is what
    core.database rule 5's corollary requires of an intervening frame.
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
        with contained_read(db):
            _inv_rows: list = []
            # M35: `Invoice.amount_paid` is a cache nothing maintains, and
            # job_display_state keys "Partially Paid" and the deposit_paid badge
            # on it — so a partial payment recorded after 2026-07-31 read as $0
            # and the job showed "Invoiced". Derive paid-to-date from payments.
            for row in db.execute(
                select(
                    Invoice.id,
                    Invoice.job_id,
                    Invoice.status,
                    Invoice.balance_due,
                    Invoice.billing_type,
                    Invoice.total,
                ).where(Invoice.job_id.in_(uuid_ids), Invoice.deleted_at.is_(None))
            ).all():
                if row.job_id is None:
                    continue
                _inv_rows.append(row)
            _paid_by_invoice = paid_to_date_bulk(db, [r.id for r in _inv_rows])
            for row in _inv_rows:
                inv_by_job.setdefault(str(row.job_id), []).append(
                    {
                        "status": row.status,
                        "balance_due": row.balance_due,
                        "amount_paid": _paid_by_invoice.get(str(row.id), 0),
                        "billing_type": row.billing_type,
                        "total": row.total,
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
        # Look up by the canonical dashed form the queries above keyed on.
        # The raw-SQL list path hands in SQLite's 32-hex storage form, which
        # never matched, so on SQLite every listed job read as invoice-less.
        # The output stays keyed by the caller's own form.
        try:
            key = str(uuid.UUID(sjid))
        except ValueError:
            key = sjid
        out[sjid] = derive_job_display_state(
            lifecycle_stage=lc,
            estimate_status=est_by_job.get(key),
            invoices=inv_by_job.get(key),
        ).as_dict()
    return out


def _derived_billing_status(display_state: dict[str, object] | None) -> str | None:
    """The API's ``billing_status``: derived from invoices, never the column.

    `Job.billing_status` is a stale cache that stopped advancing in July 2026
    and never says "paid", so serving it told every API reader that paid jobs
    were unbilled or merely invoiced. The value
    rides on the display state (one invoice query, shared). If that
    enrichment degraded to nothing, say "unknown" with None rather than fall
    back to the column's lie.
    """
    if not display_state:
        return None
    return display_state.get("billing_status")  # type: ignore[return-value]


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
    date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    order: str | None = None,
):
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    page = max(1, page)
    # Accept ``per_page`` as an alias for ``page_size`` — older clients (and the
    # public REST consumers) use ``per_page``; the server name is ``page_size``.
    # P1-4 fix 2026-04-27.
    if per_page is not None:
        page_size = per_page

    # Date scoping. ``date=YYYY-MM-DD`` (single day) has been sent by the
    # dispatch boards and the dashboard Today card since they shipped; until
    # 2026-07 the server ignored it, so those callers got page 1 of ALL jobs
    # (50 rows) and day-filtered client-side — on a tenant with >50 jobs,
    # jobs for the selected day were silently missing from the board.
    # ``date_from``/``date_to`` (inclusive calendar bounds) serve the desktop
    # board's Week view and custom range filter, which render more than one
    # day from a single fetch.
    #
    # Every scope is deliberately widened by ±1 day in UTC, not cut exactly:
    # clients bucket by the TENANT-zone calendar day (zonedDateKey), which the
    # server can't reproduce without timezone math — the wide window
    # guarantees no tenant-zone match is ever excluded, and the client filter
    # remains the precise cut. Undated jobs stay included (the boards surface
    # them on today's column).
    def _parse_day(value: str, label: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise ValueError(f"{label} must be YYYY-MM-DD") from None

    date_lo: datetime | None = None
    date_hi: datetime | None = None
    try:
        if date_from or date_to:
            if not (date_from and date_to):
                return jsonable_response(
                    {"detail": "date_from and date_to must be provided together"}, 422
                )
            lo_day = _parse_day(date_from, "date_from")
            hi_day = _parse_day(date_to, "date_to")
            if hi_day < lo_day:
                return jsonable_response({"detail": "date_to is before date_from"}, 422)
            date_lo = lo_day - timedelta(days=1)
            date_hi = hi_day + timedelta(days=2)
        elif date:
            day = _parse_day(date, "date")
            date_lo = day - timedelta(days=1)
            date_hi = day + timedelta(days=2)
    except ValueError as exc:
        return jsonable_response({"detail": str(exc)}, 422)

    if date_lo is not None and per_page is None and "page_size" not in request.query_params:
        # A date-scoped request wants the whole day/range, not page 1 of it —
        # when the caller didn't size the page explicitly, lift the cap.
        page_size = 500

    page_size = max(1, min(page_size, 500))

    # Ordering is an EVICTION POLICY, not presentation — every capped fetch of
    # this endpoint keeps the first page_size rows and drops the rest, and
    # ~15 views consume it as their job picker with caps of 50/200/500.
    #
    # Default (`scheduled`): `scheduled_at DESC NULLS LAST` — dated work always
    #   survives a cap; undated backlog is what gets evicted. Load-bearing for
    #   the picker views; do not change it out from under them.
    # Opt-in (`order=activity`, 2026-07-29): COALESCE(scheduled_at, created_at)
    #   DESC — undated jobs slot into the timeline by creation time. Exists for
    #   the /jobs list view, where the old default sank a just-created undated
    #   job below every dated job in the tenant (last page, invisible the
    #   moment it was saved → the operator concluded the save failed and saved
    #   again). Under a cap this evicts the oldest of the coalesced timeline,
    #   so it is NOT the right ordering for pickers that must never lose old
    #   scheduled-but-unfinished work.
    if (order or "").strip().lower() == "activity":
        order_sql = "ORDER BY COALESCE(j.scheduled_at, j.created_at) DESC, j.created_at DESC "
    else:
        order_sql = "ORDER BY j.scheduled_at DESC NULLS LAST, j.created_at DESC "
    offset = (page - 1) * page_size

    # Dynamic WHERE — kept as raw SQL because of ILIKE, CAST on PG enum, and
    # conditional clauses that are hard to express portably with the ORM.
    where = ["j.company_id = :tenant_id", "j.deleted_at IS NULL"]
    params: dict[str, Any] = {"tenant_id": tenant_id}
    if search:
        # job_number is the identifier every surface PRINTS — the jobs list,
        # the email link chips, the [Job #…] subject marker — so it has to be
        # the identifier you can search by. Without it, a user reads
        # "JOB-2026-014" off the screen, types it into a job search, and gets
        # nothing.
        where.append("(j.title ILIKE :search OR c.name ILIKE :search OR j.job_number ILIKE :search)")
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
        try:
            params["customer_id"] = uuid.UUID(customer_id)
            where.append("j.customer_id = :customer_id")
        except ValueError:
            where.append("1 = 0")  # no customer has a malformed id
    if date_lo is not None and date_hi is not None:
        where.append(
            "(j.scheduled_at IS NULL OR (j.scheduled_at >= :date_lo AND j.scheduled_at < :date_hi))"
        )
        params["date_lo"] = date_lo
        params["date_hi"] = date_hi
    where_sql = " AND ".join(where)

    try:
        total = db.execute(
            _uuid_text(
                f"SELECT COUNT(*) FROM jobs j "  # noqa: S608 — WHERE is joined from literal fragments and order_sql is one of two literals; values are bound
                f"LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"WHERE {where_sql}",
                "customer_id",
            ),
            params,
        ).scalar() or 0
        # Aggregate status counts across the ENTIRE filtered set (not just the current page)
        # so the frontend stat cards and status tabs can show global totals.
        count_rows = db.execute(
            _uuid_text(
                "SELECT COALESCE(CAST(j.lifecycle_stage AS text), j.status) AS st, COUNT(*) AS n "  # noqa: S608 — WHERE is joined from literal fragments and order_sql is one of two literals; values are bound
                "FROM jobs j LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"WHERE {where_sql} "
                "GROUP BY COALESCE(CAST(j.lifecycle_stage AS text), j.status)",
                "customer_id",
            ),
            params,
        ).mappings().all()
        status_counts: dict[str, int] = {}
        for cr in count_rows:
            key = _canon_status(cr.get("st"))
            status_counts[key] = status_counts.get(key, 0) + int(cr.get("n") or 0)
        rows = db.execute(
            _uuid_text(
                "SELECT j.id, j.job_number, j.title, j.description, j.status, j.lifecycle_stage, "  # noqa: S608 — WHERE is joined from literal fragments and order_sql is one of two literals; values are bound
                "j.dispatch_status, j.scheduled_at, j.completed_at, "
                "j.priority, j.job_type, j.customer_id, j.assigned_to, j.holding_area_id, "
                "j.scheduled_duration_hours, j.location_id, j.is_return_visit, "
                "j.created_at, j.updated_at, "
                "c.name AS customer_name, c.phone AS customer_phone, "
                "t.name AS tech_name, "
                "cl.label AS location_label, cl.address AS location_address "
                f"FROM jobs j LEFT JOIN customers c ON c.id = j.customer_id AND c.deleted_at IS NULL "
                f"LEFT JOIN technicians t ON CAST(t.id AS TEXT) = CAST(j.assigned_to AS TEXT) AND t.deleted_at IS NULL "
                "LEFT JOIN customer_locations cl ON cl.id = j.location_id AND cl.deleted_at IS NULL "
                f"WHERE {where_sql} "
                f"{order_sql}"
                "LIMIT :page_size OFFSET :offset",
                "customer_id",
            ),
            {**params, "page_size": page_size, "offset": offset},
        ).mappings().all()
    except SQLAlchemyError:
        log.exception("list_jobs_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)

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

    # Effective jobsite per row (core/job_site.py, one batch — no N+1 on
    # the dispatch-board hot path). The raw SELECT's location_label/
    # location_address only cover the BOUND case; the helper adds the
    # primary-location and customer fallbacks, ORM-decrypted, so this
    # endpoint never selects encrypted c.address raw (RAWENC1 gate).
    _sites = resolve_job_sites(
        db, [(r.get("id"), r.get("location_id"), r.get("customer_id")) for r in rows]
    )

    items = []
    for r in rows:
        d = dict(r)
        _site = _sites.get(str(d.get("id")))
        d["site_label"] = _site.label if _site else None
        d["site_address"] = _site.address if _site else None
        d["site_address_missing"] = bool(_site.address_missing) if _site else False
        d["status_raw"] = d.get("status")
        d["lifecycle_stage_raw"] = d.get("lifecycle_stage")
        canon = _canon_status(d.get("lifecycle_stage"), d.get("status"))
        d["status"] = canon
        # Also overwrite lifecycle_stage so any consumer preferring it gets the normalized form.
        d["lifecycle_stage"] = canon
        d["display_state"] = _ds_map.get(str(d.get("id")))
        d["billing_status"] = _derived_billing_status(d["display_state"])
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
    # A job created in the field belongs to the tech who created it (Doug,
    # 2026-08-28). This used to hinge on the dialog's "Assign to me" toggle,
    # and a tech who switched it off got a job he could see and not touch —
    # no photos, no notes, every action "job not found" — twice in eleven
    # days. So the rule is enforced HERE, on the role, not on a client flag:
    # a technician-role caller with a technician record is assigned unless
    # dispatch named someone explicitly (explicit tech fields always win).
    # assign_to_me stays honoured for any other caller who asks for it; for
    # accounts with no technician row both paths are a no-op, never an error.
    self_assigned = False
    if not tech_ids and (payload.assign_to_me or is_technician(_role_of(current_user))):
        own_tech = _caller_technician_id(db, _user_id(current_user))
        if own_tech:
            tech_ids = [own_tech]
            self_assigned = True
    require_tech_for_scheduled_job(
        tenant_id, payload.scheduled_at, tech_ids[0] if tech_ids else None
    )
    try:
        # Validate an explicit holding_area_id BEFORE anything irreversible.
        # next_job_number (below) commits a job number in tenant_settings
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
            try:
                cust_uuid = uuid.UUID(str(payload.customer_id))
            except ValueError:
                cust_uuid = None
            cust = cust_uuid and db.execute(
                select(Customer.name).where(Customer.id == cust_uuid)
            ).first()
            if cust:
                customer_name = cust[0]
        # Allocate the next job number (atomic: FOR UPDATE on tenant_settings).
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
            description=(payload.description or "").strip() or None,
            created_by=_user_id(current_user) or None,
            customer_id=uuid.UUID(payload.customer_id) if payload.customer_id else None,
            scheduled_at=payload.scheduled_at,
            status=payload.status or derived_status,
            priority=payload.priority or "Normal",
            job_type=canonical_job_type(payload.job_type) or SERVICE_CALL,
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
            notes=(payload.notes or "").strip() or None,
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
        # E4: a new job's crew is booked on its date (multi-day jobs plan §5.2a).
        from gdx_dispatch.services.visit_sync import book_new_job

        book_new_job(db, job, _user_id(current_user) or None)
        _emit_job_event(db, job, "job.created", tenant_id)
        db.commit()
        # assigned_to is in the response so the mobile dialog can tell whether
        # assign_to_me actually landed (a caller with no technician row gets
        # an unassigned job back) and word its success toast honestly.
        result = {"id": job.id, "title": job.title, "description": job.description, "status": job.status, "customer_id": job.customer_id, "scheduled_at": job.scheduled_at, "created_at": job.created_at, "job_number": job.job_number, "assigned_to": job.assigned_to}
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_created",
            entity_type="job",
            entity_id=str(job.id),
            details={
                "title": job.title,
                "status": job.status,
                "customer_id": str(job.customer_id) if job.customer_id else None,
                # Self-assignment is an authorization-relevant act (the tech
                # granted himself the write path) — the audit row must show
                # who ended up on the job and that assign_to_me did it.
                "assigned_to": str(job.assigned_to) if job.assigned_to else None,
                "self_assigned": self_assigned,
            },
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("job_created", extra={"tenant_id": tenant_id, "job_id": str(job.id)})
        return jsonable_response(result, 201)
    except SQLAlchemyError:
        db.rollback()
        log.exception("create_job_failed", extra={"tenant_id": tenant_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)

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


def _stage_change_refusal(stored_stage: str | None, requested_stage: str) -> dict | None:
    """The 409 body for a stage change no generic PATCH may make, else None.

    Shared by this router's PATCH and the public API's (api/public_router.py),
    so no writer of lifecycle_stage can skip it. Callers drop a request that
    only resends the stored stage before asking.
    """
    stored = (stored_stage or "").lower()
    if stored in ("completed", "cancelled"):
        return {
            "detail": f"This job is {stored}. Use Re-open on the job "
                      "page to change its stage, so the reason is recorded.",
            "use": "reopen",
        }
    if requested_stage == "completed":
        return {
            "detail": "Finish a job with Close out (or Close without work "
                      "when there is nothing to attest), not a status change.",
            "use": "closeout",
        }
    if requested_stage == "cancelled":
        # GDXA-375: a cancel records when and why, releases the job's
        # unordered part requests and stops its running timers — none of
        # which a bare stage flip did.
        return {
            "detail": "Cancel a job with Cancel job on the job page, so the "
                      "reason is recorded.",
            "use": "cancel",
        }
    return None


def _job_patch_result(job: Job) -> dict:
    return {
        "id": job.id, "title": job.title, "status": job.status,
        "lifecycle_stage": job.lifecycle_stage, "customer_id": job.customer_id,
        "scheduled_at": job.scheduled_at, "priority": job.priority,
        "job_type": job.job_type, "assigned_to": job.assigned_to,
        "location_id": job.location_id,
        "updated_at": job.updated_at,
    }


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
        # Keep the PARSED uuid and bind that below — the anchor comment for the
        # seven routes in this file that used to validate the id here and then
        # bind the raw path string anyway.
        #
        # `Job.id` is `Uuid()`. Postgres takes either form, so a str bind is
        # invisible in production; SQLite raises StatementError ('str' object
        # has no attribute 'hex'), which `except SQLAlchemyError` below turns
        # into this route's deliberate 500. That is the whole of GDXA-37/88's
        # "PATCH returned 500, not 403 for a technician": not an authz
        # decision, a bind the default harness cannot execute — which is also
        # why six job-scoped writes had no admission test at all until
        # tests/test_jobs_write_authz.py could finally drive them.
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("update_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — the anchor comment for all eleven call sites in
    # this file; the others point here.
    #
    # Until 2026-09-25 every job-scoped write route here was gated by nothing
    # but `require_module("jobs")` + being authenticated, so a technician with
    # no claim on the job — and even the read-only `viewer` role — could
    # mutate it (GDXA-32). One predicate now answers for all of them,
    # core.job_access.job_write_denial: hold `jobs.write`, AND either have a
    # real claim on the job or be office tier. It returns the refusal shape
    # rather than a bool because the right status depends on the caller: 404
    # for someone who cannot see the job at all (a 403 would confirm the id
    # exists and let one tech enumerate another's jobs), 403 for someone who
    # can already read it and would learn nothing from "not found".
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])

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
        # Plan §9 (audit round 2): a stale SPA tab still running the old
        # CustomerDetailView dropdown can PATCH the dead "Service" spelling
        # back in after the 042 backfill — canonicalize at the write.
        updates["job_type"] = canonical_job_type(data["job_type"])
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
        ls = _lifecycle_stage_for_write(raw_status)
        if not ls:
            # A label naming no stage used to be written to `status` alone,
            # which skipped the stage guard below: "Scheduled Later" on a
            # completed job answered 200 and rewrote its status. No UI sends
            # one; the stage strip and the edit dialog only offer stages.
            return jsonable_response(
                {"detail": f"status {str(raw_status)!r} is not a job stage"}, 422,
            )
        updates["status"] = str(raw_status).strip().title() or None
        updates["lifecycle_stage"] = ls

    if not updates:
        return jsonable_response({"detail": "no fields to update"}, 400)

    now = datetime.now(UTC)
    updates["updated_at"] = now

    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        job = db.execute(
            select(Job).where(
                Job.id == job_uuid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        # Stage guard (2026-10-04, job-stage-paths plan §4.1).
        # This PATCH used to write any stage it was handed: the desktop's
        # "Complete Job" and stage strip finished jobs here with no
        # completed_at, no dispatch_status, no closeout record and none of
        # the tenant's completion requirements, and moved finished jobs
        # anywhere with no reason recorded. Completion now belongs to
        # /closeout or /close-without-work, leaving a finished job to
        # /uncomplete or /reactivate. A patch that resends the stored stage
        # (the Jobs list edit dialog does, on every save) is dropped rather
        # than rewritten, so it can't flip a closeout's "Completed" to
        # "Complete". Cancelling is refused here too (GDXA-375): it belongs to
        # /cancel, which records the reason and cleans up after the job.
        requested_stage = updates.get("lifecycle_stage")
        stored_stage = (job.lifecycle_stage or "").lower()
        if requested_stage is not None:
            if requested_stage == stored_stage:
                updates.pop("lifecycle_stage", None)
                updates.pop("status", None)
            elif refusal := _stage_change_refusal(stored_stage, requested_stage):
                return jsonable_response(refusal, 409)
            elif requested_stage == "in_progress" and not job.started_at:
                updates["started_at"] = now
            if len(updates) == 1:  # only updated_at left — nothing to change
                return jsonable_response(_job_patch_result(job))

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

        # The job's visits (multi-day jobs plan §5.2a): planned before anything
        # is written, so a refusal (R1–R4) leaves the job, its visits and the
        # audit log untouched. A date equal to the stored one to the minute is
        # no date edit (E1) and is not rewritten. A cancel never reaches here:
        # the stage guard above sends it to /cancel, which plans X1 itself.
        from gdx_dispatch.services.visit_sync import UNSET, plan_for_job, visit_fields

        visit_plan = plan_for_job(
            db, job,
            scheduled_at=updates.get("scheduled_at", UNSET),
            crew_after=tuple(desired_tech_ids) if apply_assignments else None,
            fields=visit_fields(
                db, updates.get("title", job.title), updates.get("customer_id", job.customer_id),
            ) if ("title" in updates or "customer_id" in updates) else None,
        )
        if visit_plan.refusal is not None:
            return _visit_refused(visit_plan.refusal)
        if "scheduled_at" in updates:
            if visit_plan.scheduled_at is UNSET:
                updates.pop("scheduled_at")
            else:
                updates["scheduled_at"] = visit_plan.scheduled_at

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
        # transitioning the lifecycle explicitly (e.g., to in progress). Legacy
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
            # PG enum requires explicit CAST; use raw SQL for this one column.
            #
            # This statement is Postgres-only in BOTH halves, and the obvious
            # tidy-up — reusing the `job_uuid` parsed at the top of this
            # handler, or matching SQLite's storage form — makes it worse, not
            # better. On SQLite `id = :jid` compares a dashed uuid against the
            # 32 hex characters SQLite stores, so it matches nothing and the
            # statement is a no-op; and `CAST(... AS job_lifecycle_stage)`
            # resolves to NUMERIC affinity there, so a statement that DID
            # match would store '0'. The two defects cancel, which is the only
            # reason the SQLite harness can run this route at all. Make the
            # WHERE match without also dialect-switching the CAST and you turn
            # a harmless no-op into a corrupt write. `jobs.status` is written
            # from the same patch through the ORM above, and that half IS
            # asserted in tests/test_jobs_write_authz.py.
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
            job = db.get(Job, job_uuid)
        # One transaction: the job, its visits and their audit rows commit
        # together.
        _apply_visits(db, job, visit_plan, current_user, "job_updated")
        db.commit()
        # Re-read to get the final state including lifecycle_stage and the
        # primary recomputed by _set_job_assignments.
        db.refresh(job)
        result = _job_patch_result(job)
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
    except SQLAlchemyError:
        db.rollback()
        log.exception("update_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


@router.delete("/{job_id}", response_model=None, dependencies=[Depends(require_permission("jobs.write"))])
def delete_job(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Soft delete — sets deleted_at. Tenant-scoped; audit logged.

    jobs.write gate (2026-08-04 audit catch): this endpoint carried NO
    permission at all — any authenticated role with the jobs module could
    delete any job. jobs.write is the same key the create/update paths
    imply, so every UI that legitimately offers Delete (JobsView, the RFB
    queue) keeps working.

    That decorator is a PERMISSION gate, not an object gate, and on its own
    it is the very trap job_write_denial exists to close: the builtin
    technician role holds jobs.write, so a tech with no claim on the job
    passed it and soft-deleted someone else's job. GDXA-32 listed this route
    among the three "already gated" and it was not — same class, found by the
    2026-09-25 audit of that fix.
    """
    try:
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        log.exception("delete_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        job = db.execute(
            select(Job).where(
                Job.id == job_uuid,
                Job.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)

        job.deleted_at = now
        job.updated_at = now
        # Cascade soft-delete to the mirrored appointment so it disappears
        # from the Appointments page alongside the job. Through the ORM so the
        # Uuid column binds `job_uuid` in each dialect's storage form: a raw
        # `job_id = :jid` with the dashed string never matches SQLite's 32
        # dashless hex, and the cascade silently did nothing there (GDXA-382).
        db.execute(
            update(Appointment)
            .where(Appointment.job_id == job_uuid, Appointment.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
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
    except SQLAlchemyError:
        db.rollback()
        log.exception("delete_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


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
        "require_invoice_on_complete": False,
    }
    try:
        with SessionLocal() as cdb:
            row = cdb.execute(
                settings_sql(
                    "SELECT workflow_lock_schedule_on_start, workflow_post_arrival_event, "
                    "workflow_sms_arrival_notify, workflow_require_parts_on_complete, "
                    "workflow_require_hours_on_complete, workflow_require_signature_on_complete, "
                    "workflow_require_invoice_on_complete "
                    "FROM tenant_settings WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id_value(tenant_id)},
            ).first()
            if row:
                return {
                    "lock_schedule_on_start": bool(row[0]),
                    "post_arrival_event": bool(row[1]),
                    "sms_arrival_notify": bool(row[2]),
                    "require_parts_on_complete": bool(row[3]),
                    "require_hours_on_complete": bool(row[4]),
                    "require_signature_on_complete": bool(row[5]),
                    "require_invoice_on_complete": bool(row[6]),
                }
    except Exception:
        log.exception("workflow_flags_read_failed", extra={"tenant_id": tenant_id})
    return defaults


def _mark_job_started(job: Job, now: datetime) -> None:
    """The one stage write for "this job has started": ``started_at`` if it is
    still null (a re-start never restamps), ``lifecycle_stage='in_progress'``,
    ``status='In Progress'``. ``start_job`` and the phone's en route and
    arrival (multi-day jobs plan §5.4a, B7) all go through it. The caller
    audits and commits.
    """
    if not job.started_at:
        job.started_at = now
    job.lifecycle_stage = "in_progress"
    job.status = "In Progress"


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
    # Object-level authz — see update_job. A tech with no claim on the job
    # must not be able to self-assign it by starting it (the auto-assign
    # below would otherwise hand it to them).
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    try:
        job = db.execute(
            select(Job).where(Job.id == uuid.UUID(job_id), Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        # /reactivate is the only way out of cancelled: it clears the stamp and
        # restores the released part requests, which a start would not.
        cancelled = _cancelled_refusal(job)
        if cancelled is not None:
            return cancelled

        # Auto-assign the starter's TECHNICIAN id (it was the token's user id
        # until 2026-10-05 — a users.id in a technicians.id column). With no
        # technician row there is no crew change. C1 then hands the job's
        # unassigned visit to them (multi-day jobs plan §5.2a).
        start_plan = None
        from gdx_dispatch.services.visit_sync import job_crew, plan_for_job

        if not job.assigned_to and not job_crew(db, job):
            starter = _caller_technician_id(db, _user_id(current_user))
            if starter:

                start_plan = plan_for_job(db, job, crew_after=(starter,))
                if start_plan.refusal is not None:
                    return _visit_refused(start_plan.refusal)
                _set_job_assignments(
                    db, job_id=str(job.id), tech_ids=[starter],
                    lead_tech_id=starter, user_id=_user_id(current_user),
                )
                job = db.get(Job, job.id)
        # Idempotent — re-starting a started job never restamps started_at.
        _mark_job_started(job, now)
        if job.dispatch_status == "unassigned" and job.assigned_to:
            job.dispatch_status = "assigned"
        job.updated_at = now
        db.flush()
        if start_plan is not None:
            _apply_visits(db, job, start_plan, current_user, "job_started")
        else:
            from gdx_dispatch.services.visit_sync import recompute_job_schedule

            recompute_job_schedule(db, job, _user_id(current_user) or None, "job_started")
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
        # log the intent today; wiring it to Phone.com's send_message
        # (modules/phone_com/client.py) is a follow-up, filed 2026-09-06.
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
    except SQLAlchemyError:
        db.rollback()
        log.exception("start_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


def _completed_result(job: Job) -> dict:
    return {
        "ok": True, "id": str(job.id),
        "completed_at": job.completed_at, "lifecycle_stage": job.lifecycle_stage,
    }


def _finish_visits(db: Session, job: Job, now: datetime, user: Any) -> None:
    """F1 (multi-day jobs plan §5.2a): close every ON SITE visit and every OPEN
    one on or before the finish day; retire the OPEN days after it. Shared by
    /complete, /close-without-work and /closeout. F1 never refuses."""
    from gdx_dispatch.services.visit_sync import plan_for_job, shop_day, shop_tz

    plan = plan_for_job(db, job, finish_day=shop_day(now, shop_tz(db)))
    _apply_visits(db, job, plan, user, "job_completed")


def _day_label(day: Any) -> str:
    """"Monday, November 2": the sheet's own wording for a shop day."""
    return f"{day:%A}, {day:%B} {day.day}"


def _locked_job(db: Session, job_id: Any) -> Job | None:
    """The live job row, locked ``FOR UPDATE`` (a no-op on SQLite). Every door
    that finishes or closes a day of a job takes this one lock first, so a
    racing day-close, closeout, /complete or close-without-work commits wholly
    before or after the other (multi-day jobs plan §5.4a)."""
    jid = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    return db.execute(
        select(Job).where(Job.id == jid, Job.deleted_at.is_(None)).with_for_update()
    ).scalar_one_or_none()


def _cancelled_refusal(job: Job) -> Any:
    """409 for a finishing door on a cancelled job. The cancel already stopped
    the crew's timers at 0, so finishing it here would complete a cancelled
    job and leave the attested hours on no one's row; the office reactivates
    it first, or enters the hours through labor."""
    if (job.lifecycle_stage or "").lower() != "cancelled":
        return None
    return jsonable_response({
        "detail": "This job was cancelled by the office. Ask the office to "
                  "reactivate it, or to enter your hours through labor.",
        "code": "job_cancelled",
    }, 409)


def _tap_shop_day(tapped_at: datetime | None, now: datetime, tz: str) -> Any:
    """The closeout tap's shop day: ``tapped_at`` clamped to no later than
    server ``now``, and ``now`` when absent (multi-day jobs plan §5.4a)."""
    from gdx_dispatch.services.visit_sync import shop_day

    tap = tapped_at
    if tap is not None and tap.tzinfo is None:
        tap = tap.replace(tzinfo=UTC)
    if tap is None or tap > now:
        tap = now
    return shop_day(tap, tz)


def _earlier_day_open_refusal(
    db: Session, job: Job, today: Any, tz: str | None, *, now: datetime | None = None,
) -> JSONResponse | None:
    """409 ``earlier_day_open`` when a worked past day of this job is still
    open and is not its final day (plan §5.4a, "The sheet"), else None. The
    one predicate the sheet, the closeout, /complete and /close-without-work
    share. ``today`` is the tap's shop day; None means server ``now``'s."""
    from gdx_dispatch.services import day_close
    from gdx_dispatch.services.visit_sync import shop_day

    tz = tz or day_close.shop_tz(db)
    if today is None:
        today = shop_day(now or datetime.now(UTC), tz)
    day = day_close.earlier_day_open(db, job, today, tz)
    if day is None:
        return None
    return jsonable_response({
        "detail": f"Close {_day_label(day)} first: answer No for that day",
        "code": "earlier_day_open",
        "date": day.isoformat(),
    }, 409)


def _mark_job_completed(db: Session, job: Job, now: datetime, tenant_id: str, user: Any) -> None:
    """The completion write shared by /complete and /close-without-work: the
    stage, the "Completed" spelling, completed_at, dispatch done, the job's
    visits (F1), and the job.completed webhook staged before the caller's
    commit."""
    _finish_visits(db, job, now, user)
    job.lifecycle_stage = "completed"
    job.status = "Completed"
    job.completed_at = now
    job.dispatch_status = "done"
    job.updated_at = now
    db.flush()
    _emit_job_event(db, job, "job.completed", tenant_id)


class JobCompletePayload(BaseModel):
    hours: float | None = None
    notes: str | None = None
    # Frontend may signal that the required side-channel artifacts are present
    # so the server doesn't have to re-query (e.g. signature_data is on the
    # row already; parts_count summarizes the JobPartNeeded rows).
    parts_count: int | None = None
    signature_present: bool | None = None
    # PR5 (Doug 2026-07-07): explicit "no parts used" attestation — satisfies
    # the require_parts gate without a parts list; bare silence still 422s.
    no_parts_used: bool = False


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
    # Object-level authz — see update_job.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    # Before anything is staged: its first run per engine may commit.
    ensure_audit_table(db)
    try:
        # Locked like closeout and day-close (multi-day jobs plan §5.4a).
        job = _locked_job(db, job_id)
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        refusal = _cancelled_refusal(job)
        if refusal is not None:
            return refusal
        refusal = _earlier_day_open_refusal(db, job, None, None, now=now)
        if refusal is not None:
            return refusal

        missing: list[str] = []
        if flags["require_parts_on_complete"]:
            parts = payload.parts_count
            if parts is None:
                # Fall back to live count.
                parts = db.execute(
                    _text("SELECT COUNT(*) FROM job_parts_needed WHERE job_id = :jid"),
                    {"jid": str(job.id)},
                ).scalar() or 0
            # PR5 audit catch: the attestation is a STATEMENT, not a bypass —
            # it only satisfies the gate when the checklist agrees (zero
            # rows). Attesting "no parts" over known parts rows is a
            # contradiction and still 422s.
            if int(parts or 0) <= 0 and not payload.no_parts_used:
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
        # PR5 — optional hard gate: no completion without a billing-real
        # invoice (voids and the fabricated $0 draft don't count — the
        # canonical predicate from PR2). Default OFF: invoice-after-
        # completion is the normal flow; the follow-up loop chases those.
        if flags["require_invoice_on_complete"]:
            from gdx_dispatch.core.billing_predicates import job_billed_exists
            billed = db.execute(
                select(Job.id).where(Job.id == job.id, job_billed_exists())
            ).scalar_one_or_none() is not None
            if not billed:
                missing.append("invoice")
        if missing:
            return jsonable_response(
                {"detail": "completion requirements unmet", "missing": missing},
                422,
            )

        if payload.notes:
            job.notes = (job.notes + "\n\n" if job.notes else "") + payload.notes.strip()
        _mark_job_completed(db, job, now, tenant_id, current_user)
        # Audited BEFORE the commit (plan §5.4a): this row bounds the
        # candidate timers after a re-open, so the completion and its trail
        # commit together or not at all.
        from gdx_dispatch.core.audit import audit_or_rollback

        audit_or_rollback(
            db, tenant_id=tenant_id, actor={"user_id": _user_id(current_user)},
            action="job_completed", entity_type="job", entity_id=str(job.id),
            details={
                "hours": payload.hours,
                "flags_evaluated": flags,
                "no_parts_used": bool(payload.no_parts_used),
            },
            request=request,
        )
        db.commit()
        return jsonable_response(_completed_result(job))
    except SQLAlchemyError:
        db.rollback()
        log.exception("complete_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


class CloseWithoutWorkPayload(BaseModel):
    reason: str


@router.post(
    "/{job_id}/close-without-work", response_model=None,
    dependencies=[Depends(require_permission("jobs.write"))],
)
def close_job_without_work(
    payload: CloseWithoutWorkPayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Finish a job that has no work to attest — an old job being tidied up, a
    no-show, a duplicate. Doug 2026-10-04 (job-stage-paths-plan D4): closeout
    requires hours > 0, so without this verb the office could only finish such
    a job by typing hours nobody worked, which closeout would then attest and
    bill. No closeout row, time entry, parts or invoice draft is written, and
    the tenant's completion requirements are deliberately not evaluated — the
    audit row records which ones were skipped and the mandatory reason. The
    job still shows in Ready-for-Billing; billing a trip charge or marking it
    not billable stays the office's call."""
    cleaned = _validate_reason(payload.reason)
    if not cleaned:
        return jsonable_response({"detail": "reason is required (≥4 characters)"}, 422)
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    # Before anything is staged: its first run per engine may commit.
    ensure_audit_table(db)
    try:
        # Locked before the timer read (plan §5.4a, round 36): a "No"
        # committing between the read and the write below would have its new
        # day row overwritten with 0.
        job = _locked_job(db, job_uuid)
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        if job.lifecycle_stage in ("completed", "cancelled"):
            return jsonable_response(
                {"detail": f"This job is already {job.lifecycle_stage}."}, 409,
            )
        # A forgotten worked day's timer would be 0-closed below and its
        # hours lost by this door (plan §5.4a, round 35).
        refusal = _earlier_day_open_refusal(db, job, None, None, now=now)
        if refusal is not None:
            return refusal

        prior_stage = job.lifecycle_stage
        # A no-show after the tech tapped "I'm here" leaves an arrival timer
        # open, and closeout is otherwise the only thing that ends one. Close
        # them at zero minutes exactly as closeout does for unattested timers
        # — nothing was attested, so nothing is payable or costed.
        timers = _open_job_timers(db, job.id)
        for timer in timers:
            _close_labor_entry(timer, now, 0, None)
        _mark_job_completed(db, job, now, tenant_id, current_user)
        # Audited BEFORE the commit (plan §5.4a): this row bounds the
        # candidate timers after a re-open.
        from gdx_dispatch.core.audit import audit_or_rollback

        audit_or_rollback(
            db, tenant_id=tenant_id, actor={"user_id": _user_id(current_user)},
            action="job_closed_without_work", entity_type="job", entity_id=str(job.id),
            details={
                "reason": cleaned,
                "prior_stage": prior_stage,
                "timers_closed_at_zero": [str(t.id) for t in timers],
                "requirements_skipped": [
                    k for k in ("require_parts_on_complete", "require_hours_on_complete",
                                "require_signature_on_complete", "require_invoice_on_complete")
                    if flags.get(k)
                ],
            },
            request=request,  # audit derives the IP, X-Forwarded-For first
        )
        db.commit()
        return jsonable_response(_completed_result(job))
    except SQLAlchemyError:
        db.rollback()
        log.exception("close_without_work_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


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
    # PR5 (Doug 2026-07-07): free-text explanation for a part that isn't in
    # the system — travels to the checklist row + snapshot so the office
    # knows what it's pricing.
    note: str | None = Field(default=None, max_length=500)


class CloseoutPartToOrder(BaseModel):
    """A part the job still NEEDS — the office orders it. Distinct from
    CloseoutPart, which attests a part already USED: these land as
    job_parts_needed rows with status='needed' (the Parts-to-Order queue),
    never as inventory decrements or billable used-part lines.

    'critical' is deliberately NOT accepted here: the C5 critical-part
    dispatcher push fires only on the Parts card path (add_part_needed),
    so a closeout-path critical would skip the fan-out silently — the one
    urgency level where silence is the bug. Critical asks go through the
    job screen's Parts card until the push is wired here too."""
    name: str = Field(min_length=1, max_length=200)
    sku: str | None = Field(default=None, max_length=255)
    qty: int = Field(default=1, ge=1, le=99)
    urgency: str = Field(default="normal", pattern="^(normal|urgent)$")
    note: str | None = Field(default=None, max_length=500)


class CloseoutPayload(BaseModel):
    parts: list[CloseoutPart] = Field(default_factory=list, max_length=100)
    hours: float = Field(ge=0, le=99)
    # Plan §11 (Doug): "ask how many techs on site". Crew size for the
    # attested duration — BILLING input only (billed man-hours = hours ×
    # techs under §8), never payroll. Bounded 1..10: a 0 would zero the
    # bill, and >10 on a residential door job is a typo, not a crew.
    techs_on_site: int = Field(default=1, ge=1, le=10)
    # Plan §8 install lane: the labor-matrix row picked for a flat-priced
    # install. Optional — service jobs don't set it.
    labor_matrix_item_id: str | None = Field(default=None, max_length=36)
    signature_data: str | None = Field(default=None, max_length=200_000)
    signed_by: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    # PR5 — the deliberate "No parts used" checkbox (satisfies the
    # require_parts gate; silence still 422s).
    no_parts_used: bool = False
    # Doug 2026-08-04: the tech attests at closeout whether the job needs a
    # return visit and WHY. The reason is mandatory when the flag is set —
    # an unexplained return visit is exactly the tribal knowledge this
    # replaces (the old flow was "add a note and a tag").
    needs_return_visit: bool = False
    return_visit_reason: str | None = Field(default=None, max_length=1000)
    parts_to_order: list[CloseoutPartToOrder] = Field(default_factory=list, max_length=50)
    # Multi-day jobs plan §5.4a: the sheet's tap time. "Today" for the
    # earlier/later-day refusals and the 0 h rule is the TAP's shop day, so an
    # early finish queued offline and replayed next morning is not refused.
    # Clamped to no later than server now. Nothing else reads it: every stamp
    # the closeout writes still uses server now.
    tapped_at: datetime | None = None


# The labor row a closeout owns, so a re-closeout updates it instead of
# adding a second one (mirrors the parts replace step).
CLOSEOUT_LABOR_NOTE = "Closeout-attached"



def _resolve_technician_id(db: Session, user_id: str) -> str | None:
    """Technician.id for a user, or None — used to look up the hourly rate.

    Deliberately NOT for identity: payroll groups `time_entries` by `user_id`
    (payroll.py:246-263) and `tech_id` is vestigial there. Mirrors
    `mobile._get_technician_id`, minus its unused tenant_id arg, plus the
    `deleted_at` filter it omits. `created_at` is nullable, so NULLs sort
    per-dialect — tie-break on id to keep SQLite and Postgres agreeing.

    ``contained_read`` (GDXA-158): the sole caller is ``closeout_job``, which
    reaches here mid-write with the closeout already staged. On Postgres a bare
    swallow aborted that transaction, so ``return None`` (read downstream as
    "this user is not a technician", costing the labor row its rate) was
    followed by the tech's whole attested closeout failing to commit — the
    degraded answer was never the real damage. Reads only (core.database
    rule 2); inside the ``try`` so the ``except`` still runs (rule 1).
    """
    try:
        with contained_read(db):
            row = (
                db.query(Technician.id)
                .filter(
                    Technician.user_id == user_id,
                    Technician.active.isnot(False),
                    Technician.deleted_at.is_(None),
                )
                .order_by(Technician.created_at.desc().nullslast(), Technician.id)
                .first()
            )
    except SQLAlchemyError:
        log.exception("closeout_resolve_technician_failed")
        return None
    return str(row[0]) if row else None


def _labor_rate_for(db: Session, technician_id: str | None) -> float:
    """The tech's hourly rate, snapshotted onto the row. Cost readers use the
    stored `hourly_rate` column (labor.py:110, job_costing.py:200) and never
    re-resolve it, so a row written without it is costed at the default rate
    forever. Reuses labor.py's resolver so there is one rate definition.
    """
    from gdx_dispatch.routers.labor import DEFAULT_HOURLY_RATE, _resolve_hourly_rate

    if not technician_id:
        return DEFAULT_HOURLY_RATE
    try:
        return _resolve_hourly_rate(db, technician_id)
    except SQLAlchemyError:
        log.exception("closeout_resolve_rate_failed")
        return DEFAULT_HOURLY_RATE


def _open_job_timers(db: Session, job_uuid: uuid.UUID) -> list[TimeEntry]:
    """Every open timer on this job, whoever started it.

    Scoped to the job rather than the closer: a job can carry N techs
    (JobAssignment), and only the closer's own timer getting closed would
    leave every other tech's running forever — the bug this exists to kill.
    Day-level shifts live in `timeclock_entries` (TimeclockEntry), not here.
    """
    return list(
        db.execute(
            select(TimeEntry)
            .where(
                TimeEntry.job_id == job_uuid,
                TimeEntry.clock_out.is_(None),
                TimeEntry.deleted_at.is_(None),
            )
            .order_by(TimeEntry.clock_in.desc())
        ).scalars().all()
    )


def _owned_closeout_labor_entry(db: Session, job_uuid: uuid.UUID) -> TimeEntry | None:
    """The labor row an earlier closeout of this JOB already wrote.

    Scoped to the job, not the closer: a closeout is a job-level attestation
    (one LIVE JobCloseout per job under the supersede model — a re-closeout
    stamps the prior snapshot superseded and this labor row is restated in
    place; see core/closeouts.py). Keying this on the closer instead would
    dedupe only when the same
    human closes twice, and a tech closing 2h followed by a dispatcher closing
    3h would bill 5h for a 3h job.
    """
    return db.execute(
        select(TimeEntry)
        .where(
            TimeEntry.job_id == job_uuid,
            TimeEntry.notes == CLOSEOUT_LABOR_NOTE,
            TimeEntry.deleted_at.is_(None),
            # A day row or a timer a day-close consumed is never restated
            # (multi-day jobs plan §5.4a, the labor picker).
            TimeEntry.day_closed_at.is_(None),
        )
        .order_by(TimeEntry.clock_in.desc())
        .limit(1)
    ).scalar_one_or_none()


#: How far back a manually-stopped timer stays eligible for restatement.
#: See _stopped_job_timer_for. A tech who stops at 5pm and closes out the next
#: morning is the case this covers; anything older is a different pay period's
#: problem and must not be silently reopened.
STOPPED_TIMER_RESTATE_WINDOW = timedelta(hours=24)


def _stopped_job_timer_for(db: Session, job_uuid: uuid.UUID, user_id: str) -> TimeEntry | None:
    """The caller's OWN per-job timer that they already stopped from mobile.

    Added 2026-08-25 with the mobile Stop button. Without it, a tech who taps
    Stop and then closes out is paid nothing: `_open_job_timers` finds no open
    row, so closeout falls through to the synthetic branch below, and that row
    deliberately carries `user_id = NULL` (an unattested row must not pay the
    person who closed it). The tech's attested hours would then be invisible to
    payroll.py, which groups by user_id — a silent underpayment, and the mirror
    image of the overpayment #154 killed.

    Restating the tech's own stopped row instead keeps ONE row for the job,
    with the identity payroll needs, and cannot double-count: the row is
    already closed at zero payable minutes, so restating it adds the attested
    hours rather than stacking a second entry beside them.

    **Matched by the Stop marker, not by "zero minutes"** — and the difference
    is a real defect this function shipped with for about an hour. The first
    version inferred "a tech stopped this" from `duration_minutes = 0`, which
    is also exactly what `tools/stale_job_timer_repair.py` writes when it
    closes an abandoned timer. Four such rows exist on prod with `clock_in` in
    April-June 2026, two on jobs that still have no closeout. Closing one of
    those out would have restated a four-month-old row — and `_close_labor_entry`
    deliberately never moves `clock_in`, while payroll windows on
    `DATE(clock_in)` — so today's attested hours would post into a pay period
    already run and paid. Matching the marker the Stop path itself writes means
    only rows that path created are eligible.

    The 24h window is the second guard: even a genuine mobile stop stops being
    safe to restate once it is old enough to belong to another pay period. Past
    that, the closeout falls through to the synthetic row exactly as it did
    before this function existed, and logs so the office can enter the hours
    through labor.py rather than the tech losing them silently.

    Deliberately narrow in the other directions too: it will not touch a
    colleague's row (that is `_open_job_timers`' unpaid-close path), an
    office-entered labor row from labor.py (those leave `user_id` NULL), or a
    prior closeout's row (`_owned_closeout_labor_entry` wins ahead of it).
    """
    if not user_id:
        return None
    # Local import: mobile.py owns the marker its own writer stamps, and a
    # module-level import here would couple two routers that are otherwise
    # independent.
    from gdx_dispatch.routers.mobile import MOBILE_AUTO_STOP_LABOR_NOTE, MOBILE_STOP_LABOR_NOTE

    cutoff = datetime.now(UTC) - STOPPED_TIMER_RESTATE_WINDOW
    row = db.execute(
        select(TimeEntry)
        .where(
            TimeEntry.job_id == job_uuid,
            TimeEntry.user_id == user_id,
            TimeEntry.entry_type == "job",
            TimeEntry.clock_out.is_not(None),
            TimeEntry.notes.like(f"{MOBILE_STOP_LABOR_NOTE}%"),
            # A stopped row banks nothing. If it carries minutes, something
            # already attested it and it is not ours to restate.
            or_(TimeEntry.duration_minutes.is_(None), TimeEntry.duration_minutes == 0),
            TimeEntry.deleted_at.is_(None),
            # A Stop-marked timer a day-close consumed at 0 carries the marker
            # and is that day's, never this closeout's (plan §5.4a).
            TimeEntry.day_closed_at.is_(None),
        )
        .order_by(TimeEntry.clock_in.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return None

    # A timer the shift-end sweep stopped (D14) was, until the sweep, an open
    # timer, and `_open_job_timers` restates an open timer at any age. The 24 h
    # window is for a tech's own Stop; applying it here would turn the sweep
    # into the thing that sends a next-morning "Yes" to the user-less row,
    # unpaid. Bounded by the job's latest finish, as day_close's candidates
    # are (§5.4a: a timer from before a finish belongs to that finish), so
    # today's hours never post to an old clock_in's pay period. That bound
    # makes this STRICTER than `_open_job_timers`, which has none: after an
    # API-only `/complete`, an open timer is still restated and a swept one
    # is not.
    # A timer a cancel stopped (GDXA-375) was settled at 0 by the cancel: after
    # a /reactivate it is never restated — the office enters any hours worked
    # before the cancel through labor, once (day_close.CANCEL_STOP_SUFFIX).
    if (row.notes or "").startswith(MOBILE_STOP_LABOR_NOTE + CANCEL_TIMER_NOTE_SUFFIX):
        return None
    if (row.notes or "").startswith(MOBILE_AUTO_STOP_LABOR_NOTE):
        from gdx_dispatch.services import day_close  # noqa: PLC0415

        bound = day_close.latest_finish(db, SimpleNamespace(id=job_uuid))
        if bound is None or day_close.aware(row.clock_in) > bound:
            return row
        return None

    clock_in_at = row.clock_in
    if clock_in_at is not None and clock_in_at.tzinfo is None:
        clock_in_at = clock_in_at.replace(tzinfo=UTC)
    if clock_in_at is not None and clock_in_at < cutoff:
        log.warning(
            "closeout_stale_stopped_timer_not_restated job_id=%s user_id=%s "
            "entry_id=%s clock_in=%s — outside the %sh restate window; attested "
            "hours will not post to this row. Enter them via labor.py if they "
            "are owed.",
            job_uuid, user_id, row.id, clock_in_at.isoformat(),
            int(STOPPED_TIMER_RESTATE_WINDOW.total_seconds() // 3600),
        )
        return None
    return row


def _close_labor_entry(
    entry: TimeEntry,
    now: datetime,
    attested_minutes: int,
    hourly_rate: float | None,
) -> None:
    """Stamp an end on a labor row.

    Only ATTESTED time is payable. Wall-clock elapsed is not evidence of work —
    it measures how long the tech forgot to close the timer, and prod carried
    timers open for months — so an unattested timer closes at zero and is
    surfaced for the office rather than guessed at. Inventing hours here feeds
    job costing (job_costing.py:201) and payroll gross pay directly, and an
    overpayment gets cashed where a missing hour gets reported.

    clock_in is never moved: payroll windows on DATE(clock_in) (payroll.py:250),
    so the hours stay in the day the work happened rather than the day someone
    got around to closing out.
    """
    clock_in_at = entry.clock_in or now
    if clock_in_at.tzinfo is None:
        # SQLite hands back naive datetimes where Postgres is aware.
        clock_in_at = clock_in_at.replace(tzinfo=UTC)

    entry.clock_out = clock_in_at + timedelta(minutes=attested_minutes)
    entry.duration_minutes = attested_minutes
    if hourly_rate is not None:
        entry.hourly_rate = hourly_rate
    entry.updated_at = now


def _closeout_row_to_dict(row: JobCloseout, names: dict[str, str]) -> dict[str, Any]:
    """Closeout snapshot for the read API — plan §1.

    The raw signature blob is NEVER serialized here, by design (audit A4):
    routers/mobile.py:2053 already redacts `signature_data` for a tech
    browsing under techs_see_all_jobs, and a closeout endpoint returning the
    snapshot verbatim would walk straight around that control. What every
    caller actually needs is "was it signed, by whom, when" — metadata. The
    blob (≤1.4MB of pen strokes) stays where it already lives for the PDF
    paths.
    """
    # Parts: WHAT was used, never what it COST (audit round 3). The snapshot
    # rows carry the tech-entered unit_cost/line_total for the costing paths;
    # this read API serves screens any authenticated user can open, so the
    # money columns stay home. name × qty is the attestation.
    parts = [
        {
            "name": p.get("name"),
            "sku": p.get("sku"),
            "qty": p.get("qty"),
            "note": p.get("note"),
            "in_inventory": p.get("in_inventory"),
            "already_billed": p.get("already_billed", False),
        }
        for p in (row.parts_used or [])
        if isinstance(p, dict)
    ]
    return {
        "id": str(row.id),
        "hours_worked": float(row.hours_worked or 0),
        "techs_on_site": int(getattr(row, "techs_on_site", 1) or 1),
        "labor_matrix_item_id": getattr(row, "labor_matrix_item_id", None),
        "notes": row.notes,
        "parts_used": parts,
        "no_parts_used": bool(row.no_parts_used),
        "signature_present": bool((row.signature_data or "").strip()),
        "signed_by": row.signed_by,
        "signed_at": row.signed_at,
        "closed_by_user_id": row.closed_by_user_id,
        "closed_by_name": names.get(str(row.closed_by_user_id) or ""),
        "closed_at": row.closed_at,
        "superseded_at": row.superseded_at,
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
    }


@router.get("/{job_id}/closeout", response_model=None)
def get_job_closeout(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The job's closeout — current snapshot plus full revision history.

    Plan §1: `job_closeouts` was write-only for 2.5 months — hours, work
    notes, the parts attestation and the signer name went into the database
    and never reached any screen, so the office billed blind and the tech
    couldn't confirm what he submitted. This is the read side.

    Access (audit round 3 — flat authentication was NOT acceptable here):
    ``jobs.read_all`` (the office tiers) OR a real claim on the job
    (``core.job_access.job_belongs_to_user`` — assignment, appointment or
    crew row). Mobile built a three-tier grant precisely so one tech can't
    browse another tech's attested records; a desktop endpoint handing the
    same records to any authenticated user would have walked around it. An
    outsider gets the same 404 as a missing job, not a 403 — don't confirm
    the job exists.

    History comes back newest-first, and the current row is deliberately also
    history[0] — the card renders one list with a "current" tag rather than
    stitching two shapes (plan §14 gap 4).
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        job = db.execute(
            select(Job.id).where(Job.id == job_uuid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if job is None:
            return jsonable_response({"detail": "job not found"}, 404)

        # Office tiers hold jobs.read_all; a tech passes via an actual claim
        # on this job. Both resolved with the same helpers the rest of the
        # app uses — no bespoke ACL.
        if not can_read_job(db, tenant_id, request, current_user, job_id):
            return jsonable_response({"detail": "job not found"}, 404)

        from gdx_dispatch.core.closeouts import (
            get_closeout_history,
            get_current_closeout,
        )

        current = get_current_closeout(db, job_uuid)
        history = get_closeout_history(db, job_uuid)

        # Resolve closer ids → display names in one query. users has four
        # name-ish columns; take the first non-empty in the same precedence
        # the rest of the app displays.
        # ORM lookup, not raw SQL (audit round 3): closed_by_user_id is a
        # dashed-uuid varchar while users.id is Uuid(as_uuid=True) — the ORM
        # binds real UUID objects, which is dialect-portable for free, where a
        # hand-written text comparison hits the dashed-vs-32hex trap.
        name_of: dict[str, str | None] = {}
        closer_ids = {str(r.closed_by_user_id) for r in history if r.closed_by_user_id}
        if closer_ids:
            parseable: dict[str, uuid.UUID] = {}
            for cid in closer_ids:
                try:
                    parseable[cid] = uuid.UUID(cid)
                except (ValueError, AttributeError):
                    name_of[cid] = None
            if parseable:
                from gdx_dispatch.models.tenant_models import User

                users = db.execute(
                    select(User).where(User.id.in_(list(parseable.values())))
                ).scalars().all()
                by_uuid = {
                    u.id: (u.full_name or u.name or u.username or u.email)
                    for u in users
                }
                for cid, cuid in parseable.items():
                    name_of[cid] = by_uuid.get(cuid)

        return jsonable_response({
            "job_id": str(job_uuid),
            "closeout": _closeout_row_to_dict(current, name_of) if current else None,
            "history": [_closeout_row_to_dict(r, name_of) for r in history],
        })
    except SQLAlchemyError:
        log.exception("get_job_closeout_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "closeout read failed"}, 500)


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
    # Object-level authz — see update_job. This route is the sharpest case of
    # the class: the closeout is the tech's attested parts + hours, billed
    # labor comes from attested hours ONLY, and the body below runs
    # autodraft_invoice_for_closeout. Hours nobody attested could otherwise
    # reach an invoice. The matching READ (get_job_closeout, just above) has
    # been gated since it shipped; this WRITE was not.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    flags = _load_workflow_flags(tenant_id)
    user_id = _user_id(current_user)

    # Pull job (PR4: bind a UUID object — the Uuid column rejects a raw str
    # on the SQLite test path; the id was already validated above). Locked
    # FIRST, before the timer step (multi-day jobs plan §5.4a): the same lock
    # day-close takes, so a racing "No" commits wholly before this closeout
    # reads timers or after it commits — never between the read and the
    # write, where the timer step would overwrite its day row.
    job = _locked_job(db, job_id)
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)
    _cancelled = _cancelled_refusal(job)
    if _cancelled is not None:
        return _cancelled

    # The tap's shop day feeds only the two day refusals and the 0 h rule.
    from gdx_dispatch.services import day_close as _day_close

    _tz = _day_close.shop_tz(db)
    _tap_day = _tap_shop_day(payload.tapped_at, now, _tz)
    _earlier = _earlier_day_open_refusal(db, job, _tap_day, _tz)
    if _earlier is not None:
        return _earlier
    _later = _day_close.later_day_started(db, job, _tap_day, _tz)
    if _later is not None:
        return jsonable_response({
            "detail": f"Day not recorded: the crew has already started {_later.isoformat()}; tell the office.",
            "code": "later_day_started",
            "date": _later.isoformat(),
        }, 409)

    # Same gate vocabulary as /complete so frontend toasts are uniform.
    # PR5 audit catch: attesting "no parts used" while SUBMITTING parts is a
    # contradiction — reject it before the gates.
    if payload.no_parts_used and payload.parts:
        return jsonable_response(
            {"detail": "no_parts_used cannot be combined with a parts list"},
            422,
        )
    # A return visit without a reason is not accepted — dispatch schedules
    # from the reason, so "yes but I won't say why" is the exact silence
    # this field exists to end. Same 422 shape as the tenant gates so the
    # mobile toast path renders it.
    if payload.needs_return_visit and not (payload.return_visit_reason or "").strip():
        return jsonable_response(
            {"detail": "completion requirements unmet", "missing": ["return_visit_reason"]},
            422,
        )
    missing: list[str] = []
    if flags["require_parts_on_complete"] and not payload.parts and not payload.no_parts_used:
        # Parts logged live while the job was worked already answer this gate
        # (2026-08-12). Without this, the tech who recorded three springs as
        # they installed them is blocked here unless they type all three AGAIN
        # — and each retype mints a SECOND billable row (live capture is
        # source='mobile', the closeout writes source='closeout'; the replace
        # step below only touches its own source, by design). So the gate as
        # written manufactured exactly the double-billing it looks like it
        # prevents. Unbilled rows only: a part already on an invoice is
        # history, not evidence about this closeout.
        _live_used = db.execute(
            select(func.count())
            .select_from(JobPartNeeded)
            .where(
                JobPartNeeded.job_id == str(job.id),
                JobPartNeeded.source.in_(("mobile", "van")),
                JobPartNeeded.status == "used",
                JobPartNeeded.billed_invoice_id.is_(None),
            )
        ).scalar() or 0
        if not _live_used:
            missing.append("parts")
    if (
        flags["require_hours_on_complete"]
        and (payload.hours or 0) <= 0
        # The 0 h "Yes" (Doug, 2026-10-06): today was already closed with
        # "No" and nobody still has a timer running on it today. Otherwise
        # the flag refuses 0 exactly as before.
        and not _day_close.today_closed_by_no(db, job, _tap_day, _tz)
    ):
        missing.append("hours")
    if flags["require_signature_on_complete"]:
        sig = (payload.signature_data or "").strip() or (job.signature_data or "").strip()
        if not sig:
            missing.append("signature")
    # PR5 — optional invoice gate (see /complete for rationale).
    if flags["require_invoice_on_complete"]:
        from gdx_dispatch.core.billing_predicates import job_billed_exists
        billed = db.execute(
            select(Job.id).where(Job.id == job.id, job_billed_exists())
        ).scalar_one_or_none() is not None
        if not billed:
            missing.append("invoice")
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
        # 0) Autodraft reset (2026-08-07) — MUST run before the closeout-
        #    parts replace step below: un-claiming the untouched draft's
        #    part stamps turns those rows back into unbilled closeout rows,
        #    so the replace step deletes them (reversing stock) and this
        #    restatement lands cleanly. The emptied draft is rebuilt in
        #    step 7 with the same invoice number. A draft a human touched
        #    (verified/sent/locked/paid) is left alone — the §12
        #    discrepancy flow owns that story.
        from gdx_dispatch.core.closeout_billing import (
            autodraft_invoice_for_closeout,
            release_untouched_autodraft,
        )
        reused_autodraft = release_untouched_autodraft(
            db, job=job, actor=user_id, request=request
        )

        # 1) Insert one JobPart row per closeout part WHEN the part is in
        #    inventory (has a real parts.id). Free-text closeout lines
        #    (tech wrote "Torsion spring 200x2.0" without picking from
        #    catalog) live ONLY in the JobCloseout snapshot — job_parts
        #    has a FK to parts.id and rejects synthetic UUIDs.
        #    Stock-shortage non-blocking: if the tenant tracks inventory
        #    and the part is tracked, allow qty_on_hand to go negative.
        #    Doug 2026-05-10: blocking completion on a stock count is
        #    a worse UX — the tech is on-site, the part is in the door.
        # PR4-billing-capture: the closeout is the tech's ATTESTED parts
        # statement and job_parts_needed is the single billable spine — so a
        # re-closeout REPLACES the job's still-unbilled closeout-sourced rows
        # (event identity = job_id + source='closeout'); billed rows are
        # never touched. No fuzzy matching against request rows — the office
        # checklist shows both with source badges and decides (audit round 1:
        # sku/name upsert-matching silently undercounted).
        #
        # Audit round 2 (PR4) — two re-closeout repros fixed here:
        # 1. STOCK: the delete must reverse the stock decrement of the rows
        #    it removes, or every "fix a note and resubmit" silently drains
        #    qty_on_hand (repro: identical resubmit 10 → 6).
        # 2. BILLED DUPES: the tech re-attests the FULL parts list, so lines
        #    exactly matching an already-BILLED closeout row (sku/name + qty)
        #    must be suppressed — else they resurrect as unbilled duplicates
        #    on the leak card and one-click bills them twice. A differing qty
        #    deliberately still lands (over-shows for operator review; exact
        #    dedup against billed rows only is NOT the rejected fuzzy-merge
        #    of live capture events).
        _stale_closeout_rows = db.execute(
            select(JobPartNeeded).where(
                JobPartNeeded.job_id == str(job.id),
                JobPartNeeded.source == "closeout",
                JobPartNeeded.billed_invoice_id.is_(None),
            )
        ).scalars().all()
        for _stale in _stale_closeout_rows:
            if _stale.sku:
                _stale_part = db.execute(
                    select(Part).where(Part.sku == _stale.sku, Part.deleted_at.is_(None))
                ).scalar_one_or_none()
                if _stale_part is not None:
                    _stale_part.qty_on_hand = int(_stale_part.qty_on_hand or 0) + int(_stale.quantity or 0)
        db.execute(
            JobPartNeeded.__table__.delete().where(
                JobPartNeeded.job_id == str(job.id),
                JobPartNeeded.source == "closeout",
                JobPartNeeded.billed_invoice_id.is_(None),
            )
        )
        _billed_keys = {
            ((r.sku or "").strip().lower() or (r.part_name or "").strip().lower(), int(r.quantity or 0))
            for r in db.execute(
                select(JobPartNeeded).where(
                    JobPartNeeded.job_id == str(job.id),
                    JobPartNeeded.source == "closeout",
                    JobPartNeeded.billed_invoice_id.is_not(None),
                )
            ).scalars().all()
        }

        part_lines: list[dict] = []
        for p in payload.parts:
            _line_key = ((p.sku or "").strip().lower() or (p.name or "").strip().lower(), int(p.qty))
            if _line_key in _billed_keys:
                # Already billed by an earlier closeout of this job — the
                # re-attestation repeats it; do not re-insert, re-cost, or
                # re-decrement. (Snapshot below still records it.)
                part_lines.append({
                    "part_id": None,
                    "sku": p.sku,
                    "name": p.name,
                    "qty": int(p.qty),
                    "unit_cost": float(p.unit_cost or 0),
                    "line_total": float(p.unit_cost or 0) * int(p.qty),
                    "in_inventory": False,
                    "already_billed": True,
                })
                continue
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
            part_row = None
            if part_uuid is not None:
                part_row = db.execute(
                    select(Part).where(
                        Part.id == part_uuid,
                        Part.deleted_at.is_(None),
                    ),
                ).scalar_one_or_none()
            part_exists = part_row is not None
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
                # PR4: closeout now decrements stock (it recorded JobPart
                # rows but never touched qty_on_hand). Deliberately looser
                # than mobile's insufficient-stock 400: allow-negative,
                # non-blocking (Doug 2026-05-10 — the part is already in the
                # door; a stock count must not block completion). Re-closeout
                # is stock-neutral: the replace step above reversed the
                # deleted rows' decrements.
                part_row.qty_on_hand = int(part_row.qty_on_hand or 0) - int(p.qty)
            # PR4: one billable checklist row per closeout line, 1:1 — two
            # same-sku lines stay two rows. unit_price = catalog SELL price
            # when the part resolved (NOT the closeout unit_cost); NULL means
            # the office prices it at invoicing.
            #
            # 2026-08-19: resolution moved to core.part_pricing. Reading only
            # `part_row.unit_price` made this unreachable for catalog-picked
            # parts, which never carry a part_id by design — the tech attested
            # a part the office had already priced on this very job and it was
            # captured NULL, then dropped from the draft.
            _co_price, _co_price_source = resolve_sell_price_with_source(
                db,
                job_id=str(job.id),
                sku=p.sku,
                part_id=part_uuid if part_exists else None,
                customer_id=job.customer_id,
            )
            db.add(JobPartNeeded(
                id=str(uuid.uuid4()),
                company_id=tenant_id,
                job_id=str(job.id),
                part_name=p.name,
                sku=p.sku,
                quantity=int(p.qty),
                status="used",
                source="closeout",
                unit_price=_co_price,
                # Migration 075 — the lane, beside the number. Without it this
                # path would write a price with no provenance and make the
                # column a half-truth: some rows answerable, some not, and no
                # way to tell which from the data.
                price_source=_co_price_source,
                # PR5: the tech's free-text explanation (a part not in the
                # system) leads; the cost breadcrumb follows.
                notes=" — ".join(
                    s for s in (
                        (p.note or "").strip() or None,
                        f"cost ${unit_cost:.2f} ea" if unit_cost else None,
                    ) if s
                ) or None,
                requested_by_user_id=user_id,
                created_at=now,
                updated_at=now,
            ))
            part_lines.append({
                "part_id": str(part_uuid) if part_exists and part_uuid else None,
                "sku": p.sku,
                "name": p.name,
                "qty": int(p.qty),
                "unit_cost": unit_cost,
                "line_total": line_total,
                "note": (p.note or "").strip() or None,
                # Mark whether the row was reflected in inventory or
                # snapshot-only — useful for the office's review (RFB).
                "in_inventory": part_exists,
            })

        # 2) Labor trail. The mobile arrival auto-clocks-in a per-job timer
        #    (mobile.py `mobile_job_arrived`) and closeout is the only thing
        #    that can end it — it never did, so prod carried timers open for
        #    months while closeout added a second, synthetic row beside them.
        #
        #    Two readers decide whether a row counts and the old code
        #    satisfied neither: payroll groups by `user_id` and skips a NULL
        #    clock_out (payroll.py:246-263) — the timer had no clock_out and
        #    the synthetic had no user_id, so NO job hours were payable at
        #    all — while costing reads the stored `hourly_rate` (labor.py:110,
        #    job_costing.py:201) and never re-resolves it, so every row costed
        #    at the default rate. Rows written here set all three.
        #
        #    Attested hours are the only payable input. Elapsed measures how
        #    long a timer went unclosed, not work done, so an unattested timer
        #    closes at zero and is surfaced rather than guessed at.
        technician_id = _resolve_technician_id(db, user_id)
        attested_minutes = int(round(float(payload.hours or 0) * 60))

        timers = _open_job_timers(db, job.id)

        # The job's single attested-labor row. Order matters: an existing
        # closeout row wins so a re-closeout RESTATES it (a second submit, or
        # a re-arrival then re-closeout, must not stack a second row); then
        # the caller's own arrival timer; then a synthetic.
        target = _owned_closeout_labor_entry(db, job.id)
        if target is None:
            target = next((t for t in timers if t.user_id == user_id), None)
        if target is None:
            # The tech stopped their own job clock from mobile before closing
            # out. Their row is closed but banked nothing; restate it so the
            # attested hours land under their identity instead of on the
            # unattributed synthetic below, which payroll cannot see.
            target = _stopped_job_timer_for(db, job.id, user_id)
        if target is None and attested_minutes:
            # No timer — the closer attests the work happened (a dispatcher
            # closing for a forgetful tech, or a tech who never tapped
            # "I'm here"). Identity stays exactly as it was before this
            # change: tech_id = the closer, user_id NULL. Stamping the
            # closer's user_id would make payroll pay THEM for a tech's
            # hours; NULL keeps the row unpayable (as today) while job
            # costing still counts it. Who owns an unattested row is a
            # product decision, not a bug fix.
            #
            # DECIDED (Doug, 2026-08-28, #529): this row is job-costing
            # evidence, NOT payroll. The day clock is the paid time; the
            # per-job clock and this attested row exist for costing. So a
            # desk closer's row stays user_id NULL on purpose — payroll is
            # supposed not to see it — and the assigned tech is paid by their
            # day clock regardless of who closes the job. Known residual: the
            # rate below resolves from the CLOSER's technician record, not
            # the assigned tech's (moot while no per-tech rates are set).
            target = TimeEntry(
                id=uuid.uuid4(),
                company_id=tenant_id,
                job_id=job.id,
                tech_id=user_id,
                clock_in=now - timedelta(minutes=attested_minutes),
                entry_type="work",
                created_at=now,
            )
            db.add(target)

        if target is not None:
            _close_labor_entry(
                target, now, attested_minutes, _labor_rate_for(db, technician_id)
            )
            # Set, never appended. _owned_closeout_labor_entry finds the row
            # by `notes == CLOSEOUT_LABOR_NOTE` exactly, so appending to an
            # existing note (e.g. the mobile Stop's elapsed span) would hide
            # this row from the next re-closeout and mint a SECOND labor row —
            # double-billed hours. The elapsed span is not lost: the clock-out
            # audit event carries elapsed_minutes permanently.
            target.notes = CLOSEOUT_LABOR_NOTE

        # Every other open timer on the job closes UNPAID. The caller attested
        # for the job, not for a colleague's clock, and elapsed is not
        # evidence. Zero + a warning is honest; a guess gets cashed.
        for timer in timers:
            if timer is target:
                continue
            _close_labor_entry(timer, now, 0, None)
            log.warning(
                "closeout_unattested_timer_closed",
                extra={
                    "job_id": str(job.id),
                    "entry_id": str(timer.id),
                    "timer_user_id": timer.user_id,
                    "closed_by": user_id,
                },
            )

        # 3) JobCloseout snapshot row — under the supersede model (plan §12).
        #    This used to be a bare INSERT: the comment at
        #    _owned_closeout_labor_entry says "one JobCloseout per job,
        #    re-closeout restates it", but that was only ever true of the
        #    LABOR row — the snapshot was append-only, so a re-closeout left
        #    TWO live rows and every job_id reader either 500'd
        #    (MultipleResultsFound) or double-counted (tech_efficiency).
        #    Now the prior snapshot is stamped superseded — never deleted or
        #    edited, an attestation is evidence — and the new row links back.
        from gdx_dispatch.core.closeouts import get_current_closeout

        prior_closeout = get_current_closeout(db, job.id)
        prior_snapshot: dict[str, Any] | None = None
        if prior_closeout is not None:
            prior_closeout.superseded_at = now
            prior_closeout.updated_at = now
            # Captured pre-commit for the audit event: §13's rule is that a
            # change records OLD and NEW explicitly — reconstructing a delta
            # by diffing two events is how changes get missed.
            prior_snapshot = {
                "id": str(prior_closeout.id),
                "hours": float(prior_closeout.hours_worked or 0),
                "techs_on_site": int(getattr(prior_closeout, "techs_on_site", 1) or 1),
                "no_parts_used": bool(prior_closeout.no_parts_used),
                "parts_count": len(prior_closeout.parts_used or []),
                "closed_at": prior_closeout.closed_at.isoformat()
                if prior_closeout.closed_at
                else None,
            }

        closeout = JobCloseout(
            id=uuid.uuid4(),
            job_id=uuid.UUID(job_id),
            parts_used=part_lines,
            no_parts_used=bool(payload.no_parts_used),
            hours_worked=float(payload.hours or 0),
            techs_on_site=int(payload.techs_on_site or 1),
            labor_matrix_item_id=(payload.labor_matrix_item_id or None),
            signature_data=payload.signature_data or None,
            signed_by=payload.signed_by or None,
            signed_at=now if (payload.signature_data or "").strip() else None,
            notes=(payload.notes or "").strip() or None,
            closed_by_user_id=user_id,
            closed_at=now,
            created_at=now,
            updated_at=now,
            supersedes_id=(prior_closeout.id if prior_closeout is not None else None),
        )
        db.add(closeout)

        # 4) Flip the job to completed. Stamp signature for back-compat
        #    with the Phase 1 /complete reader path that checks job.signature_data.
        #    Its visits first (F1).
        _finish_visits(db, job, now, current_user)
        job.lifecycle_stage = "completed"
        job.status = "Completed"
        job.completed_at = now
        job.dispatch_status = "done"
        if payload.signature_data:
            job.signature_data = payload.signature_data
        if payload.notes:
            job.notes = (job.notes + "\n\n" if job.notes else "") + payload.notes.strip()
        job.updated_at = now

        # 5) Parts the job still NEEDS (Doug 2026-08-04) — filed as the same
        #    row the job-screen Parts card creates (status='needed',
        #    source='request') so they land in the office Parts-to-Order
        #    queue unchanged. Deliberately NOT source='closeout': that tag
        #    means "attested used" and the restatement replace step above
        #    deletes+reinserts it — a needed row must survive a re-closeout
        #    once dispatch starts acting on it (ordered/received + ETA).
        #
        #    Replay guard (audit 2026-08-04): a queued offline closeout can
        #    reach this handler twice (idempotency cache is redis — TTL
        #    expiry / pass-through when redis is down), and a restatement
        #    re-submits the same list. Without a guard the office orders
        #    double springs. Exact-match dedup only — same requester, name
        #    (case-folded), sku and qty against this job's unbilled request
        #    rows: a different qty or part is a new ask and always lands.
        #    Matching an open Parts-card row is equally correct — same
        #    person asking for the same thing on the same job IS one ask.
        _open_request_keys = {
            (
                (r.part_name or "").strip().lower(),
                (r.sku or "").strip().lower() or None,
                int(r.quantity or 0),
                r.requested_by_user_id,
            )
            for r in db.execute(
                select(JobPartNeeded).where(
                    JobPartNeeded.job_id == str(job.id),
                    JobPartNeeded.source == "request",
                    JobPartNeeded.billed_invoice_id.is_(None),
                )
            ).scalars().all()
        }
        parts_to_order_new = 0
        for p in payload.parts_to_order:
            _key = (
                p.name.strip().lower(),
                (p.sku or "").strip().lower() or None,
                int(p.qty),
                user_id,
            )
            if _key in _open_request_keys:
                continue
            _open_request_keys.add(_key)
            parts_to_order_new += 1
            db.add(JobPartNeeded(
                id=str(uuid.uuid4()),
                company_id=tenant_id,
                job_id=str(job.id),
                part_name=p.name.strip(),
                sku=(p.sku or "").strip() or None,
                quantity=int(p.qty),
                urgency=p.urgency,
                status="needed",
                source="request",
                notes=(p.note or "").strip() or None,
                requested_by_user_id=user_id,
                created_at=now,
                updated_at=now,
            ))

        # 6) Return visit. One OPEN child per job: a re-closeout (or an
        #    offline replay that slipped past the idempotency cache) reuses
        #    the existing open child instead of minting a sibling — dispatch
        #    schedules ONE return trip, not one per submit.
        return_visit_job_id: str | None = None
        reason_text = (payload.return_visit_reason or "").strip()
        if payload.needs_return_visit:
            existing_child = db.execute(
                select(Job).where(
                    Job.parent_job_id == job.id,
                    Job.is_return_visit.is_(True),
                    Job.deleted_at.is_(None),
                    Job.lifecycle_stage.notin_(["completed", "cancelled"]),
                )
            ).scalars().first()
            if existing_child is not None:
                return_visit_job_id = str(existing_child.id)
                # Reuse must not discard the attested WHY (audit
                # 2026-08-04: the 422 forces the reason, so throwing it
                # away on this branch would make the requirement theater).
                # Append it to the open child — dispatch sees every reason
                # given — unless this exact text is already there (replay).
                if reason_text and reason_text not in (existing_child.description or ""):
                    existing_child.description = (
                        (existing_child.description + "\n\n" if existing_child.description else "")
                        + reason_text
                    )
                    existing_child.updated_at = now
            else:
                # Same guarded allocation as spawn_return_visit: the number
                # counter commits on its own session. On failure the child
                # keeps job_number NULL — this code has no fallback; the
                # jobs list renders the UUID prefix for NULL numbers — and
                # the closeout is never blocked on numbering.
                #
                # GDXA-158: that last promise was false on Postgres. The
                # counter runs on its OWN session (``cdb``), but the customer
                # name is read on the CALLER's (``db``) — so a failed read
                # there aborted this request's transaction, and the
                # ``db.add(child)``/``commit()`` below died with 25P02. The
                # closeout WAS blocked on numbering, by the one read nobody
                # counted as part of numbering. ``contained_read`` wraps only
                # that read: pure, on ``db``, inside the existing ``try``
                # (core.database rules 1/2).
                assigned_number: str | None = None
                try:
                    with SessionLocal() as cdb:
                        cust_name = None
                        if job.customer_id:
                            with contained_read(db):
                                cust = db.execute(
                                    select(Customer.name).where(Customer.id == job.customer_id)
                                ).first()
                            if cust:
                                cust_name = cust[0]
                        assigned_number = next_job_number(cdb, tenant_id, customer_name=cust_name)
                        cdb.commit()
                except Exception:
                    log.exception("closeout_return_visit_number_alloc_failed")
                child = Job(
                    id=uuid.uuid4(),
                    title=f"Return visit: {job.title}"[:200],
                    # The WHY is the description — it's what dispatch reads
                    # when slotting the trip.
                    description=reason_text,
                    customer_id=job.customer_id,
                    job_type=canonical_job_type(job.job_type) or SERVICE_CALL,
                    status="Service Call",
                    company_id=tenant_id,
                    parent_job_id=job.id,
                    job_number=assigned_number,
                    created_at=now,
                    updated_at=now,
                    is_demo=False,
                    lifecycle_stage="service_call",
                    dispatch_status="unassigned",
                    billing_status="unbilled",
                    priority=job.priority or "Normal",
                    is_return_visit=True,
                )
                db.add(child)
                return_visit_job_id = str(child.id)
                log_audit_event_sync(
                    db=db, tenant_id=tenant_id, user_id=user_id,
                    action="return_visit_spawned",
                    entity_type="job",
                    entity_id=return_visit_job_id,
                    details={
                        "original_job_id": str(job.id),
                        "reason": reason_text[:500],
                        "source": "closeout",
                    },
                    ip_address=request.client.host if request.client else None,
                    request=request,
                )

        # 7) Autodraft (Doug 2026-08-07): the closeout mints (or rebuilds)
        #    a DRAFT invoice priced from this attestation — labor lanes +
        #    priced closeout parts — so Ready-for-Billing offers "review
        #    the draft" instead of a blank form. Savepoint-guarded: a
        #    pricing failure must never cost the tech their closeout; the
        #    job then just lands in RFB on the classic Create Invoice path.
        #    The flush puts everything written so far inside the OUTER
        #    transaction, so a savepoint rollback can only undo the draft.
        db.flush()
        autodraft_invoice = None
        try:
            with db.begin_nested():
                autodraft_invoice = autodraft_invoice_for_closeout(
                    db, tenant_id=tenant_id, job=job, closeout=closeout,
                    reuse_invoice=reused_autodraft,
                )
        except Exception:  # noqa: BLE001
            log.exception(
                "closeout_autodraft_failed",
                extra={"tenant_id": tenant_id, "job_id": job_id},
            )
            autodraft_invoice = None
        if autodraft_invoice is not None:
            log_audit_event_sync(
                db=db, tenant_id=tenant_id, user_id=user_id,
                action="invoice_autodrafted",
                entity_type="invoice",
                entity_id=str(autodraft_invoice.id),
                details={
                    "job_id": str(job.id),
                    "invoice_number": autodraft_invoice.invoice_number,
                    "total": float(autodraft_invoice.total or 0),
                    "rebuilt": reused_autodraft is not None,
                },
                ip_address=request.client.host if request.client else None,
                request=request,
            )

        # Audit events join the SAME transaction as the closeout (adversarial
        # audit, round 2). The old shape committed the closeout first and the
        # events in a second commit — so a failed audit write 500'd a closeout
        # that was already durable (the tech retried and stacked a spurious
        # restatement), and could silently lose the job_closeout_superseded
        # event that §12's discrepancy flow keys on. log_audit_event_sync only
        # add()+flush()es, so it composes into this transaction.
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=user_id,
            action="job_closeout",
            entity_type="job",
            entity_id=str(job.id),
            details={
                "closeout_id": str(closeout.id),
                "parts_count": len(part_lines),
                "hours": float(payload.hours or 0),
                "techs_on_site": int(payload.techs_on_site or 1),
                "signature_present": bool(payload.signature_data),
                "supersedes_id": prior_snapshot["id"] if prior_snapshot else None,
                "needs_return_visit": bool(payload.needs_return_visit),
                # The attested WHY + asked-for parts live HERE durably
                # (audit 2026-08-04): the JobCloseout snapshot has no
                # columns for them yet, and child.description is editable —
                # the append-only audit row is what answers "what did the
                # tech say at closeout?" after the fact.
                "return_visit_reason": (reason_text[:500] or None) if payload.needs_return_visit else None,
                "return_visit_job_id": return_visit_job_id,
                "parts_to_order": [
                    {
                        "name": p.name.strip(),
                        "sku": (p.sku or "").strip() or None,
                        "qty": int(p.qty),
                        "urgency": p.urgency,
                    }
                    for p in payload.parts_to_order
                ] or None,
                "parts_to_order_new": parts_to_order_new,
            },
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        if prior_snapshot is not None:
            # §13: a restatement is its own auditable fact, with OLD and NEW
            # side by side — this event is what the office's "closeout changed
            # after billing" discrepancy flow (plan §12) will key on.
            log_audit_event_sync(
                db=db, tenant_id=tenant_id, user_id=user_id,
                action="job_closeout_superseded",
                entity_type="job",
                entity_id=str(job.id),
                details={
                    "old": prior_snapshot,
                    "new": {
                        "id": str(closeout.id),
                        "hours": float(payload.hours or 0),
                        "techs_on_site": int(payload.techs_on_site or 1),
                        "no_parts_used": bool(payload.no_parts_used),
                        "parts_count": len(part_lines),
                    },
                },
                ip_address=request.client.host if request.client else None,
                request=request,
            )

        db.flush()
        db.commit()

        return jsonable_response({
            "ok": True,
            "closeout_id": str(closeout.id),
            "job_id": str(job.id),
            "completed_at": job.completed_at,
            "parts_count": len(part_lines),
            "return_visit_job_id": return_visit_job_id,
            # Rows actually INSERTED — a replay/restatement that deduped to
            # zero reports zero, not the size of the re-sent list.
            "parts_to_order_count": parts_to_order_new,
            # The machine-drafted invoice, when one was minted/rebuilt —
            # None when the job has an estimate, a live invoice, no
            # customer, or nothing priceable landed.
            "autodraft_invoice_id": (
                str(autodraft_invoice.id) if autodraft_invoice is not None else None
            ),
            "autodraft_invoice_number": (
                autodraft_invoice.invoice_number if autodraft_invoice is not None else None
            ),
        }, 201)
    except IntegrityError as exc:
        # Two people closing out the same job within the same moment: B's
        # get_current_closeout ran before A committed, so B's INSERT hits the
        # uq_job_closeouts_live partial index at flush. The rollback is clean
        # (parts/stock/timer mutations all live in this one transaction) and
        # this is a coordination event, not breakage — say so, instead of a
        # 500 with psycopg2 internals on a phone screen.
        db.rollback()
        # Postgres names the index; SQLite reports "UNIQUE constraint failed:
        # job_closeouts.job_id". Match both, or the test harness exercises a
        # branch prod never takes (and vice versa).
        _msg = str(exc)
        if "uq_job_closeouts_live" in _msg or (
            "UNIQUE constraint failed" in _msg and "job_closeouts" in _msg
        ):
            log.warning(
                "closeout_job_concurrent_conflict",
                extra={"tenant_id": tenant_id, "job_id": job_id},
            )
            return jsonable_response(
                {
                    "detail": (
                        "Someone else closed this job out at the same moment. "
                        "Reload the job to review their closeout, then restate "
                        "it if yours differs."
                    )
                },
                409,
            )
        log.exception("closeout_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)
    except SQLAlchemyError:
        db.rollback()
        log.exception("closeout_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


@router.get("/ready-for-billing", response_model=None, dependencies=[Depends(require_permission("invoices.read_all"))])
def ready_for_billing(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Jobs completed but not yet billed — ready for billing."""
    try:
        # Filter on lifecycle_stage (canonical post-D99) rather than the legacy
        # `status` varchar — QB-imported jobs have NULL status but
        # lifecycle_stage='completed', so the old `Job.status IN (Complete...)`
        # filter silently undercounted by ~50% on GDX prod (S114 reconcile:
        # /api/invoices/summary returned 8, this endpoint returned 4).
        # PR2-billing-capture: "has no invoice at all" → the canonical billed
        # predicate. The old LEFT-JOIN-IS-NULL treated a VOIDED invoice (and
        # the fabricated $0 draft) as billing the job, so those jobs vanished
        # from this queue forever — disagreeing with the display state, which
        # already excluded void.
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        # 055: resolved = billed OR office-marked not billable — the marked
        # jobs leave this queue (that's the whole point of the mark).
        from gdx_dispatch.core.billing_predicates import job_billing_resolved
        results = db.execute(
            select(Job, Customer)
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .where(
                Job.lifecycle_stage == "completed",
                Job.deleted_at.is_(None),
                ~job_billing_resolved(),
            )
            .order_by(Job.created_at.desc())
            .limit(100)
        ).all()
        # Autodraft (2026-08-07): drafts no longer settle the queue — a job
        # with a live draft stays HERE, and the row carries the draft so the
        # UI offers "Review invoice" instead of "Create Invoice". Latest
        # live non-deposit draft per job (created_at asc + dict overwrite =
        # last one wins).
        drafts_by_job: dict[str, dict[str, Any]] = {}
        job_ids = [job.id for job, _ in results]
        if job_ids:
            for inv in db.execute(
                select(Invoice).where(
                    Invoice.job_id.in_(job_ids),
                    Invoice.deleted_at.is_(None),
                    Invoice.status == "draft",
                    or_(Invoice.billing_type.is_(None), Invoice.billing_type != "deposit"),
                ).order_by(Invoice.created_at.asc())
            ).scalars():
                drafts_by_job[str(inv.job_id)] = {
                    "draft_invoice_id": str(inv.id),
                    "draft_invoice_number": inv.invoice_number,
                    "draft_total": float(inv.total or 0),
                    "draft_origin": inv.origin,
                }
        return [
            {
                "id": str(job.id),
                "title": job.title or job.description or "",
                "customer_name": customer.name if customer else "",
                "customer_id": str(job.customer_id) if job.customer_id else None,
                "status": job.status,
                "created_at": str(job.created_at) if job.created_at else None,
                **drafts_by_job.get(
                    str(job.id),
                    {
                        "draft_invoice_id": None,
                        "draft_invoice_number": None,
                        "draft_total": None,
                        "draft_origin": None,
                    },
                ),
            }
            for job, customer in results
        ]
    except Exception:
        log.exception("ready_for_billing_failed")
        with contextlib.suppress(Exception):
            db.rollback()
        return []


@router.get(
    "/{job_id}/closeout-billing-suggestion",
    response_model=None,
    dependencies=[Depends(require_permission("invoices.read_all"))],
)
def closeout_billing_suggestion(
    job_id: str,
    request: Request,
    invoice_id: str | None = None,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """What the closeout says this job's bill should start from.

    The office create-invoice screen (/billing/new) prefilled from the
    ESTIMATE and offered parts — but a service job with neither showed a
    blank form: the tech's attested hours and field notes never reached
    the person typing the bill (Doug 2026-08-07: "click invoice — it does
    not show hours or notes from the job"). This read-only endpoint hands
    the UI a priced labor line (same core/billing_lanes math the autodraft
    and mobile paths use) plus the closeout context to display. Nothing is
    written or claimed — the operator still confirms every line.

    ``labor_lines`` carries whichever lane's lines apply: the service lane's
    one or two hourly lines (exactly one of them names the day rows it priced
    in ``time_entry_ids``, which the create request sends back to claim), or
    the install lane's single matrix line.

    ``?invoice_id=`` is the invoice asking (an edit of an existing invoice):
    the day rows it already holds count as unbilled for it, and its own labor
    line does not count as "first hour already charged".
    """
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    job = db.execute(
        select(Job).where(Job.id == jid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)

    from gdx_dispatch.core.billing_lanes import (
        install_labor_line,
        job_labor_lines,
        lane_for_job,
    )
    from gdx_dispatch.core.closeouts import get_current_closeout
    from gdx_dispatch.modules.proposals.models import Estimate

    asking: uuid.UUID | None = None
    if invoice_id:
        try:
            asking = uuid.UUID(str(invoice_id))
        except (ValueError, AttributeError):
            return jsonable_response({"detail": "invoice_id is not a valid id"}, 422)
        owner = db.execute(
            select(Invoice.job_id).where(Invoice.id == asking, Invoice.deleted_at.is_(None))
        ).first()
        # Only an invoice on THIS job may ask: another job's invoice would
        # make its own rows and first hour "this invoice's" here.
        if owner is None or owner[0] != jid:
            return jsonable_response({"detail": "invoice not found on this job"}, 404)

    estimate_exists = db.execute(
        select(Estimate.id).where(
            Estimate.job_id == job.id,
            Estimate.status == "accepted",
            Estimate.deleted_at.is_(None),
        ).limit(1)
    ).first() is not None

    def _wire(line: dict) -> dict:
        out = dict(line)
        for k in ("quantity", "unit_price", "line_total", "estimated_man_hours"):
            if out.get(k) is not None:
                out[k] = float(out[k])
        return out

    # The day rows are offered only on a completed job (multi-day jobs plan
    # §5.4a Billing), so a hand-made mid-job invoice is never offered the same
    # days again at the end. Read BEFORE the no-closeout return: a job finished
    # by Close-without-work or /complete has no closeout, and its days would
    # otherwise reach no invoice (round 35).
    completed = (job.lifecycle_stage or "").lower() == "completed"
    closeout = get_current_closeout(db, jid)
    if closeout is None:
        return jsonable_response({
            "has_closeout": False,
            "estimate_exists": estimate_exists,
            "closeout": None,
            "labor_lines": [
                _wire(ln) for ln in job_labor_lines(
                    db, job, None, invoice_id=asking, include_day_rows=completed,
                )
            ],
            # Mobile and van captures happen on jobs that never get a
            # closeout, and van rows are exactly what started billing in
            # v1.69. Omitting the key here made the warning unreachable for
            # them.
            "duplicate_part_warnings": duplicate_capture_groups(db, str(jid)),
        })

    labor_lines: list[dict[str, Any]] = []
    lane = lane_for_job(job.job_type)
    # `source` is NOT decoration. These two lanes produce numbers that mean
    # different things, and callers were left to guess which they had:
    #
    #   install -> a QUOTED FLAT PRICE from a labor-matrix row. It is a
    #              contract price and says nothing about how long the work took.
    #   service -> the tech's ATTESTED hours x the rate. This is evidence.
    #
    # Billed labor comes from attested hours only, so a caller that labels a
    # matrix flat price as "attested hours" records a quoted number as evidence
    # — inverting the invariant. The invoice Add Labor picker consumed this
    # field and did exactly that until 2026-08-20. Say which lane it is here,
    # at the source, rather than making every consumer re-derive it.
    if lane == "install" and getattr(closeout, "labor_matrix_item_id", None):
        _install = install_labor_line(db, closeout.labor_matrix_item_id)
        if _install is not None:
            labor_lines.append({
                "description": _install.description,
                "quantity": _install.quantity,
                "unit_price": float(_install.unit_price),
                "line_total": float(_install.line_total),
                "source": "matrix",
                # Which matrix row quoted it — the provenance an invoice line
                # needs, and which this payload used to drop on the floor.
                "labor_price_item_id": str(closeout.labor_matrix_item_id),
                "man_hours": None,
            })
    if lane == "service":
        labor_lines = [
            _wire(ln) for ln in job_labor_lines(
                db, job, closeout, invoice_id=asking, include_day_rows=completed,
            )
        ]

    # The tech's JOB notes too (Doug 2026-08-07 round 2: "it is missing the
    # notes the tech put on it") — the real work summary usually lives in
    # job_notes (the mobile Add-note flow), NOT the closeout's own notes
    # field. Newest last, capped: the card is context, not an archive.
    # visibility rides along so the UI can badge internal notes before the
    # operator copies one onto a customer-facing invoice.
    from gdx_dispatch.models.tenant_models import JobNote
    job_notes = [
        {
            "body": (n.body or "")[:2000],
            "author_name": n.author_name,
            "visibility": n.visibility,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in db.execute(
            # job_notes.job_id is TEXT (Flask-era schema) — bind the dashed
            # string, never a UUID object (PG: operator does not exist
            # text = uuid; the model's own header warns about this).
            select(JobNote)
            .where(JobNote.job_id == str(jid), JobNote.deleted_at.is_(None))
            .order_by(JobNote.created_at.asc())
            .limit(5)
        ).scalars()
    ]

    return jsonable_response({
        "has_closeout": True,
        "estimate_exists": estimate_exists,
        "closeout": {
            "hours_worked": float(closeout.hours_worked or 0),
            "techs_on_site": int(getattr(closeout, "techs_on_site", 1) or 1),
            "notes": closeout.notes,
            "no_parts_used": bool(closeout.no_parts_used),
            "closed_at": closeout.closed_at.isoformat() if closeout.closed_at else None,
        },
        "job_notes": job_notes,
        "labor_lines": labor_lines,
        # Parts this job captured more than once and that are still unbilled
        # (2026-08-19). Capture rows are never machine-merged — AUDIT-R1 ruled
        # any automatic dedup either undercounts or double-counts — so the
        # office is told instead, before it verifies a draft. Empty list is the
        # normal case.
        "duplicate_part_warnings": duplicate_capture_groups(db, str(jid)),
    })


# --- Day close ("No" on the closeout sheet) — multi-day jobs plan §5.4a ---
#
# "Is this job finished?" -> No closes one shop day of a job: the visits the
# closer ticks, and each tapped-in person's attested hours for that day. The
# sheet's rows come from GET /day-log; the reads live in services/day_close.py
# so this route, the closeout, /complete and /close-without-work ask one
# predicate.

DAY_CLOSE_MAX_ADDED = 10
DAY_CLOSE_NOTE_MAX = 2000


def _day_close_hours(raw: Any, where: str) -> tuple[int | None, str | None]:
    """Attested hours → whole minutes, or a 422 message. 0 < hours ≤ 24."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, f"{where}: hours must be a number"
    hours = float(raw)
    if not (0 < hours <= 24):
        return None, f"{where}: hours must be more than 0 and at most 24"
    minutes = int(round(hours * 60))
    if minutes <= 0:
        return None, f"{where}: hours must be at least one minute"
    return minutes, None


def _parse_day_close_body(body: Any) -> tuple[dict | None, str | None]:
    """The shape 422s. They read the body only — never server time or stored
    state — so a lost-response replay passes them exactly when the original
    did (round 27)."""
    from datetime import date as _date

    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    raw_day = body.get("day")
    try:
        if not isinstance(raw_day, str) or len(raw_day) != 10:
            raise ValueError
        day = _date.fromisoformat(raw_day)
    except ValueError:
        return None, "day is required as an ISO date (YYYY-MM-DD)"
    raw_closed = body.get("closed_at")
    try:
        if not isinstance(raw_closed, str):
            raise ValueError
        closed_at = datetime.fromisoformat(raw_closed)
    except ValueError:
        return None, "closed_at is required as an ISO timestamp with a zone"
    if closed_at.tzinfo is None or closed_at.utcoffset() is None:
        return None, "closed_at is required as an ISO timestamp with a zone"

    visits_raw = body.get("visits") or []
    if not isinstance(visits_raw, list):
        return None, "visits must be a list of visit ids"
    visits: list[uuid.UUID] = []
    for v in visits_raw:
        try:
            vid = uuid.UUID(str(v)) if isinstance(v, str) else None
        except ValueError:
            vid = None
        if vid is None:
            return None, "visits must be a list of visit ids"
        if vid in visits:
            return None, "a visit is listed more than once"
        visits.append(vid)

    people_raw = body.get("people") or []
    if not isinstance(people_raw, list):
        return None, "people must be a list of {user_id, hours}"
    people: list[tuple[str, int]] = []
    for p in people_raw:
        if not isinstance(p, dict) or not isinstance(p.get("user_id"), str) or not p["user_id"].strip():
            return None, "people must be a list of {user_id, hours}"
        uid = p["user_id"].strip()
        if any(uid == seen for seen, _m in people):
            return None, "a person is listed more than once"
        minutes, err = _day_close_hours(p.get("hours"), "people")
        if err:
            return None, err
        people.append((uid, minutes))

    added_raw = body.get("added") or []
    if not isinstance(added_raw, list):
        return None, "added must be a list of {hours}"
    if len(added_raw) > DAY_CLOSE_MAX_ADDED:
        return None, f"added holds at most {DAY_CLOSE_MAX_ADDED} helpers"
    added: list[int] = []
    for a in added_raw:
        if not isinstance(a, dict):
            return None, "added must be a list of {hours}"
        minutes, err = _day_close_hours(a.get("hours"), "added")
        if err:
            return None, err
        added.append(minutes)

    if not (visits or people or added):
        return None, "nothing to close: list a visit, a person or an added helper"
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return None, "note must be text"
    if note is not None and len(note) > DAY_CLOSE_NOTE_MAX:
        return None, f"note is at most {DAY_CLOSE_NOTE_MAX} characters"
    note = (note or "").strip() or None
    return {
        "day": day,
        # The submission's key, stored as UTC: a zone is only a spelling of
        # the instant, and SQLite would otherwise store the wall clock.
        "closed_at": closed_at.astimezone(UTC),
        "visits": visits,
        "people": people,
        "added": added,
        "note": note,
    }, None


def _day_close_conflict(code: str, detail: str, **extra: Any) -> JSONResponse:
    return jsonable_response({"detail": detail, "code": code, **extra}, 409)


def _already_closed(db: Session, job: Job, day: Any, tz: str) -> dict:
    """D's day rows, each with its person, hours and closer — what the sheet
    shows as "Already closed by <name> with N h"."""
    from gdx_dispatch.services import day_close

    key = day.isoformat()
    return {"rows": [
        {"person_name": r["person_name"], "hours": r["hours"], "closed_by": r["closed_by"]}
        for r in day_close.day_log(db, job, tz) if r["date"] == key
    ]}


def _next_visit(db: Session, job: Job) -> dict | None:
    from gdx_dispatch.services import day_close

    current = day_close.current_visits(db, job)
    if not current:
        return None
    nxt = min(current, key=lambda v: (day_close.aware(v.start_at), str(v.id)))
    return {"id": str(nxt.id), "start_at": day_close.aware(nxt.start_at).isoformat()}


def _day_close_landed(db: Session, job: Job, closed_at: datetime) -> list[TimeEntry] | None:
    """The replay key: a time row or a visit on this job carrying
    ``day_closed_at == closed_at``. Returns that submission's rows (possibly
    none, for a visits-only one), or None when it never landed. Compared in
    Python on aware UTC values, so SQLite's naive storage still matches."""
    from gdx_dispatch.models.tenant_models import Appointment
    from gdx_dispatch.services.day_close import aware

    rows = [
        t for t in db.execute(
            select(TimeEntry).where(TimeEntry.job_id == job.id, TimeEntry.day_closed_at.is_not(None))
        ).scalars().all()
        if aware(t.day_closed_at) == closed_at
    ]
    if rows:
        return rows
    for a in db.execute(
        select(Appointment).where(Appointment.job_id == job.id, Appointment.day_closed_at.is_not(None))
    ).scalars().all():
        if aware(a.day_closed_at) == closed_at:
            return []
    return None


def _day_row_out(rows: list[TimeEntry], names: dict[str, str | None]) -> list[dict]:
    return [
        {
            "id": str(r.id),
            "person_name": names.get(str(r.id)),
            "hours": round((r.duration_minutes or 0) / 60, 2),
        }
        for r in rows if (r.duration_minutes or 0) > 0
    ]


@router.post("/{job_id}/day-close", response_model=None)
def day_close_job(
    job_id: str,
    request: Request,
    body: Any = Body(default=None),
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """"No" on "Is this job finished?": close one shop day of the job.

    One transaction, one commit: the listed visits close (``visit_closed``),
    each listed person's day-D timers become one day row of their attested
    hours, each added helper gets a row, the job's dispatch status and
    schedule follow its remaining visits, and ``job_day_closed`` records the
    whole submission. Never touches the closeout, the invoice or the job's
    lifecycle stage. See plan §5.4a for the check order and the replay key.
    """
    from gdx_dispatch.core.audit import audit_or_rollback
    from gdx_dispatch.models.tenant_models import Appointment
    from gdx_dispatch.routers.appointments import close_visit
    from gdx_dispatch.services import day_close
    from gdx_dispatch.services.visit_sync import (
        ON_SITE,
        _audit,
        is_current,
        recompute_job_schedule,
        shop_day,
        shop_instant,
        visit_state,
    )

    try:
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # The same permission as closeout.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])

    # 1. Shape.
    parsed, error = _parse_day_close_body(body)
    if error:
        return jsonable_response({"detail": error}, 422)
    day = parsed["day"]
    closed_at: datetime = parsed["closed_at"]
    note = parsed["note"]
    actor = _user_id(current_user)
    now = datetime.now(UTC)
    # Before anything is staged: its first run per engine may commit.
    ensure_audit_table(db)

    try:
        # 2. The job-row lock, then the replay check, before any check that
        #    reads stored state (round 23).
        job = _locked_job(db, job_uuid)
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        tz = day_close.shop_tz(db)

        landed = _day_close_landed(db, job, closed_at)
        if landed is not None:
            owners = day_close.row_owners(db, job)
            names = day_close.user_names(db, {
                p for p, _c in owners.values() if p and p != "added"
            })
            row_names = {
                rid: ("Added helper" if p == "added" else names.get(p or ""))
                for rid, (p, _c) in owners.items()
            }
            return jsonable_response({
                "ok": True, "replay": True,
                "day_rows": _day_row_out(landed, row_names),
                "next_visit": _next_visit(db, job),
            })

        # 3. job_finished.
        if (job.lifecycle_stage or "").lower() in ("completed", "cancelled"):
            return _day_close_conflict(
                "job_finished", "Day not recorded: the job was already finished.",
            )

        # 4. visit_not_open, then day_moved, then person_not_open.
        visits: list[Appointment] = []
        for vid in parsed["visits"]:
            v = db.get(Appointment, vid)
            if v is None or v.job_id != job.id or v.deleted_at is not None or not is_current(v):
                extra: dict[str, Any] = {"visit_id": str(vid)}
                if v is not None and v.job_id == job.id and v.day_closed_at is not None:
                    extra["already_closed"] = _already_closed(db, job, shop_day(day_close.aware(v.start_at), tz), tz)
                return _day_close_conflict("visit_not_open", "That visit is no longer open.", **extra)
            visits.append(v)
        for v in visits:
            if shop_day(day_close.aware(v.start_at), tz) != day:
                return _day_close_conflict(
                    "day_moved", "The visit moved; reopen the sheet.", visit_id=str(v.id),
                )
        candidates = day_close.candidate_timers(db, job, day, tz)
        by_person: dict[str, list[TimeEntry]] = {}
        for t in candidates:
            if t.user_id:
                by_person.setdefault(str(t.user_id), []).append(t)
        for uid, _minutes in parsed["people"]:
            if uid in by_person:
                continue
            extra = {"user_id": uid}
            consumed = [
                t for t in db.execute(
                    select(TimeEntry).where(
                        TimeEntry.job_id == job.id,
                        TimeEntry.user_id == uid,
                        TimeEntry.entry_type == "job",
                        TimeEntry.deleted_at.is_(None),
                        TimeEntry.day_closed_at.is_not(None),
                    )
                ).scalars().all()
                if shop_day(day_close.aware(t.clock_in), tz) == day
            ]
            if consumed:
                extra["already_closed"] = _already_closed(db, job, day, tz)
            return _day_close_conflict(
                "person_not_open", "That person has no open time on this day.", **extra,
            )

        # --- the write: one transaction, no _record ---
        day_iso = day.isoformat()
        completed_at = min(closed_at, now)

        # Step 1: visits.
        for v in visits:
            close_visit(v, completed_at, closed_at)
            _audit(db, job, actor, "visit_closed", {
                "visit_id": str(v.id), "reason": "job_day_closed", "day": day_iso,
                "completed_at": completed_at.isoformat(),
            })

        # Step 2: people.
        written: list[TimeEntry] = []
        row_names: dict[str, str | None] = {}
        people_detail: list[dict] = []
        zeroed: list[str] = []
        names = day_close.user_names(db, {uid for uid, _m in parsed["people"]})
        for uid, minutes in parsed["people"]:
            timers = by_person[uid]  # oldest clock_in first
            rate = _labor_rate_for(db, _resolve_technician_id(db, uid))
            if uid == actor:
                open_ones = [t for t in timers if t.clock_out is None]
                row = (open_ones or timers)[-1]
                _close_labor_entry(row, now, minutes, rate)
                row.notes = note
                row.day_closed_at = closed_at
                for t in timers:
                    if t is row:
                        continue
                    if t.clock_out is None:
                        _close_labor_entry(t, now, 0, None)
                        zeroed.append(str(t.id))
                    t.day_closed_at = closed_at
            else:
                for t in timers:
                    if t.clock_out is None:
                        _close_labor_entry(t, now, 0, None)
                        zeroed.append(str(t.id))
                        log.warning(
                            "day_close_unattested_timer_closed",
                            extra={
                                "job_id": str(job.id), "entry_id": str(t.id),
                                "timer_user_id": t.user_id, "closed_by": actor,
                            },
                        )
                    t.day_closed_at = closed_at
                first = timers[0]
                row = TimeEntry(
                    id=uuid.uuid4(),
                    company_id=tenant_id,
                    job_id=job.id,
                    tech_id=first.tech_id,
                    user_id=None,
                    clock_in=day_close.aware(first.clock_in),
                    entry_type="work",
                    created_at=now,
                )
                db.add(row)
                _close_labor_entry(row, now, minutes, rate)
                row.notes = note
                row.day_closed_at = closed_at
                db.flush()
                _audit(db, job, actor, "day_row_created", {
                    "day_row_id": str(row.id), "user_id": uid,
                    "hours": round(minutes / 60, 2), "day": day_iso,
                })
            written.append(row)
            row_names[str(row.id)] = names.get(uid)
            people_detail.append({
                "user_id": uid, "hours": round(minutes / 60, 2), "day_row_id": str(row.id),
            })

        # Step 3: added helpers.
        if visits:
            anchor = min(day_close.aware(v.start_at) for v in visits)
        elif candidates:
            anchor = day_close.aware(candidates[0].clock_in)
        else:
            from datetime import time as _time

            anchor = shop_instant(day, _time(12, 0), tz)
        added_detail: list[dict] = []
        for minutes in parsed["added"]:
            row = TimeEntry(
                id=uuid.uuid4(),
                company_id=tenant_id,
                job_id=job.id,
                tech_id=actor,
                user_id=None,
                clock_in=anchor,
                entry_type="work",
                created_at=now,
            )
            db.add(row)
            _close_labor_entry(row, now, minutes, _labor_rate_for(db, _resolve_technician_id(db, actor)))
            row.notes = day_close.ADDED_HELPER_NOTE
            row.day_closed_at = closed_at
            db.flush()
            _audit(db, job, actor, "day_row_created", {
                "day_row_id": str(row.id), "user_id": None, "added": True,
                "hours": round(minutes / 60, 2), "day": day_iso,
            })
            written.append(row)
            row_names[str(row.id)] = "Added helper"
            added_detail.append({"hours": round(minutes / 60, 2), "day_row_id": str(row.id)})

        # Step 5: dispatch status, rolled up from the remaining Current
        # visits on that day.
        db.flush()
        remaining = [
            v for v in day_close.current_visits(db, job)
            if shop_day(day_close.aware(v.start_at), tz) == day
        ]
        if any(visit_state(v) == ON_SITE for v in remaining):
            rolled = "on_site"
        elif any(v.en_route_at is not None for v in remaining):
            rolled = "en_route"
        else:
            rolled = "assigned"
        prior_dispatch = job.dispatch_status
        job.dispatch_status = rolled
        job.updated_at = now

        # Step 6: the schedule follows the next Current visit.
        recompute_job_schedule(db, job, actor, "job_day_closed")

        # Step 7: the submission's own record, in the same transaction.
        next_visit = _next_visit(db, job)
        audit_or_rollback(
            db, tenant_id=tenant_id, actor={"user_id": actor},
            action=day_close.DAY_CLOSED_ACTION, entity_type="job", entity_id=str(job.id),
            details={
                "day": day_iso,
                "closed_at": closed_at.isoformat(),
                "visits": [str(v.id) for v in visits],
                "people": people_detail,
                "added": added_detail,
                "timers_closed_at_zero": zeroed,
                "actor": actor,
                "note": note,
                "next_visit": next_visit,
                "dispatch_status": {"from": prior_dispatch, "to": rolled},
            },
            request=request,
        )
        db.commit()
        return jsonable_response({
            "ok": True, "replay": False,
            "day_rows": _day_row_out(written, row_names),
            "next_visit": next_visit,
        })
    except SQLAlchemyError:
        db.rollback()
        log.exception("day_close_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


@router.get("/{job_id}/day-log", response_model=None)
def get_job_day_log(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The job's daily log and the "No" sheet's rows (plan §5.4a).

    ``rows`` are the day rows, newest first. ``open_day`` is what the sheet
    shows: the oldest past worked day, else today, with its Current visits
    and the people who have a candidate timer on it. ``earlier_day_open`` is
    the date that blocks "Yes", or null. Read permission is the job's own:
    an outsider gets the same 404 as a missing job.
    """
    from gdx_dispatch.services import day_close
    from gdx_dispatch.services.visit_sync import shop_day

    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    if not can_read_job(db, tenant_id, request, current_user, job_id):
        return jsonable_response({"detail": "job not found"}, 404)
    job = db.execute(
        select(Job).where(Job.id == jid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        return jsonable_response({"detail": "job not found"}, 404)
    tz = day_close.shop_tz(db)
    today = shop_day(datetime.now(UTC), tz)
    rows = day_close.day_log(db, job, tz)
    earlier = day_close.earlier_day_open(db, job, today, tz)
    return jsonable_response({
        "rows": rows,
        "logged_hours_total": round(sum(r["hours"] for r in rows), 2),
        "today_has_day_row": any(r["date"] == today.isoformat() for r in rows),
        "earlier_day_open": earlier.isoformat() if earlier else None,
        "open_day": day_close.open_day(db, job, _user_id(current_user), today, tz),
    })


class NotBillablePayload(BaseModel):
    # Mandatory, like every staff decline path (Doug 2026-07-30): a job
    # leaving the billing queue with no invoice needs a why on the record.
    reason: str = Field(min_length=1, max_length=300)


@router.post("/{job_id}/not-billable", response_model=None, dependencies=[Depends(require_permission("invoices.write"))])
def mark_job_not_billable(
    job_id: str,
    payload: NotBillablePayload,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The Ready-for-Billing dismiss verb: this job will never get an invoice.

    Warranty/goodwill/internal work lands in RFB with no exit other than
    Create Invoice — the queue floods and becomes wallpaper (same failure
    PR4 fixed for leaked parts with ``wont_bill``). The mark keeps the job
    and its audit trail but leaves every unbilled-nag surface via
    ``core/billing_predicates.job_billing_resolved()``. Reversible with the
    DELETE twin below.

    ``invoices.write`` gate (owner/admin/accounting): suppressing revenue is
    an invoicing decision — NOT invoices.read_all, which the read-only
    viewer role also holds.
    """
    # Bind the parsed UUID, not the raw string — Job.id is Uuid(as_uuid=True)
    # and the SQLite test path refuses str binds (same trap the closeout and
    # mobile paths already dodge).
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    reason = (payload.reason or "").strip()
    if not reason:
        return jsonable_response({"detail": "a reason is required"}, 422)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Before anything is staged (#696): the invoice_voided rows below are
        # staged into the commit that makes the void durable, and the first
        # audit write on an engine initializes the guard — committing, or on a
        # Postgres without CREATE rights rolling back, whatever is pending.
        ensure_audit_table(db)
        job = db.execute(
            select(Job).where(Job.id == jid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        # Completed jobs only (audit catch): the verb belongs to the RFB
        # queue, which contains nothing else. Marking a job mid-flight would
        # also hide the mobile Bill button while the work is still live —
        # with a require-invoice completion gate that's a deadlock.
        if job.lifecycle_stage != "completed":
            return jsonable_response(
                {"detail": "only completed jobs can be marked not billable"},
                409,
            )
        # A billed job isn't in the queue and "not billable" would be a lie
        # on the record — void the invoice first if that's really the intent.
        # EXCEPT the machine's own untouched autodraft (2026-08-07): the
        # closeout minted it without a human deciding anything, so "not
        # billable" voids it in the same stroke — otherwise every autodrafted
        # job would 409 here and the dismiss verb would be dead. A draft that
        # was verified/sent/locked/paid, or that a human created, still 409s.
        from gdx_dispatch.core.billing_predicates import invoice_bills_job
        from gdx_dispatch.core.closeout_billing import (
            is_untouched_autodraft,
            void_untouched_autodraft,
        )
        live_invoices = db.execute(
            select(Invoice).where(
                Invoice.job_id == job.id,
                Invoice.deleted_at.is_(None),
                Invoice.status != "void",
            )
        ).scalars().all()
        voided_autodrafts: list[str] = []
        for inv in live_invoices:
            if is_untouched_autodraft(inv, db):
                continue
            if invoice_bills_job(inv.status, float(inv.total or 0), inv.deleted_at, inv.billing_type):
                return jsonable_response(
                    {"detail": "job is already billed — void its invoice instead of marking it not billable"},
                    409,
                )
        for inv in live_invoices:
            if is_untouched_autodraft(inv, db):
                released_parts = void_untouched_autodraft(
                    db, inv, actor=_user_id(current_user)
                )
                voided_autodrafts.append(inv.invoice_number)
                # #696: the invoice gets its own row. The job's row below names
                # it by number, but an invoice's history is read by invoice id,
                # and without this the void had no who or when on the record it
                # changed. Same action as the /void route writes, and staged
                # into the same commit as the void.
                log_audit_event_sync(
                    db=db,
                    tenant_id=tenant_id,
                    user_id=_user_id(current_user),
                    action="invoice_voided",
                    entity_type="invoice",
                    entity_id=str(inv.id),
                    details={
                        "invoice_number": inv.invoice_number,
                        "total": float(inv.total or 0),
                        "released_parts": released_parts,
                        "via": "job_marked_not_billable",
                        "job_id": str(job.id),
                        "reason": reason,
                    },
                    ip_address=request.client.host if request.client else None,
                    request=request,
                )
        now = datetime.now(UTC)
        job.not_billable_at = now
        job.not_billable_reason = reason
        job.not_billable_by = _user_id(current_user)
        job.updated_at = now
        db.commit()
        log_audit_event_sync(
            db=db,
            tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_marked_not_billable",
            entity_type="job",
            entity_id=str(job.id),
            details={
                "title": job.title,
                "reason": reason,
                "voided_autodrafts": voided_autodrafts or None,
            },
            ip_address=request.client.host if request.client else None,
            request=request,
        )
        db.commit()
        log.info("job_marked_not_billable", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"ok": True, "id": str(job.id), "not_billable_at": now.isoformat()})
    except SQLAlchemyError:
        db.rollback()
        log.exception("mark_job_not_billable_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


@router.delete("/{job_id}/not-billable", response_model=None, dependencies=[Depends(require_permission("invoices.write"))])
def unmark_job_not_billable(
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo for the mark above — the job re-enters Ready-for-Billing.

    Idempotent: clearing an unmarked job is a 200 no-op (the state the
    caller asked for already holds; a retry must not 4xx).
    """
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        job = db.execute(
            select(Job).where(Job.id == jid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        was_marked = job.not_billable_at is not None
        prior_reason = job.not_billable_reason
        job.not_billable_at = None
        job.not_billable_reason = None
        job.not_billable_by = None
        if was_marked:
            job.updated_at = datetime.now(UTC)
        db.commit()
        if was_marked:
            log_audit_event_sync(
                db=db,
                tenant_id=tenant_id,
                user_id=_user_id(current_user),
                action="job_not_billable_cleared",
                entity_type="job",
                entity_id=str(job.id),
                details={"title": job.title, "prior_reason": prior_reason},
                ip_address=request.client.host if request.client else None,
                request=request,
            )
            db.commit()
            log.info("job_not_billable_cleared", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"ok": True, "id": str(job.id)})
    except SQLAlchemyError:
        db.rollback()
        log.exception("unmark_job_not_billable_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


@router.get("/return-visits-unscheduled", response_model=None, dependencies=[Depends(require_permission("jobs.read_all"))])
def return_visits_unscheduled(
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return-visit child jobs no one has dealt with in ANY way.

    The closeout's needs_return_visit path creates the child unscheduled,
    unassigned, and unparked — if nothing surfaces it, "I need to come back"
    is exactly the silent-disappearing-job leak the closeout sheet exists to
    close. Every clause below is an "already dealt with" exit, so the count
    can't nag about work that's on a board somewhere:

    * scheduled_at set → it's on the dispatch board for that day.
    * assigned_to set (spawn-return-visit can pre-assign without a date) →
      it's on that tech's column.
    * holding_area_id set → deliberately parked (e.g. Waiting on Parts —
      the DESIGNED state for a return visit whose part is on order, since
      parts-to-order is this feature's sibling); the holding-area board is
      its surface. Counting parked jobs here would nag forever and teach
      everyone to ignore the entry.
    * completed/cancelled excluded (stage predicate matching the closeout's
      open-child check; every writer of is_return_visit sets a non-NULL
      lifecycle_stage, so notin_'s NULL blindness can't bite).

    jobs.read_all gate: office tiers only, same silent-403 contract as
    ready-for-billing — the dashboard drops the entry for ungranted roles.
    """
    _ = current_user
    try:
        results = db.execute(
            select(Job, Customer)
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .where(
                Job.is_return_visit.is_(True),
                Job.deleted_at.is_(None),
                Job.scheduled_at.is_(None),
                Job.assigned_to.is_(None),
                Job.holding_area_id.is_(None),
                Job.lifecycle_stage.notin_(["completed", "cancelled"]),
            )
            # Oldest first — the trip that's been waiting longest is the one
            # the customer has been waiting on longest.
            .order_by(Job.created_at.asc())
            .limit(100)
        ).all()
        return [
            {
                "id": str(job.id),
                "title": job.title or "",
                # The WHY the tech attested at closeout — dispatch reads this
                # to slot the trip.
                "description": job.description or "",
                "customer_name": customer.name if customer else "",
                "customer_id": str(job.customer_id) if job.customer_id else None,
                "job_number": job.job_number,
                "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
                "created_at": str(job.created_at) if job.created_at else None,
            }
            for job, customer in results
        ]
    except Exception:
        log.exception("return_visits_unscheduled_failed")
        with contextlib.suppress(Exception):
            db.rollback()
        return []


# create_invoice_from_job (POST /{job_id}/create-invoice) was DELETED here
# (2026-08-08 audit): ~200 lines of one-click invoice creation with its own
# numbering scheme, its own tax resolution, and CO auto-pull — and ZERO
# frontend callers (every Create Invoice affordance routes to /billing/new,
# which POSTs /api/invoices). Its business rules live on the reachable
# paths: estimate-vs-closeout-parts exclusivity in the autodraft + mobile
# builders, CO claiming via from_change_order_ids on POST /api/invoices.

@router.get("/{job_id}/financials", response_model=None)
def get_job_financials(
    job_id: str,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Money summary for one job (2026-07-23, deposit-invoices Phase 3).

    Feeds the Financials card on the office job page and the mobile job
    view: what's been invoiced (deposit vs. final), what's been paid, and
    what's still owed. Derive-don't-cache: everything comes from live
    invoice/payment rows, same exclusions as the billed predicate."""
    try:
        _job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "Job not found"}, 404)
    job = db.execute(
        select(Job).where(Job.id == _job_uuid, Job.deleted_at.is_(None))
    ).scalar_one_or_none()
    if not job:
        return jsonable_response({"detail": "Job not found"}, 404)

    invoices = db.execute(
        select(Invoice).where(
            Invoice.job_id == _job_uuid,
            Invoice.deleted_at.is_(None),
        ).order_by(Invoice.created_at.asc())
    ).scalars().all()

    invoiced_total = 0.0     # non-deposit, non-void — what the work is billed at
    deposit_total = 0.0      # deposit invoices, non-void
    deposit_paid = 0.0
    paid_total = 0.0         # all non-void payments incl. deposit money
    balance_open = 0.0       # what the customer still owes (issued, unpaid)
    rows: list[dict[str, Any]] = []
    for inv in invoices:
        is_void = inv.status == "void"
        is_deposit = (inv.billing_type or "") == "deposit"
        paid = float(db.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.invoice_id == inv.id,
                Payment.voided_at.is_(None),
            )
        ).scalar_one() or 0)
        if not is_void:
            paid_total += paid
            if is_deposit:
                deposit_total += float(inv.total or 0)
                deposit_paid += paid
            else:
                invoiced_total += float(inv.total or 0)
            if inv.status in ("sent", "overdue"):
                balance_open += float(inv.balance_due or 0)
        rows.append({
            "id": str(inv.id),
            "invoice_number": inv.invoice_number,
            "billing_type": inv.billing_type,
            "status": inv.status,
            "total": float(inv.total or 0),
            "balance_due": float(inv.balance_due or 0),
            "paid": round(paid, 2),
        })

    return {
        "job_id": str(job.id),
        "invoiced_total": round(invoiced_total, 2),
        "deposit_total": round(deposit_total, 2),
        "deposit_paid": round(deposit_paid, 2),
        "paid_total": round(paid_total, 2),
        "balance_due": round(balance_open, 2),
        "invoices": rows,
    }


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
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        logging.getLogger(__name__).exception("get_job caught exception")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        result = db.execute(
            select(Job, Customer).outerjoin(Customer, Job.customer_id == Customer.id).where(
                Job.id == job_uuid,
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
        d["billing_status"] = _derived_billing_status(d["display_state"])
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
        # GDXA-227: a parent that is completed (lifecycle_stage, or a legacy
        # status spelling — prod has NULL / 'Complete' / 'Completed') but no
        # completed_at cannot be dated, so the 90-day test cannot run. Say so
        # rather than reporting "not a callback"; never invent the date.
        d["callback_undetermined"] = False
        if job.parent_job_id:
            try:
                parent_row = db.execute(
                    select(Job.completed_at, Job.lifecycle_stage, Job.status).where(
                        Job.id == job.parent_job_id,
                        Job.deleted_at.is_(None),
                    )
                ).first()
                parent_completed = parent_row[0] if parent_row else None
                if (
                    parent_row is not None
                    and parent_completed is None
                    and (
                        parent_row[1] == "completed"
                        or parent_row[2] in ("Complete", "Completed", "completed")
                    )
                ):
                    d["callback_undetermined"] = True
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
    except SQLAlchemyError:
        log.exception("get_job_failed", extra={"tenant_id": tenant_id, "job_id": job_id})
        return jsonable_response({"detail": "A database error occurred"}, 500)


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
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        log.exception("get_job_duration_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    _ = current_user
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        # Get time entries for this job — kept as raw SQL because TimeEntry
        # has no company_id column in the ORM model but the DB table might.
        binds = {"job_id": job_uuid, "tenant_id": tenant_id}
        entries = db.execute(
            _uuid_text(
                """
                SELECT COALESCE(SUM(duration_minutes), 0) AS actual_minutes,
                       COUNT(*) AS entry_count
                FROM time_entries
                WHERE job_id = :job_id AND company_id = :tenant_id
                  AND deleted_at IS NULL
                """,
                "job_id",
            ),
            binds,
        ).mappings().first()

        actual_min = int(entries["actual_minutes"]) if entries else 0
        actual_hours = round(actual_min / 60, 2)

        # Get estimated hours from job_type average — involves cross-table
        # aggregate with subquery; cleaner as raw SQL. "Completed" is
        # lifecycle_stage or any legacy spelling: the status string is NULL or
        # 'Complete' on most historical completed jobs (GDXA-227).
        avg = db.execute(
            _uuid_text(
                """
                SELECT j.job_type,
                       AVG(te.duration_minutes) AS avg_minutes,
                       COUNT(DISTINCT te.job_id) AS sample_size
                FROM jobs j
                JOIN time_entries te ON te.job_id = j.id
                    AND te.company_id = j.company_id
                    AND te.deleted_at IS NULL
                WHERE j.job_type = (SELECT job_type FROM jobs WHERE id = :job_id LIMIT 1)
                  AND j.company_id = :tenant_id
                  AND (j.lifecycle_stage = 'completed'
                       OR j.status IN ('Complete', 'Completed', 'completed'))
                  AND j.deleted_at IS NULL
                GROUP BY j.job_type
                """,
                "job_id",
            ),
            binds,
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
        job_uuid = uuid.UUID(job_id)
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
            _uuid_text(
                "SELECT user_id, duration_minutes, hourly_rate, clock_in "
                "FROM time_entries "
                "WHERE job_id = :job_id AND company_id = :tenant_id "
                "AND deleted_at IS NULL",
                "job_id",
            ),
            {"job_id": job_uuid, "tenant_id": tenant_id},
        ).mappings().all()
        true_total = 0.0
        est_total = 0.0
        labor_minutes = 0
        sources_seen: set[str] = set()
        for tr in time_rows:
            mins = int(tr.get("duration_minutes") or 0)
            labor_minutes += mins
            hours = mins / 60.0
            when_val = tr.get("clock_in")
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
                # M35: was SUM(amount_paid) — a column nothing writes, so job
                # profitability understated collections by the whole drift.
                func.coalesce(func.sum(paid_amount_sq()), 0).label("total_paid"),
            ).where(
                Invoice.job_id == job_uuid,
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
    # Object-level authz — see update_job. Gates the job the dependency is
    # written ONTO; `depends_on_job_id` is only existence-checked below, as
    # it was before.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    # Guardrails added with the first UI door (2026-07): a self-dependency
    # deadlocks can-start forever, and a typo'd target id would create a
    # blocker no list could ever resolve to a job.
    if str(payload.depends_on_job_id) == str(job_id):
        return jsonable_response({"detail": "a job cannot depend on itself"}, 422)
    try:
        target = db.get(Job, uuid.UUID(str(payload.depends_on_job_id)))
    except (ValueError, AttributeError):
        target = None
    if target is None or target.deleted_at is not None:
        return jsonable_response({"detail": "depends_on job not found"}, 422)
    # Direct-cycle guard: if the target already depends on THIS job, adding
    # the reverse edge deadlocks both forever (can-start never clears).
    # Deeper cycles are not walked — this catches the one-click mistake.
    reverse = db.execute(
        select(JobDependency).where(
            JobDependency.tenant_id == tenant_id,
            JobDependency.job_id == str(payload.depends_on_job_id),
            JobDependency.depends_on_job_id == job_id,
        )
    ).scalar_one_or_none()
    if reverse is not None:
        return jsonable_response(
            {"detail": "that job already depends on this one — adding both directions would deadlock them"}, 422
        )
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
        # Resolve the depended-on jobs so the UI can render names, not UUIDs.
        # JobDependency stores Text ids while jobs.id is UUID — coerce in
        # Python rather than CASTing per-dialect.
        dep_uuids = []
        for d in deps:
            with contextlib.suppress(ValueError, AttributeError, TypeError):
                dep_uuids.append(uuid.UUID(str(d.depends_on_job_id)))
        target_info: dict[str, dict[str, Any]] = {}
        if dep_uuids:
            rows = db.execute(
                select(Job.id, Job.title, Job.status, Job.lifecycle_stage).where(Job.id.in_(dep_uuids))
            ).all()
            target_info = {
                str(r[0]): {
                    "title": r[1],
                    "status": r[2],
                    "lifecycle_stage": str(r[3]) if r[3] else None,
                }
                for r in rows
            }
        return jsonable_response([
            {
                "id": d.id,
                "job_id": d.job_id,
                "depends_on_job_id": d.depends_on_job_id,
                "depends_on_title": target_info.get(str(d.depends_on_job_id), {}).get("title"),
                "depends_on_status": target_info.get(str(d.depends_on_job_id), {}).get("lifecycle_stage")
                or target_info.get(str(d.depends_on_job_id), {}).get("status"),
                "created_at": d.created_at,
            }
            for d in deps
        ])
    except SQLAlchemyError:
        log.exception("list_job_dependencies_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to list dependencies"}, 500)


@router.delete("/{job_id}/dependencies/{dependency_id}", response_model=None)
def remove_job_dependency(
    job_id: str,
    dependency_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a blocking relationship. Added with the first dependencies UI
    (2026-07) — an add-only contract would have made every mis-click a
    permanent blocker."""
    try:
        uuid.UUID(job_id)
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    try:
        dep = db.execute(
            select(JobDependency).where(
                JobDependency.tenant_id == tenant_id,
                JobDependency.job_id == job_id,
                JobDependency.id == dependency_id,
            )
        ).scalar_one_or_none()
        if dep is None:
            return jsonable_response({"detail": "dependency not found"}, 404)
        db.delete(dep)
        db.commit()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id,
            user_id=_user_id(current_user),
            action="job_dependency_removed", entity_type="job_dependency", entity_id=dependency_id,
            details={"job_id": job_id, "depends_on": dep.depends_on_job_id},
            request=request,
        )
        db.commit()
        return jsonable_response({"deleted": True, "id": dependency_id})
    except SQLAlchemyError:
        db.rollback()
        log.exception("remove_job_dependency_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to remove dependency"}, 500)


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
        # for the JOIN against jobs.id (UUID). A dependency is done when its
        # lifecycle_stage is completed or its status is a completed spelling;
        # status alone is NULL on imported completed jobs, which would block
        # forever (GDXA-227). A missing dependency row still blocks.
        row = db.execute(
            _text("""
                SELECT COUNT(*) AS blocking
                FROM job_dependencies d
                LEFT JOIN jobs j ON j.id = CAST(d.depends_on_job_id AS uuid) AND j.company_id = :tid
                WHERE d.tenant_id = :tid AND d.job_id = :jid
                  AND (j.id IS NULL OR NOT (j.lifecycle_stage = 'completed'
                       OR COALESCE(j.status, '') IN ('Complete', 'Completed', 'completed')))
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
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        log.exception("create_follow_up_job_failed")
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job. The new job is spawned FROM this
    # one and copies its customer, location and description, so a caller who
    # may not read the parent may not mint a child off it either.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    try:
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        original = db.execute(
            select(Job).where(
                Job.id == job_uuid,
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
            job_type=canonical_job_type(original.job_type) or SERVICE_CALL,
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
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job. Same reasoning as /follow-up: the
    # child is spawned from this job's customer and location.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    try:
        original = db.execute(
            select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not original:
            return jsonable_response({"detail": "Original job not found"}, 404)

        new_id = uuid.uuid4()
        title = (payload.title or f"Return visit: {original.title}")[:200]
        # Allocate a job_number for the child — same atomic counter as create_job.
        # GDXA-158: ``contained_read`` on the customer-name read for the same
        # reason as the closeout copy above — the counter is on ``cdb``, but this
        # read is on the caller's ``db``, so its failure aborted the request's
        # transaction and killed the ``db.add(child)`` that follows. Pure read,
        # inside the existing ``try`` (core.database rules 1/2).
        assigned_number: str | None = None
        try:
            with SessionLocal() as cdb:
                cust_name = None
                if original.customer_id:
                    with contained_read(db):
                        cust = db.execute(
                            select(Customer.name).where(Customer.id == original.customer_id)
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
            job_type=canonical_job_type(original.job_type) or SERVICE_CALL,
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
        # E4: a dated callback is booked like any new job, so it is on the board.
        from gdx_dispatch.services.visit_sync import book_new_job

        book_new_job(db, child, _user_id(current_user) or None)
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
    # The re-open's question (multi-day jobs plan §5.2a, R0): when a crew tech
    # already has a closed visit on the new day, book them there again?
    # Unanswered, such a re-open is 409 needs_answer.
    rebook_closed_day: bool | None = None


def _plan_reopen(db: Session, job: Job, payload: StateOverridePayload, now: datetime) -> Any:
    """R0, then the crew and the date, planned before anything is written."""
    from gdx_dispatch.services.visit_sync import UNSET, job_visit_fields, plan_for_job, shop_day, shop_tz

    finished_at = job.completed_at or now
    return plan_for_job(
        db, job,
        reopen=True,
        reopen_day=shop_day(finished_at, shop_tz(db)),
        scheduled_at=payload.scheduled_at if payload.scheduled_at else UNSET,
        crew_after=(payload.assigned_to,) if payload.assigned_to else None,
        rebook_closed_day=payload.rebook_closed_day,
        fields=job_visit_fields(db, job),
    )


def _apply_reopen(db: Session, job: Job, plan: Any, payload: StateOverridePayload, user: Any) -> Job:
    """The re-open's writes after its status flip: the job's date and crew,
    its visits (R0 and E4), and recompute."""
    from gdx_dispatch.services.visit_sync import UNSET

    if plan.scheduled_at is not UNSET:
        job.scheduled_at = plan.scheduled_at
    db.flush()
    if payload.assigned_to:
        _set_job_assignments(
            db, job_id=str(job.id), tech_ids=[payload.assigned_to],
            lead_tech_id=payload.assigned_to, user_id=_user_id(user),
        )
        job = db.get(Job, job.id)
    _apply_visits(db, job, plan, user, "job_reopened")
    return job


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
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job. Reversing a completion re-opens a
    # job that closeout/billing already treated as finished; it carried no
    # permission, access or role check of any kind before 2026-09-25.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        if job.lifecycle_stage != "completed":
            return jsonable_response({"detail": "only completed jobs can be un-completed"}, 409)

        plan = _plan_reopen(db, job, payload, now)
        if plan.refusal is not None:
            return _visit_refused(plan.refusal)
        prior_completed_at = job.completed_at
        job.lifecycle_stage = "in_progress"
        job.status = "In Progress"
        job.completed_at = None
        job.updated_at = now
        job = _apply_reopen(db, job, plan, payload, current_user)
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
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job. Same shape as /uncomplete: it
    # carried no check of any kind before 2026-09-25.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    try:
        job = db.execute(
            select(Job).where(Job.id == job_uuid, Job.deleted_at.is_(None))
        ).scalar_one_or_none()
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        if job.lifecycle_stage != "cancelled":
            return jsonable_response({"detail": "only cancelled jobs can be reactivated"}, 409)

        plan = _plan_reopen(db, job, payload, now)
        if plan.refusal is not None:
            return _visit_refused(plan.refusal)
        # Live first, so recompute sees a live job; the stage is picked below.
        job.lifecycle_stage = "service_call"
        if payload.assigned_to:
            job.dispatch_status = "assigned"
        job.updated_at = now
        prior_scheduled_at = job.scheduled_at
        job = _apply_reopen(db, job, plan, payload, current_user)
        # Reactivate to "scheduled" only if the job has a date — read AFTER
        # recompute. Cancelling retired its visits, so a re-activate sent with
        # no date and no Current visit left has only the stale date: clear it
        # and drop back to a service call, so the job shows up in "New Jobs to
        # Schedule" instead of appearing scheduled with nothing on the board.
        # The old date stays in this call's audit row. A date the office typed
        # is kept, even when the rebook question was answered No.
        from gdx_dispatch.services.visit_sync import is_current, visit_rows

        if payload.scheduled_at is None and not any(is_current(v) for v in visit_rows(db, job.id)):
            job.scheduled_at = None
        if job.scheduled_at:
            job.lifecycle_stage = "scheduled"
            job.status = "Scheduled"
        else:
            job.lifecycle_stage = "service_call"
            job.status = "Service Call"
        # GDXA-375: a live job carries no cancel stamp (the cancel's own audit
        # row keeps it), and the part requests the cancel released are owed
        # to the job again.
        prior_cancel = {
            "cancelled_at": job.cancelled_at.isoformat() if job.cancelled_at else None,
            "cancel_reason": job.cancel_reason,
        }
        job.cancelled_at = None
        job.cancel_reason = None
        restored = list(db.execute(
            select(JobPartNeeded.id).where(
                JobPartNeeded.job_id == str(job.id),
                JobPartNeeded.source == "request",
                JobPartNeeded.status == PART_REQUEST_CANCELLED,
            )
        ).scalars().all())
        if restored:
            db.execute(
                update(JobPartNeeded)
                .where(JobPartNeeded.id.in_(restored))
                .values(status="needed")
                .execution_options(synchronize_session=False)
            )
        db.flush()
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_reactivated", entity_type="job", entity_id=str(job.id),
            details={
                "reason": cleaned,
                "new_scheduled_at": payload.scheduled_at.isoformat() if payload.scheduled_at else None,
                "prior_scheduled_at": prior_scheduled_at.isoformat() if prior_scheduled_at else None,
                "prior_cancel": prior_cancel,
                "restored_part_request_ids": [str(i) for i in restored],
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


# ---------------------------------------------------------------------------
# GDXA-375 — the cancel lifecycle.
#
# Cancelling used to be a bare stage flip through the generic PATCH: no
# reason, no time, no webhook, and the job's part requests and running timers
# carried on as if it were live. /cancel is now the only way in (the PATCH
# answers 409 "use": "cancel"), and /reactivate the only way out. One
# transaction writes all of it:
#
#   * the stage, cancelled_at and cancel_reason (migration 111);
#   * the visits (X1: OPEN retired, ON SITE closed as cancelled, arrival kept);
#   * part requests the office has not ordered yet (source='request',
#     status='needed') move to 'cancelled', so they leave Parts to Order.
#     An ordered or received part is a real purchase in flight — it stays as
#     it is, for the office to return or shelve. /reactivate puts the
#     released requests back to 'needed';
#   * per-job timers still running are STOPPED, never deleted, the way the
#     phone's Stop and the shift-end sweep stop them: clock_out set, 0
#     minutes banked, the Stop marker leading the note. Elapsed clock time is
#     not evidence of work (#154); it is recorded in the audit row only, and
#     the office enters attested hours through labor if any were worked;
#   * one job_cancelled audit row naming all of the above, and job.cancelled.
# ---------------------------------------------------------------------------

#: Width of jobs.cancel_reason (migration 111). The audit row keeps the full
#: reason _validate_reason allows.
CANCEL_REASON_MAX = 300
#: CANCEL_TIMER_NOTE_SUFFIX (imported from day_close at the top) is what a
#: cancel stamps after the Stop marker on a timer it stops. One marker, read by
#: day_close.is_candidate and _stopped_job_timer_for.
#: The part-request status a cancel releases to, and /reactivate restores from.
#: MobileJobCloseoutDialog already reads it as "not a live request".
PART_REQUEST_CANCELLED = "cancelled"


class CancelJobPayload(BaseModel):
    reason: str  # mandatory, like /uncomplete and /reactivate


def _release_part_requests(db: Session, job: Job, now: datetime) -> list[str]:
    """Move the job's unordered part requests to 'cancelled'; return their ids."""
    ids = list(db.execute(
        select(JobPartNeeded.id).where(
            JobPartNeeded.job_id == str(job.id),
            JobPartNeeded.source == "request",
            JobPartNeeded.status == "needed",
        )
    ).scalars().all())
    if ids:
        db.execute(
            update(JobPartNeeded)
            .where(JobPartNeeded.id.in_(ids), JobPartNeeded.status == "needed")
            .values(status=PART_REQUEST_CANCELLED)
            .execution_options(synchronize_session=False)
        )
    return [str(i) for i in ids]


def _stop_running_timers(db: Session, job: Job, now: datetime) -> list[dict[str, Any]]:
    """Stop every per-job timer still running on the job, banking 0 minutes.

    Same write as tasks/job_timer_sweep.py: a user-less open row is an
    office-entered labor row (routers/labor.py), not a timer, and is left
    alone — setting its minutes to 0 would erase hours someone attested.
    """
    from gdx_dispatch.routers.mobile import MOBILE_STOP_LABOR_NOTE  # noqa: PLC0415 — mobile imports this router
    from gdx_dispatch.tasks.job_timer_sweep import (  # noqa: PLC0415 — celery_app imports the sweep
        running_job_timer_filters,
        stop_open_job_timer,
    )

    note = MOBILE_STOP_LABOR_NOTE + CANCEL_TIMER_NOTE_SUFFIX
    rows = db.execute(
        select(TimeEntry.id, TimeEntry.user_id, TimeEntry.clock_in).where(
            TimeEntry.job_id == job.id, *running_job_timer_filters(),
        )
    ).all()
    stopped: list[dict[str, Any]] = []
    for row in rows:
        if not stop_open_job_timer(db, row.id, clock_out=now, note=note, now=now):
            continue  # stopped by the tech (or the sweep) in the meantime
        started = row.clock_in if row.clock_in.tzinfo else row.clock_in.replace(tzinfo=UTC)
        stopped.append({
            "entry_id": str(row.id),
            "user_id": str(row.user_id),
            "clock_in": started.isoformat(),
            # What the clock read vs what was banked: evidence only.
            "elapsed_minutes": int(max((now - started).total_seconds(), 0) // 60),
            "recorded_minutes": 0,
        })
    return stopped


@router.post("/{job_id}/cancel", response_model=None)
def cancel_job(
    payload: CancelJobPayload,
    job_id: str,
    request: Request,
    current_user: Any = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a live job. Reason is mandatory. See the section comment above."""
    cleaned = _validate_reason(payload.reason)
    if not cleaned:
        return jsonable_response({"detail": "reason is required (≥4 characters)"}, 422)
    try:
        job_uuid = uuid.UUID(job_id)  # bind the parsed uuid — see update_job
    except (ValueError, AttributeError):
        return jsonable_response({"detail": "job not found"}, 404)
    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # Object-level authz — see update_job.
    denial = job_write_denial(db, tenant_id, request, current_user, job_id)
    if denial:
        return jsonable_response({"detail": denial[1]}, denial[0])
    now = datetime.now(UTC)
    try:
        # The finishing doors' lock, so a racing closeout or day-close commits
        # wholly before this reads the stage, or after the cancel commits.
        job = _locked_job(db, job_uuid)
        if not job:
            return jsonable_response({"detail": "job not found"}, 404)
        stage = (job.lifecycle_stage or "").lower()
        if stage == "cancelled":
            return jsonable_response({"detail": "this job is already cancelled"}, 409)
        if stage == "completed":
            # LIFECYCLE_TRANSITIONS: completed goes nowhere. Re-open it first,
            # which records why the finished job is being moved.
            return jsonable_response({
                "detail": "This job is completed. Use Re-open on the job page "
                          "first, so the reason is recorded.",
                "use": "reopen",
            }, 409)

        # X1, planned before anything is written: a refusal (an old
        # status-only arrival) leaves the job, its visits and the audit log
        # untouched.
        from gdx_dispatch.services.visit_sync import plan_for_job

        plan = plan_for_job(db, job, cancel=True)
        if plan.refusal is not None:
            return _visit_refused(plan.refusal)

        prior_stage = job.lifecycle_stage
        prior_scheduled_at = job.scheduled_at
        job.lifecycle_stage = "cancelled"
        job.status = "Cancelled"
        job.cancelled_at = now
        job.cancel_reason = cleaned[:CANCEL_REASON_MAX]
        job.updated_at = now
        db.flush()
        _apply_visits(db, job, plan, current_user, "job_cancelled")
        released = _release_part_requests(db, job, now)
        stopped = _stop_running_timers(db, job, now)
        log_audit_event_sync(
            db=db, tenant_id=tenant_id, user_id=_user_id(current_user),
            action="job_cancelled", entity_type="job", entity_id=str(job.id),
            details={
                "reason": cleaned,
                "prior_stage": prior_stage,
                "prior_scheduled_at": prior_scheduled_at.isoformat() if prior_scheduled_at else None,
                "released_part_request_ids": released,
                "stopped_timers": stopped,
            },
            request=request,
        )
        _emit_job_event(db, job, "job.cancelled", tenant_id)
        db.commit()
        return jsonable_response({
            "ok": True, "id": str(job.id),
            "lifecycle_stage": job.lifecycle_stage,
            "cancelled_at": job.cancelled_at,
            "cancel_reason": job.cancel_reason,
            "released_part_requests": len(released),
            "stopped_timers": len(stopped),
        })
    except SQLAlchemyError:
        db.rollback()
        log.exception("cancel_job_failed", extra={"job_id": job_id})
        return jsonable_response({"detail": "Failed to cancel job"}, 500)
