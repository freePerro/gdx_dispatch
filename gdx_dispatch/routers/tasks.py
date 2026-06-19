"""
Internal tasks router — user-facing todo/task management.

NOT an async job queue. This tracks user-assigned work items (follow-ups,
reminders, admin tasks) tied to jobs/customers/users. CRUD + complete/reopen.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync, utcnow
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["tasks"],
    dependencies=[Depends(require_module("jobs"))],
)


TASK_STATUSES = ("open", "in_progress", "completed", "cancelled")
TASK_PRIORITIES = ("low", "normal", "high", "urgent")


from gdx_dispatch.models.tenant_models import InternalTask as Task  # noqa: E402


class TaskIn(BaseModel):
    assigned_to: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    priority: str = Field(default="normal", pattern=r"^(low|normal|high|urgent)$")
    status: str = Field(default="open", pattern=r"^(open|in_progress|completed|cancelled)$")
    due_date: datetime | None = None
    related_job_id: str | None = Field(default=None, max_length=64)
    related_customer_id: str | None = Field(default=None, max_length=64)


class TaskPatchIn(BaseModel):
    assigned_to: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    priority: str | None = Field(default=None, pattern=r"^(low|normal|high|urgent)$")
    status: str | None = Field(default=None, pattern=r"^(open|in_progress|completed|cancelled)$")
    due_date: datetime | None = None


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", {}) or {}
    tid = str(tenant.get("id") or "").strip()
    if not tid:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tid


def _user_id(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("sub") or user.get("user_id") or user.get("email") or "system")
    return "system"


def _serialize(t: Task) -> dict[str, Any]:
    return {
        "id": str(t.id),
        "company_id": t.company_id,
        "assigned_to": t.assigned_to,
        "title": t.title,
        "description": t.description,
        "priority": t.priority,
        "status": t.status,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "related_job_id": str(t.related_job_id) if t.related_job_id else None,
        "related_customer_id": str(t.related_customer_id) if t.related_customer_id else None,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
    }


def _parse_uuid(value: str | None) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid UUID: {value}") from None


def _audit(
    db: Session,
    *,
    request: Request,
    user: Any,
    action: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
) -> None:
    try:
        log_audit_event_sync(
            db,
            tenant_id=_tenant_id(request),
            user_id=_user_id(user),
            action=action,
            entity_type="task",
            entity_id=entity_id,
            details=details or {},
            request=request,
        )
        db.commit()
    except Exception:
        log.exception("task_audit_failed action=%s entity_id=%s", action, entity_id)


@router.get("/api/tasks", response_model=None)
def list_tasks(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str | None = Query(default=None),
    assigned_to: str | None = Query(default=None),
    related_job_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    tenant_id = _tenant_id(request)
    stmt = select(Task).where(
        Task.company_id == tenant_id,
        Task.deleted_at.is_(None),
    )
    if status:
        if status not in TASK_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status filter")
        stmt = stmt.where(Task.status == status)
    if assigned_to:
        stmt = stmt.where(Task.assigned_to == assigned_to)
    if related_job_id:
        stmt = stmt.where(Task.related_job_id == _parse_uuid(related_job_id))
    stmt = stmt.order_by(Task.due_date.asc().nullslast(), Task.created_at.desc())
    stmt = stmt.limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/api/tasks", response_model=None, status_code=201)
def create_task(
    payload: TaskIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = Task(
        id=uuid4(),
        company_id=tenant_id,
        assigned_to=payload.assigned_to,
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        status=payload.status,
        due_date=payload.due_date,
        related_job_id=_parse_uuid(payload.related_job_id),
        related_customer_id=_parse_uuid(payload.related_customer_id),
        created_by=_user_id(user),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    _audit(
        db,
        request=request,
        user=user,
        action="task_created",
        entity_id=str(task.id),
        details={"title": task.title, "priority": task.priority, "status": task.status},
    )
    return _serialize(task)


def _get_task_or_404(db: Session, tenant_id: str, task_id: UUID) -> Task:
    task = db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.company_id == tenant_id,
            Task.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.get("/api/tasks/{task_id}", response_model=None)
def get_task(
    task_id: UUID,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = _get_task_or_404(db, tenant_id, task_id)
    return _serialize(task)


@router.patch("/api/tasks/{task_id}", response_model=None)
def update_task(
    task_id: UUID,
    payload: TaskPatchIn,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = _get_task_or_404(db, tenant_id, task_id)

    changed: dict[str, Any] = {}
    data = payload.model_dump(exclude_unset=True)
    for field in ("assigned_to", "title", "description", "priority", "status", "due_date"):
        if field in data:
            setattr(task, field, data[field])
            changed[field] = data[field] if not isinstance(data[field], datetime) else data[field].isoformat()

    task.updated_at = utcnow()
    db.commit()
    db.refresh(task)
    _audit(
        db,
        request=request,
        user=user,
        action="task_updated",
        entity_id=str(task.id),
        details={"changed": changed},
    )
    return _serialize(task)


@router.post("/api/tasks/{task_id}/complete", response_model=None)
def complete_task(
    task_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = _get_task_or_404(db, tenant_id, task_id)
    now = utcnow()
    task.status = "completed"
    task.completed_at = now
    task.updated_at = now
    db.commit()
    db.refresh(task)
    _audit(
        db,
        request=request,
        user=user,
        action="task_completed",
        entity_id=str(task.id),
        details={"completed_at": now.isoformat()},
    )
    return _serialize(task)


@router.post("/api/tasks/{task_id}/reopen", response_model=None)
def reopen_task(
    task_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = _get_task_or_404(db, tenant_id, task_id)
    task.status = "open"
    task.completed_at = None
    task.updated_at = utcnow()
    db.commit()
    db.refresh(task)
    _audit(
        db,
        request=request,
        user=user,
        action="task_reopened",
        entity_id=str(task.id),
        details={},
    )
    return _serialize(task)


@router.delete("/api/tasks/{task_id}", response_model=None, status_code=200)
def delete_task(
    task_id: UUID,
    request: Request,
    user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tenant_id = _tenant_id(request)
    task = _get_task_or_404(db, tenant_id, task_id)
    task.deleted_at = utcnow()
    task.updated_at = task.deleted_at
    db.commit()
    _audit(
        db,
        request=request,
        user=user,
        action="task_deleted",
        entity_id=str(task.id),
        details={},
    )
    return {"deleted": True, "id": str(task.id)}
