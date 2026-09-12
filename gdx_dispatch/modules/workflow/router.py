"""Workflow flags API — read/write tenant Job workflow toggles."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import audit_ready_db
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.settings_audit import audited_settings_upsert
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


_FLAG_COLUMNS = (
    "workflow_lock_schedule_on_start",
    "workflow_post_arrival_event",
    "workflow_sms_arrival_notify",
    "workflow_require_parts_on_complete",
    "workflow_require_hours_on_complete",
    "workflow_require_signature_on_complete",
    # PR5-billing-capture (Doug 2026-07-07): optional invoice-before-complete
    # hard gate. Default OFF — the daily billing follow-up loop chases
    # invoice-after-completion shops instead.
    "workflow_require_invoice_on_complete",
    # §12 (Doug 2026-07-30): surface jobs re-closed-out AFTER they were billed
    # so the office can reconcile. Company-wide on/off. Default OFF.
    "closeout_billing_reconciliation",
    # QB phase-out (Doug 2026-07-30, payment-date plan): pause the QB→GDX
    # invoice/payment pulls so GDX-entered payment corrections can't be
    # overwritten or duplicated by a webhook-triggered sync. Default OFF;
    # flip ON before starting the backfill.
    "qb_money_pull_paused",
)


class WorkflowFlags(BaseModel):
    lock_schedule_on_start: bool = False
    post_arrival_event: bool = False
    sms_arrival_notify: bool = False
    require_parts_on_complete: bool = False
    require_hours_on_complete: bool = False
    require_signature_on_complete: bool = False
    require_invoice_on_complete: bool = False
    closeout_billing_reconciliation: bool = False
    qb_money_pull_paused: bool = False


def _tenant_uuid(request: Request) -> UUID:
    tid = str(getattr(request.state, "tenant", {}).get("id", ""))
    try:
        return UUID(tid)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="invalid tenant context") from exc


def _read(db: Session, tid: UUID) -> dict[str, bool]:
    cols = ", ".join(_FLAG_COLUMNS)
    row = db.execute(
        text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
        {"tid": str(tid)},
    ).first()
    if row is None:
        db.execute(
            text("INSERT INTO tenant_settings (tenant_id) VALUES (:tid) ON CONFLICT (tenant_id) DO NOTHING"),
            {"tid": str(tid)},
        )
        db.commit()
        row = db.execute(
            text(f"SELECT {cols} FROM tenant_settings WHERE tenant_id = :tid"),
            {"tid": str(tid)},
        ).first()
    if row is None:
        # Unreachable — the upsert above guarantees the row — but the guard
        # narrows Row|None for every row[N] read below (9 mypy errors gone).
        raise HTTPException(status_code=500, detail="tenant_settings seed failed")
    return {
        "lock_schedule_on_start": bool(row[0]),
        "post_arrival_event": bool(row[1]),
        "sms_arrival_notify": bool(row[2]),
        "require_parts_on_complete": bool(row[3]),
        "require_hours_on_complete": bool(row[4]),
        "require_signature_on_complete": bool(row[5]),
        "require_invoice_on_complete": bool(row[6]),
        "closeout_billing_reconciliation": bool(row[7]),
        "qb_money_pull_paused": bool(row[8]),
    }


@router.get("/flags", response_model=WorkflowFlags)
def get_flags(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    _ = user
    return _read(db, _tenant_uuid(request))


@router.patch("/flags", response_model=WorkflowFlags)
def update_flags(
    payload: WorkflowFlags,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(audit_ready_db),
) -> dict[str, bool]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    # Invariant #1 — the highest-impact settings write in the module set.
    # These nine flags include `qb_money_pull_paused` (gates the QuickBooks
    # money pull) and `workflow_require_signature_on_complete`. Someone quietly
    # turning off signature-on-complete is exactly the "who changed this"
    # question that had no answer before #558.
    #
    # NOTE the payload/column naming split: the DB columns carry a `workflow_`
    # prefix that `_read` strips, so the audit diff is keyed the way a reader
    # sees the flags.
    return audited_settings_upsert(
        db,
        request,
        user,
        tenant_id=tid,
        values={
            "workflow_lock_schedule_on_start": payload.lock_schedule_on_start,
            "workflow_post_arrival_event": payload.post_arrival_event,
            "workflow_sms_arrival_notify": payload.sms_arrival_notify,
            "workflow_require_parts_on_complete": payload.require_parts_on_complete,
            "workflow_require_hours_on_complete": payload.require_hours_on_complete,
            "workflow_require_signature_on_complete": payload.require_signature_on_complete,
            "workflow_require_invoice_on_complete": payload.require_invoice_on_complete,
            "closeout_billing_reconciliation": payload.closeout_billing_reconciliation,
            "qb_money_pull_paused": payload.qb_money_pull_paused,
        },
        action="workflow_flags_updated",
        read=_read,
    )
