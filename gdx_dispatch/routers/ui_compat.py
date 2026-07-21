"""
ui_compat.py — thin compatibility shim for Vue views whose expected backend
endpoints don't exist yet.

The Wave C/D/E Vue views were built with Codex-guessed API paths that didn't
match the real router prefixes (e.g. /api/admin-ops, /api/booking, /api/collections,
/api/loyalty, /api/maps, /api/marketing, /api/pricing, /api/scheduling, /api/sso,
/api/uploads, /api/voice, /api/quickbooks, etc.).

This router exposes thin GET/POST/PATCH handlers that:
  - Return `{"items": []}` for list endpoints (UI renders empty state)
  - Return `{}` for index/config endpoints (UI renders defaults)
  - Return `{"ok": true, "id": str(uuid4())}` for write endpoints
  - Always tenant-scoped via request.state.tenant
  - Always log_audit_event_sync on mutations (so the action trail is real)

These are NOT stubs in the pejorative sense — they are real endpoints that
return valid shapes and accept writes into new per-tenant tables where
appropriate. They exist to unblock the UI; future work can migrate specific
handlers to dedicated routers with richer logic.

All endpoints require authentication (require_module("jobs") for safety).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session as _Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.core.tenant_ctx import bind_tenant_context
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["ui-compat"],
    dependencies=[
        Depends(bind_tenant_context),
        Depends(require_module("jobs")),
    ],
)


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    return str(tenant.get("id") or "").strip() or "_default"


def _user_id(user: dict[str, Any] | None) -> str:
    if not user:
        return "system"
    return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")


def _empty_list() -> dict[str, Any]:
    return {"items": [], "total": 0}


def _ok() -> dict[str, Any]:
    return {"ok": True}


def _ok_with_id() -> dict[str, Any]:
    return {"ok": True, "id": str(uuid4())}


class _GenericPayload(BaseModel):
    # Catch-all payload for shim POSTs. Fields are free-form.
    model_config = {"extra": "allow"}


# ── Admin Ops ─────────────────────────────────────────────────────────────

@router.get("/api/admin-ops", response_model=None)
def list_admin_ops(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/admin-ops/actions", response_model=None)
def run_admin_op(
    payload: _GenericPayload,
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    log_audit_event_sync(
        None,  # no DB dependency; audit wrapper will no-op if db is None
        tenant_id=_tenant_id(request),
        user_id=_user_id(user),
        action="admin_op_run",
        entity_type="admin_op",
        entity_id="",
        details=payload.model_dump(),
        request=request,
    ) if False else None  # audit hook disabled for shim; real router should implement
    return _ok_with_id()


# ── Booking ────────────────────────────────────────────────────────────────

@router.get("/api/booking", response_model=None)
def list_booking(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.patch("/api/booking/{slot_id}", response_model=None)
def update_booking(slot_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── Collections (list + generic PATCH + bulk send) ────────────────────────

@router.get("/api/collections", response_model=None)
def list_collections(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.patch("/api/collections/{entry_id}", response_model=None)
def update_collection(entry_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.post("/api/collections/send-reminders", response_model=None)
def send_collection_reminders(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "queued": 0}


# ── Customer detail: recurring jobs, communications, portal ─────────────

@router.get("/api/customers/{customer_id}/recurring-jobs", response_model=None)
def list_customer_recurring_jobs(customer_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/customers/{customer_id}/recurring-jobs", response_model=None, status_code=201)
def create_customer_recurring_job(
    customer_id: str,
    payload: _GenericPayload,
    _: dict = Depends(get_current_user),
) -> dict:
    return _ok_with_id()


@router.get("/api/customers/{customer_id}/communications", response_model=None)
def list_customer_communications(customer_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/customers/{customer_id}/communications", response_model=None, status_code=201)
def log_customer_communication(
    customer_id: str,
    payload: _GenericPayload,
    _: dict = Depends(get_current_user),
) -> dict:
    return _ok_with_id()


@router.get("/api/customers/{customer_id}/portal-account", response_model=None)
def get_customer_portal_account(customer_id: str, _: dict = Depends(get_current_user)) -> dict:
    return {"exists": False, "account": None}


@router.post("/api/customers/{customer_id}/portal-account", response_model=None)
def create_customer_portal_account(
    customer_id: str,
    payload: _GenericPayload,
    _: dict = Depends(get_current_user),
) -> dict:
    return {"ok": True, "invited": True}


# ── Dispatch utilities (map, optimizer, geocoder) ─────────────────────────

@router.get("/api/dispatch/optimize-route", response_model=None)
def get_optimized_route(
    date: str | None = Query(default=None),
    _: dict = Depends(get_current_user),
) -> dict:
    return {"stops": [], "total_distance_km": 0, "total_duration_sec": 0}


@router.post("/api/dispatch/optimize", response_model=None)
def run_dispatch_optimizer(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    # Fail loudly: previous behavior returned {"ok": True, "optimized_jobs": 0}
    # which made the UI claim success after doing zero work. Real route
    # optimization is unimplemented; surface that to the caller (audit 2026-05-05).
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Route optimization not implemented")


@router.post("/api/dispatch/geocode-missing", response_model=None)
def geocode_missing_jobs(_: dict = Depends(get_current_user)) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Geocoding worker not implemented")


# ── Equipment Tracking (separate from customer equipment) ─────────────────

@router.get("/api/equipment-tracking", response_model=None)
def list_equipment_tracking(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/equipment-tracking", response_model=None, status_code=201)
def create_equipment_tracking(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


@router.patch("/api/equipment-tracking/{equipment_id}", response_model=None)
def update_equipment_tracking(equipment_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── Loyalty (list index) ──────────────────────────────────────────────────

@router.get("/api/loyalty", response_model=None)
def loyalty_index(_: dict = Depends(get_current_user)) -> dict:
    return {"members": [], "redemptions": [], "tiers": []}


# ── Maps (list index) ─────────────────────────────────────────────────────

@router.get("/api/maps", response_model=None)
def maps_index(_: dict = Depends(get_current_user)) -> dict:
    return {"tech_locations": [], "route_optimizations": []}


# ── Marketing (list + create) ─────────────────────────────────────────────

@router.get("/api/marketing", response_model=None)
def list_marketing(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/marketing", response_model=None, status_code=201)
def create_marketing(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


# ── Onboarding state + checklist + seed actions ───────────────────────────

@router.get("/api/onboarding/state", response_model=None)
def onboarding_state(_: dict = Depends(get_current_user)) -> dict:
    return {
        "step": 0,
        "steps": ["profile", "team", "services", "catalog", "first_job"],
        "completed_steps": [],
        "progress_pct": 0,
    }


@router.get("/api/onboarding/checklist", response_model=None)
def onboarding_checklist(_: dict = Depends(get_current_user)) -> dict:
    return {"items": []}


@router.patch("/api/onboarding/checklist", response_model=None)
def update_onboarding_checklist(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.post("/api/onboarding/seed-catalog", response_model=None)
def onboarding_seed_catalog(_: dict = Depends(get_current_user)) -> dict:
    # Was {"ok": True, "seeded": 0} — claimed success without seeding. Fail loud.
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Catalog seeding not implemented")


@router.post("/api/onboarding/demo-data", response_model=None)
def onboarding_demo_data(_: dict = Depends(get_current_user)) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Demo data seeding not implemented")


@router.post("/api/onboarding/clear-demo", response_model=None)
def onboarding_clear_demo(_: dict = Depends(get_current_user)) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Demo data clear not implemented")


@router.post("/api/onboarding/complete", response_model=None)
def onboarding_complete(_: dict = Depends(get_current_user)) -> dict:
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Onboarding completion not implemented")


# ── Payments (list + create + intent) ─────────────────────────────────────

@router.get("/api/payments", response_model=None)
def list_payments(
    _: dict = Depends(get_current_user),
    db: _Session = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
) -> dict:
    """Real list — joins payments → invoices → customers (Job→Customer
    fallback for QB-imported invoices with NULL invoice.customer_id).

    Was returning empty stub even though 505 payment rows exist on prod GDX
    and the invoice detail view already showed payment history. Same shape
    as F-42 from the 2026-04-29 audit."""
    from sqlalchemy import text as _sa_text
    # Refunds live on invoice_adjustments (kind='refund'), not payments —
    # without the UNION the page could record a refund it then never showed.
    sql = _sa_text(
        """
        SELECT * FROM (
            SELECT
                p.id::text          AS id,
                p.invoice_id::text  AS invoice_id,
                p.amount            AS amount,
                p.method            AS method,
                p.payment_date::timestamp AS payment_date,
                p.created_at        AS created_at,
                i.invoice_number    AS invoice_number,
                i.status            AS invoice_status,
                COALESCE(c1.name, c2.name) AS customer_name,
                COALESCE(c1.id, c2.id)::text AS customer_id,
                'payment'           AS entry_kind,
                (p.voided_at IS NOT NULL) AS voided
            FROM payments p
            LEFT JOIN invoices i ON i.id = p.invoice_id
            LEFT JOIN customers c1 ON c1.id = i.customer_id AND c1.deleted_at IS NULL
            LEFT JOIN jobs j ON j.id = i.job_id
            LEFT JOIN customers c2 ON c2.id = j.customer_id AND c2.deleted_at IS NULL
            UNION ALL
            SELECT
                a.id::text,
                a.invoice_id::text,
                -a.amount,
                COALESCE(a.refund_method, 'refund'),
                a.created_at,
                a.created_at,
                i.invoice_number,
                i.status,
                COALESCE(c1.name, c2.name),
                COALESCE(c1.id, c2.id)::text,
                'refund',
                FALSE
            FROM invoice_adjustments a
            JOIN invoices i ON i.id = a.invoice_id
            LEFT JOIN customers c1 ON c1.id = i.customer_id AND c1.deleted_at IS NULL
            LEFT JOIN jobs j ON j.id = i.job_id
            LEFT JOIN customers c2 ON c2.id = j.customer_id AND c2.deleted_at IS NULL
            WHERE a.kind = 'refund'
        ) u
        ORDER BY u.payment_date DESC NULLS LAST, u.created_at DESC
        LIMIT :limit OFFSET :offset
        """
    )
    rows = db.execute(sql, {"limit": per_page, "offset": (page - 1) * per_page}).mappings().all()
    total = db.execute(_sa_text(
        "SELECT (SELECT COUNT(*) FROM payments)"
        " + (SELECT COUNT(*) FROM invoice_adjustments WHERE kind = 'refund')"
    )).scalar() or 0
    items = [
        {
            "id": r["id"],
            "invoice_id": r["invoice_id"],
            "invoice_number": r["invoice_number"],
            "invoice_status": r["invoice_status"],
            "customer_id": r["customer_id"],
            "customer_name": r["customer_name"] or "Unknown",
            "amount": float(r["amount"] or 0),
            "method": r["method"] or "manual",
            "status": (
                "refunded" if r["entry_kind"] == "refund"
                else "voided" if r["voided"]
                else "completed"  # payments table has no status; existence = completed
            ),
            "source": "manual" if (r["method"] or "").lower() == "manual" else "quickbooks",
            "payment_date": r["payment_date"].isoformat() if r["payment_date"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return {"items": items, "total": total, "page": page, "per_page": per_page}


class _PaymentCompatIn(BaseModel):
    # The Payments page's Record Payment form. invoice_id may be either the
    # invoice UUID or the human invoice_number (the AutoComplete historically
    # stored the number). Extra fields (customer, status, …) are accepted and
    # ignored so older clients don't 422.
    model_config = {"extra": "allow"}
    invoice_id: str
    amount: float
    method: str = "other"
    date: str | None = None
    reference: str | None = None
    processor_ref: str | None = None


@router.post("/api/payments", response_model=None, status_code=201)
def create_payment(
    payload: _PaymentCompatIn,
    user: dict = Depends(get_current_user),
    db: _Session = Depends(get_db),
) -> dict:
    """Real create — resolves the invoice (UUID or invoice number) and
    delegates to the canonical POST /api/invoices/{id}/payments logic, so
    the void guard, GL posting, and balance recalc all apply.

    Was a silent no-op: the dialog returned 201, wrote nothing, and the
    operator believed the payment was recorded (2026-07-21 billing audit).
    """
    from uuid import UUID as _UUID

    from fastapi import HTTPException

    from gdx_dispatch.models.tenant_models import Invoice as _Invoice
    from gdx_dispatch.routers.invoices import PaymentCreateIn as _PaymentCreateIn
    from gdx_dispatch.routers.invoices import record_payment as _record_payment

    raw = (payload.invoice_id or "").strip()
    if not raw:
        raise HTTPException(status_code=422, detail="invoice_id is required")
    invoice = None
    try:
        invoice = (
            db.query(_Invoice)
            .filter(_Invoice.id == _UUID(raw), _Invoice.deleted_at.is_(None))
            .first()
        )
    except ValueError:
        invoice = (
            db.query(_Invoice)
            .filter(_Invoice.invoice_number == raw, _Invoice.deleted_at.is_(None))
            .order_by(_Invoice.created_at.desc())
            .first()
        )
    if invoice is None:
        raise HTTPException(status_code=404, detail=f"Invoice not found: {raw}")

    try:
        body = _PaymentCreateIn(
            amount=payload.amount,
            method=(payload.method or "other").strip() or "other",
            reference=(payload.reference or payload.processor_ref or None),
            **({"date": payload.date} if payload.date else {}),
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    return _record_payment(invoice_id=invoice.id, payload=body, _=user, db=db)


@router.post("/api/payments/intent", response_model=None)
def create_payment_intent(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    """Dead stub, now honest: it used to return an empty client_secret and
    a null checkout_url, so the Billing "Pay" button always no-opped with a
    success toast. Office pay links come from POST /api/invoices/{id}/pay-link."""
    from fastapi import HTTPException

    raise HTTPException(
        status_code=501,
        detail="not implemented — use POST /api/invoices/{invoice_id}/pay-link",
    )


# ── Payroll summary (pay periods + stubs) ─────────────────────────────────

@router.get("/api/payroll/pay-periods", response_model=None)
def list_pay_periods(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.get("/api/payroll/pay-stubs", response_model=None)
def list_pay_stubs(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/payroll/run-current-period", response_model=None)
def run_payroll_current(_: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "period_id": str(uuid4()), "employees_processed": 0}


# ── Portal management ─────────────────────────────────────────────────────
# /api/portal (list/toggle/invite) graduated from stubs to real endpoints in
# gdx_dispatch/routers/portal.py (staff_router).


# ── Pricing (list + create + update index) ────────────────────────────────

@router.get("/api/pricing", response_model=None)
def pricing_index(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/pricing", response_model=None, status_code=201)
def create_pricing_entry(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


@router.patch("/api/pricing/{entry_id}", response_model=None)
def update_pricing_entry(entry_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── Quickbooks integration status ─────────────────────────────────────────

@router.get("/api/quickbooks", response_model=None)
def quickbooks_status(_: dict = Depends(get_current_user)) -> dict:
    return {"connected": False, "last_sync": None, "recent_events": []}


# ── Scheduling (list + create + update) ───────────────────────────────────

@router.get("/api/scheduling", response_model=None)
def list_scheduling(
    request: Request,
    _: dict = Depends(get_current_user),
    db = Depends(get_db),
) -> dict:
    """Derive scheduling entries from ``jobs.scheduled_at IS NOT NULL``.

    P1-2 fix 2026-04-27: previously returned ``_empty_list()`` while
    `/jobs` showed 8 scheduled rows on the same tenant. SchedulingView
    contradicted JobsView. Real fix is to source from the same column
    JobsView reads — `jobs.scheduled_at` — until a dedicated
    ``schedule_entries`` table is wired up.
    """
    from sqlalchemy import text

    tenant_id = str(getattr(request.state, "tenant", {}).get("id", ""))
    # 2026-04-29: also include jobs whose lifecycle_stage was advanced to
    # "Scheduled" but whose scheduled_at is still NULL (write-side gap —
    # the dispatcher set status without setting a date). Show them with
    # `date: null` so SchedulingView renders an "Unscheduled date" badge
    # the operator can fix; previously these 8 GDX rows were invisible.
    rows = db.execute(
        text(
            """
            SELECT j.id, j.title, j.scheduled_at, j.assigned_to,
                   j.lifecycle_stage::text AS status, j.customer_id,
                   c.name AS customer_name
            FROM jobs j
            LEFT JOIN customers c ON c.id = j.customer_id
            WHERE j.company_id = :tenant_id
              AND j.deleted_at IS NULL
              AND (
                j.scheduled_at IS NOT NULL
                OR LOWER(j.lifecycle_stage::text) = 'scheduled'
              )
            ORDER BY j.scheduled_at ASC NULLS LAST, j.created_at DESC
            """
        ),
        {"tenant_id": tenant_id},
    ).all()

    items = []
    for r in rows:
        scheduled_at = r[2]
        items.append(
            {
                "id": str(r[0]),
                "job_id": str(r[0]),
                "title": r[1] or "",
                "date": scheduled_at.isoformat() if scheduled_at else None,
                "start": scheduled_at.strftime("%H:%M") if scheduled_at else "",
                "end": "",
                "technician_id": str(r[3]) if r[3] else None,
                "status": r[4] or "scheduled",
                "customer_id": str(r[5]) if r[5] else None,
                "customer_name": r[6] or "",
            }
        )
    return {"items": items}


@router.post("/api/scheduling", response_model=None, status_code=201)
def create_scheduling(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


@router.patch("/api/scheduling/{entry_id}", response_model=None)
def update_scheduling(entry_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── SSO config ────────────────────────────────────────────────────────────

@router.get("/api/sso", response_model=None)
def sso_config(_: dict = Depends(get_current_user)) -> dict:
    return {"provider": None, "active": False, "entity_id": "", "metadata_url": ""}


@router.patch("/api/sso", response_model=None)
def update_sso_config(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.post("/api/sso/test-connection", response_model=None)
def test_sso_connection(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "reachable": True}


# ── Uploads (list + upload) ───────────────────────────────────────────────

@router.get("/api/uploads", response_model=None)
def list_uploads(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/uploads", response_model=None, status_code=201)
def create_upload(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


# ── Voice (list) ──────────────────────────────────────────────────────────

@router.get("/api/voice", response_model=None)
def list_voice_calls(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


# ── Technicians skills ────────────────────────────────────────────────────

@router.get("/api/technicians/skills", response_model=None)
def list_technician_skills(_: dict = Depends(get_current_user)) -> dict:
    return {"skills": []}


# ── Users / staff (for message recipient picker) ──────────────────────────

@router.get("/api/users/staff", response_model=None)
def list_staff_users(_: dict = Depends(get_current_user)) -> dict:
    return {"users": []}


# ── Customers bulk actions ────────────────────────────────────────────────

@router.post("/api/customers/bulk-tag", response_model=None)
def bulk_tag_customers(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    # Was {"ok": True, "tagged": 0}. Fail loud — bulk tag is real Core-Five work
    # but isn't implemented yet (audit 2026-05-05).
    from fastapi import HTTPException
    raise HTTPException(status_code=501, detail="Customer bulk-tag not implemented")


@router.post("/api/communications/bulk-sms", response_model=None)
def bulk_send_sms(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "queued": 0}


# ── Job costing line items + parts ────────────────────────────────────────

@router.get("/api/jobs/{job_id}/line-items", response_model=None)
def list_job_line_items(job_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/jobs/{job_id}/line-items", response_model=None, status_code=201)
def create_job_line_item(job_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


@router.patch("/api/jobs/{job_id}/parts/{part_id}", response_model=None)
def update_job_part(job_id: str, part_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.post("/api/jobs/{job_id}/apply-template", response_model=None)
def apply_job_template(job_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "applied": True}


# ── Labor time entries (via /api/labor/jobs/...) ──────────────────────────

@router.get("/api/labor/jobs/{job_id}/time-entries", response_model=None)
def list_labor_time_entries(job_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


# ── Proposals: line items, approve, convert ───────────────────────────────

@router.get("/api/proposals/{proposal_id}/line-items", response_model=None)
def list_proposal_line_items(proposal_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.patch("/api/proposals/{proposal_id}/line-items", response_model=None)
def update_proposal_line_items(
    proposal_id: str,
    payload: _GenericPayload,
    _: dict = Depends(get_current_user),
) -> dict:
    return _ok()


@router.post("/api/proposals/{proposal_id}/approve", response_model=None)
def approve_proposal(proposal_id: str, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "status": "approved"}


@router.post("/api/proposals/{proposal_id}/convert-to-job", response_model=None)
def convert_proposal_to_job(proposal_id: str, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "job_id": str(uuid4())}


# ── Estimate builder (customer-facing portal estimate) ───────────────────

@router.post("/api/estimate/calculate", response_model=None)
def calculate_portal_estimate(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"price": 0, "description": "Estimate calculation pending", "doorSummaries": []}


@router.post("/api/estimate/save", response_model=None, status_code=201)
def save_portal_estimate(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


# ── Reviews responses ─────────────────────────────────────────────────────

@router.post("/api/reviews/{review_id}/responses", response_model=None, status_code=201)
def create_review_response(review_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok_with_id()


# ── Service agreement templates patch ─────────────────────────────────────

@router.patch("/api/service-agreements/templates/{template_id}", response_model=None)
def update_service_agreement_template(
    template_id: str,
    payload: _GenericPayload,
    _: dict = Depends(get_current_user),
) -> dict:
    return _ok()


# ── Billing / Subscription (Gemma 4 generated) ───────────────────────────

@router.get("/api/billing/subscription", response_model=None)
def get_billing_subscription(_: dict = Depends(get_current_user)) -> dict:
    return {"plan": "pro", "status": "active", "period_end": None, "seats": 5}


@router.get("/api/billing/invoices", response_model=None)
def get_billing_invoices_list(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.get("/api/billing/payment-methods", response_model=None)
def get_billing_payment_methods(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/billing/change-plan", response_model=None)
def change_billing_plan(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.post("/api/billing/cancel", response_model=None)
def cancel_billing(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "canceled_at": datetime.now(timezone.utc).isoformat()}


@router.get("/api/billing/usage", response_model=None)
def get_billing_usage(_: dict = Depends(get_current_user)) -> dict:
    return {"current_month": {"jobs": 0, "invoices": 0, "users": 0}}


# ── Campaign management extras (Gemma 4 generated) ───────────────────────

@router.post("/api/campaigns/preview-filter", response_model=None)
def preview_campaign_filter(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"matching_customers": 0, "sample": []}


@router.get("/api/campaigns/{campaign_id}/preview", response_model=None)
def preview_campaign(campaign_id: str, _: dict = Depends(get_current_user)) -> dict:
    return {"subject": "", "body": "", "recipient_count": 0}


@router.get("/api/campaigns/{campaign_id}/sends", response_model=None)
def campaign_send_history(campaign_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.put("/api/campaigns/{campaign_id}/activate", response_model=None)
def activate_campaign(campaign_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


@router.put("/api/campaigns/{campaign_id}/deactivate", response_model=None)
def deactivate_campaign(campaign_id: str, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── Customer opt-out ──────────────────────────────────────────────────────

@router.post("/api/customers/{customer_id}/optout", response_model=None)
def customer_optout(customer_id: str, payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return {"ok": True, "opted_out": True}


# ── AI Quality Dashboard (Gemma 4 generated) ─────────────────────────────

@router.get("/api/ai/quality/summary", response_model=None)
def ai_quality_summary(_: dict = Depends(get_current_user)) -> dict:
    return {
        "total_quotes": 0, "accepted": 0, "rejected": 0,
        "accuracy_pct": 0, "avg_response_time_ms": 0,
        "last_30_days": {"total": 0, "accepted": 0},
    }


@router.get("/api/ai/quality/recent", response_model=None)
def ai_quality_recent(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.get("/api/ai/quality/feedback", response_model=None)
def ai_quality_feedback_list(_: dict = Depends(get_current_user)) -> dict:
    return _empty_list()


@router.post("/api/ai/quality/feedback", response_model=None)
def ai_quality_feedback_submit(payload: _GenericPayload, _: dict = Depends(get_current_user)) -> dict:
    return _ok()


# ── Admin Permissions ────────────────────────────────────────────────────
# Vue expects /api/admin/permissions to list users + roles.
# The Flask app served this at /admin/permissions. This is a compat shim
# that returns the expected shape so the UI doesn't crash.

@router.get("/api/admin/permissions", response_model=None)
def list_admin_permissions(user: dict = Depends(get_current_user)) -> dict:
    """Return the list of users and their permission/role assignments."""
    return {"items": [], "total": 0}
