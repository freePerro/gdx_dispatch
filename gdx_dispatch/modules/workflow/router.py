"""Workflow flags API — read/write tenant Job workflow toggles."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
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
)


class WorkflowFlags(BaseModel):
    lock_schedule_on_start: bool = False
    post_arrival_event: bool = False
    sms_arrival_notify: bool = False
    require_parts_on_complete: bool = False
    require_hours_on_complete: bool = False
    require_signature_on_complete: bool = False
    require_invoice_on_complete: bool = False


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
    return {
        "lock_schedule_on_start": bool(row[0]),
        "post_arrival_event": bool(row[1]),
        "sms_arrival_notify": bool(row[2]),
        "require_parts_on_complete": bool(row[3]),
        "require_hours_on_complete": bool(row[4]),
        "require_signature_on_complete": bool(row[5]),
        "require_invoice_on_complete": bool(row[6]),
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
    db: Session = Depends(get_db),
) -> dict[str, bool]:
    if (user.get("role") or "").lower() not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="admin or owner required")
    tid = _tenant_uuid(request)
    db.execute(
        text(
            "INSERT INTO tenant_settings (tenant_id, workflow_lock_schedule_on_start, "
            "workflow_post_arrival_event, workflow_sms_arrival_notify, "
            "workflow_require_parts_on_complete, workflow_require_hours_on_complete, "
            "workflow_require_signature_on_complete, "
            "workflow_require_invoice_on_complete) "
            "VALUES (:tid, :a, :b, :c, :d, :e, :f, :g) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "  workflow_lock_schedule_on_start = EXCLUDED.workflow_lock_schedule_on_start, "
            "  workflow_post_arrival_event = EXCLUDED.workflow_post_arrival_event, "
            "  workflow_sms_arrival_notify = EXCLUDED.workflow_sms_arrival_notify, "
            "  workflow_require_parts_on_complete = EXCLUDED.workflow_require_parts_on_complete, "
            "  workflow_require_hours_on_complete = EXCLUDED.workflow_require_hours_on_complete, "
            "  workflow_require_signature_on_complete = EXCLUDED.workflow_require_signature_on_complete, "
            "  workflow_require_invoice_on_complete = EXCLUDED.workflow_require_invoice_on_complete"
        ),
        {
            "tid": str(tid),
            "a": payload.lock_schedule_on_start,
            "b": payload.post_arrival_event,
            "c": payload.sms_arrival_notify,
            "d": payload.require_parts_on_complete,
            "e": payload.require_hours_on_complete,
            "f": payload.require_signature_on_complete,
            "g": payload.require_invoice_on_complete,
        },
    )
    db.commit()
    return _read(db, tid)
