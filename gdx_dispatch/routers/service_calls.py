"""Service Call intake — fast path to create jobs from phone calls."""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from gdx_dispatch.core.tenant import company_id
from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/service-calls",
    tags=["service-calls"],
    dependencies=[Depends(require_module("jobs"))],
)


class ServiceCallIn(BaseModel):
    customer_id: str | None = Field(default=None, max_length=36)
    customer_name: str = Field(min_length=1, max_length=200)
    customer_phone: str = Field(default="", max_length=20)
    problem_description: str = Field(min_length=3, max_length=2000)
    urgency: str = Field(default="normal", pattern="^(normal|urgent|emergency)$")
    preferred_window: str = Field(default="", max_length=200)


@router.post("", status_code=201)
def create_service_call(
    request: Request,
    payload: ServiceCallIn,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a service call — creates a job with Service Call type."""
    tid = str(company_id())
    uid = str(user.get("sub") or user.get("user_id") or "system")
    job_id = str(uuid4())
    datetime.now(timezone.utc).isoformat()

    # Map urgency to priority
    priority_map = {"emergency": "Urgent", "urgent": "High", "normal": "Normal"}
    priority = priority_map.get(payload.urgency, "Normal")

    try:
        from uuid import UUID as _UUID

        from gdx_dispatch.models.tenant_models import Job
        now_dt = datetime.now(timezone.utc)
        job = Job(
            id=_UUID(job_id),
            company_id=tid,
            customer_id=_UUID(payload.customer_id) if payload.customer_id else None,
            title=payload.customer_name,
            description=payload.problem_description,
            job_type="Service Call",
            status="Service Call",
            priority=priority,
            lifecycle_stage="service_call",
            created_at=now_dt,
            updated_at=now_dt,
        )
        db.add(job)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("service_call_create_failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="Failed to create service call") from None

    log_audit_event_sync(
        db, tenant_id=tid, user_id=uid, action="create",
        entity_type="service_call", entity_id=job_id,
        details={
            "customer_name": payload.customer_name,
            "urgency": payload.urgency,
            "phone": payload.customer_phone,
            "preferred_window": payload.preferred_window,
        },
        request=request,
    )

    return {
        "status": "created",
        "job_id": job_id,
        "customer_name": payload.customer_name,
        "urgency": payload.urgency,
        "priority": priority,
    }


@router.get("/active")
def active_service_calls(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """List active service calls (not complete/invoiced)."""
    try:
        from sqlalchemy import case, select

        from gdx_dispatch.models.tenant_models import Customer, Job
        # Three-plane (2026-04-24 B1): tenant isolation is the connection; company_id filter removed.
        stmt = (
            select(Job, Customer.name.label("customer_name"))
            .outerjoin(Customer, Job.customer_id == Customer.id)
            .where(
                Job.job_type == "Service Call",
                Job.status.notin_(["Complete", "Completed", "Invoiced", "done"]),
                Job.deleted_at.is_(None),
            )
            .order_by(
                case(
                    (Job.priority == "Urgent", 1),
                    (Job.priority == "High", 2),
                    else_=3,
                ),
                Job.created_at.desc(),
            )
            .limit(50)
        )
        rows = db.execute(stmt).all()
        return [
            {
                "id": str(j.id),
                "title": j.title or "",
                "description": j.description or "",
                "status": j.status,
                "priority": j.priority or "Normal",
                "customer_name": cname or "",
                "created_at": str(j.created_at) if j.created_at else None,
            }
            for j, cname in rows
            for r in rows
        ]
    except Exception:
        log.exception("active_service_calls_failed")
        with contextlib.suppress(Exception):
            db.rollback()
        raise RuntimeError("Failed to fetch active service calls") from None
