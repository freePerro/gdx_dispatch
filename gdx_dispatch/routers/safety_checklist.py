"""Safety Checklist — required safety checks before job close."""
from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Job, SafetyChecklist
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/safety",
    tags=["safety"],
    dependencies=[Depends(require_module("jobs"))],
)

DEFAULT_TEMPLATE_ITEMS = [
    "Tested auto-reverse sensor",
    "Verified spring tension",
    "Checked cable condition",
    "Tested manual release",
    "Inspected weatherstripping",
    "Verified track alignment",
    "Tested remote and keypad",
    "Checked door balance",
    "Inspected hardware and fasteners",
    "Photographed completed work",
]


def _tid(request: Request) -> str:
    return str((getattr(request.state, "tenant", {}) or {}).get("id", ""))


def _uid(user: dict) -> str:
    return str(user.get("sub") or user.get("user_id") or "system")


class ChecklistCompleteIn(BaseModel):
    job_id: str = Field(min_length=1, max_length=36)
    items: list[dict[str, Any]] = Field(
        ...,
        description="List of {item: str, checked: bool} entries",
        max_length=100,
    )
    photo_url: str | None = Field(default=None, max_length=2000)
    signed: bool = Field(default=False)


def _serialize(row: SafetyChecklist) -> dict[str, Any]:
    items_raw = row.items
    if isinstance(items_raw, str):
        try:
            items_parsed = json.loads(items_raw)
        except (json.JSONDecodeError, TypeError):
            logging.getLogger(__name__).exception("_serialize caught exception")
            items_parsed = []
    else:
        items_parsed = items_raw or []

    return {
        "id": str(row.id),
        "company_id": str(row.company_id),
        "job_id": str(row.job_id),
        "technician_id": str(row.technician_id),
        "items": items_parsed,
        "completed": bool(row.completed),
        "photo_url": row.photo_url,
        "signed_at": str(row.signed_at) if row.signed_at else None,
        "created_at": str(row.created_at) if row.created_at else None,
    }


@router.get("/template")
def get_template(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the default garage door safety checklist template."""
    return {
        "items": [{"item": item, "checked": False} for item in DEFAULT_TEMPLATE_ITEMS],
    }


@router.post("/complete", status_code=201)
def complete_checklist(
    request: Request,
    payload: ChecklistCompleteIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tid = _tid(request)
    uid = _uid(user)
    checklist_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()

    all_checked = all(
        item.get("checked", False) for item in payload.items
    )
    items_json = json.dumps(payload.items)
    signed_at = now if payload.signed else None

    try:
        checklist = SafetyChecklist(
            id=checklist_id,
            company_id=tid,
            job_id=payload.job_id,
            technician_id=uid,
            items=items_json,
            completed=all_checked,
            photo_url=payload.photo_url,
            signed_at=signed_at,
            created_at=now,
        )
        db.add(checklist)
        db.commit()
        db.refresh(checklist)
    except Exception:
        db.rollback()
        log.exception("safety_checklist_complete_failed")
        raise HTTPException(status_code=500, detail="Failed to save safety checklist") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="safety_checklist", entity_id=checklist_id,
        details={"job_id": payload.job_id, "completed": all_checked,
                 "items_count": len(payload.items),
                 "checked_count": sum(1 for i in payload.items if i.get("checked"))},
        request=request,
    )
    return _serialize(checklist)


@router.get("/job/{job_id}")
def get_job_checklist(
    job_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any] | None:
    # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
    checklist = db.execute(
        select(SafetyChecklist)
        .where(
            SafetyChecklist.job_id == job_id,
            SafetyChecklist.deleted_at.is_(None),
        )
        .order_by(SafetyChecklist.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not checklist:
        return {"checklist": None, "job_id": job_id, "status": "missing"}
    return _serialize(checklist)


@router.get("/incomplete")
def incomplete_checklists(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Jobs that are missing safety checklists or have incomplete ones."""
    tid = _tid(request)

    try:
        # Find completed jobs with no associated safety checklist (missing/incomplete).
        # Uses outerjoin so jobs without any checklist (sc.id IS NULL) are included.
        rows = db.execute(
            select(
                Job.id.label("job_id"),
                Job.title,
                Job.description,
                Job.status,
                Job.created_at.label("job_created"),
                SafetyChecklist.id.label("checklist_id"),
                SafetyChecklist.completed,
            )
            .outerjoin(
                SafetyChecklist,
                (SafetyChecklist.job_id == Job.id) & (SafetyChecklist.company_id == tid),
            )
            .where(
                Job.deleted_at.is_(None),
                Job.status.in_(["Complete", "Completed", "done"]),
                SafetyChecklist.id.is_(None),
            )
            .order_by(Job.created_at.desc())
            .limit(100)
        ).mappings().all()

        return [
            {
                "job_id": str(r["job_id"]),
                "description": r.get("description") or r.get("title") or "",
                "job_status": r["status"],
                "job_created": str(r["job_created"]) if r["job_created"] else None,
                "has_checklist": r["checklist_id"] is not None,
                "checklist_completed": bool(r["completed"]) if r["completed"] is not None else False,
            }
            for r in rows
        ]
    except Exception:
        log.exception("incomplete_checklists: query failed, table may not exist")
        with contextlib.suppress(Exception):
            db.rollback()
        raise RuntimeError("Failed to fetch incomplete checklists due to database error") from None
