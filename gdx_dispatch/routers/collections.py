"""
Collections router — AR aging, dunning workflow, payment reminders.

Port of archive/dispatch_flask/blueprints/api_collections.py + api_invoice_reminders.py subset.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    tags=["collections"],
    dependencies=[Depends(require_module("invoices"))],
)


REMINDER_STAGES = ("friendly", "first_reminder", "second_reminder", "final_notice", "collections")


import contextlib
import logging

from gdx_dispatch.models.tenant_models import PaymentReminder  # noqa: E402

log = logging.getLogger(__name__)


class ReminderIn(BaseModel):
    invoice_id: str = Field(min_length=1, max_length=36)
    customer_id: str | None = Field(default=None, max_length=36)
    customer_name: str | None = Field(default=None, max_length=200)
    # stage/channel are enum-ish
    stage: str = Field(default="friendly", min_length=1, max_length=50)
    channel: str = Field(default="email", min_length=1, max_length=20)
    notes: str | None = Field(default=None, max_length=2000)
    # ISO date strings are at most 25 chars (with timezone); give some slack
    promised_payment_date: str | None = Field(default=None, max_length=32)


def _serialize(r: PaymentReminder) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "invoice_id": str(r.invoice_id),
        "customer_id": str(r.customer_id) if r.customer_id else None,
        "customer_name": r.customer_name,
        "stage": r.stage,
        "channel": r.channel,
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
        "sent_by": r.sent_by,
        "notes": r.notes,
        "promised_payment_date": r.promised_payment_date.isoformat() if r.promised_payment_date else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/api/collections/reminders", response_model=None)
def list_reminders(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    invoice_id: str | None = None,
    stage: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(PaymentReminder)
    if invoice_id:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(PaymentReminder.invoice_id == UUID(invoice_id))
    if stage:
        stmt = stmt.where(PaymentReminder.stage == stage)
    rows = db.execute(stmt.order_by(PaymentReminder.created_at.desc())).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/collections/reminders", response_model=None, status_code=201)
def create_reminder(
    payload: ReminderIn,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if payload.stage not in REMINDER_STAGES:
        raise HTTPException(status_code=422, detail=f"Invalid stage. Must be one of: {REMINDER_STAGES}")
    promised = None
    if payload.promised_payment_date:
        with contextlib.suppress(ValueError):
            promised = datetime.fromisoformat(payload.promised_payment_date)
    r = PaymentReminder(
        invoice_id=UUID(payload.invoice_id),
        customer_id=UUID(payload.customer_id) if payload.customer_id else None,
        customer_name=payload.customer_name,
        stage=payload.stage,
        channel=payload.channel,
        sent_at=utcnow(),
        sent_by=user.get("email") if isinstance(user, dict) else None,
        notes=payload.notes,
        promised_payment_date=promised,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="create_reminder",
                entity_type="reminder",
                entity_id="",
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('create_reminder_audit_failed')
    return _serialize(r)


@router.get("/api/collections/aging", response_model=None)
def aging_report(
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """AR aging buckets: current (0-30), 31-60, 61-90, 90+.

    Reads from invoices table. Returns totals per bucket + detail list.
    """
    from gdx_dispatch.models.tenant_models import Invoice  # lazy import to avoid circular

    today = date.today()
    buckets = {
        "current": {"label": "Current (0-30)", "min": 0, "max": 30, "count": 0, "total": 0.0, "invoices": []},
        "bucket_31_60": {"label": "31-60 Days", "min": 31, "max": 60, "count": 0, "total": 0.0, "invoices": []},
        "bucket_61_90": {"label": "61-90 Days", "min": 61, "max": 90, "count": 0, "total": 0.0, "invoices": []},
        "over_90": {"label": "Over 90 Days", "min": 91, "max": 99999, "count": 0, "total": 0.0, "invoices": []},
    }

    try:
        # PR1-billing-capture (2026-07-07): this filter shipped with
        # capitalized statuses ("Sent","Overdue","Partial") against the
        # lowercase enum ("sent","overdue",...) — "Partial" isn't a status at
        # all — so it matched ZERO rows and the aging report was always $0.
        # "overdue" is never persisted either (computed at read time), so the
        # honest receivable predicate is: not deleted, not draft (not yet a
        # receivable), not void, money still owed.
        # balance_due is NOT NULL by schema (tenant_models.py Invoice,
        # ORM-created table), so no NULL fallback is needed here.
        stmt = select(Invoice).where(
            Invoice.deleted_at.is_(None),
            Invoice.status.notin_(("draft", "void")),
            Invoice.balance_due > 0,
        )
        invoices = db.execute(stmt).scalars().all()
    except Exception:
        logging.getLogger(__name__).exception("aging_report caught exception")
        return {"buckets": list(buckets.values()), "total_outstanding": 0.0}

    total_outstanding = 0.0
    for inv in invoices:
        if not inv.due_date:
            continue
        days_overdue = (today - inv.due_date).days
        if days_overdue < 0:
            continue  # not yet due
        # balance_due is the canonical remainder (kept current by
        # _recalculate_invoice); the old total-minus-amount_paid math read
        # the deprecated amount_paid field that balance recomputation ignores.
        amount = float(inv.balance_due or 0)
        if amount <= 0:
            continue
        total_outstanding += amount
        for _key, bucket in buckets.items():
            if bucket["min"] <= days_overdue <= bucket["max"]:
                bucket["count"] += 1
                bucket["total"] += amount
                bucket["invoices"].append({
                    "id": str(inv.id),
                    "invoice_number": inv.invoice_number,
                    "customer_name": getattr(inv, "customer_name", None),
                    "due_date": inv.due_date.isoformat(),
                    "days_overdue": days_overdue,
                    "amount_due": amount,
                })
                break

    return {
        "buckets": list(buckets.values()),
        "total_outstanding": total_outstanding,
        "as_of": today.isoformat(),
    }


@router.delete("/api/collections/reminders/{reminder_id}", response_model=None, status_code=204)
def delete_reminder(reminder_id: UUID, _: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    r = db.get(PaymentReminder, reminder_id)
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    db.delete(r)
    db.commit()
    _audit_db = locals().get('db')
    if _audit_db is not None:
        try:
            _audit_user_obj = locals().get('user') or locals().get('current_user') or {}
            _audit_req = locals().get('request')
            _audit_tenant = ''
            if _audit_req is not None:
                _audit_tenant = str((getattr(getattr(_audit_req, 'state', None), 'tenant', {}) or {}).get('id') or '')
            _audit_user = str((_audit_user_obj or {}).get('sub') or (_audit_user_obj or {}).get('user_id') or 'system')
            log_audit_event_sync(
                _audit_db,
                tenant_id=_audit_tenant,
                user_id=_audit_user,
                action="delete_reminder",
                entity_type="reminder",
                entity_id=str(reminder_id),
                details={},
                request=_audit_req,
            )
            _audit_db.commit()
        except Exception:
            log.exception('delete_reminder_audit_failed')
    return None
