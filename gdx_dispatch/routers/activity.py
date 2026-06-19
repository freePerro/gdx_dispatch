"""
Activity feed router — read-only view of the audit_logs table.

Exposes recent company-wide activity plus per-entity feeds for jobs and
customers. All queries are tenant-scoped against ``request.state.tenant``.
No new tables, no migrations — backed entirely by the existing
``audit_logs`` table populated by ``gdx_dispatch.core.audit.log_audit_event``.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import AuditLog, ensure_audit_table
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(
    tags=["activity"],
    dependencies=[Depends(require_module("jobs"))],
)


_user_cache: dict[str, str] = {}


def _resolve_user(db: Session, user_id: str | None) -> str:
    """Resolve a user UUID to email/username. Returns the ID if not found."""
    if not user_id or user_id in ("system", "anonymous", ""):
        return user_id or ""
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        from sqlalchemy import select

        from gdx_dispatch.models.tenant_models import User
        u = db.execute(select(User).where(User.id == user_id)).scalars().first()
        name = (u.email or u.username or user_id) if u else user_id
    except Exception:
        logging.getLogger(__name__).exception("_resolve_user caught exception")
        name = user_id
    _user_cache[user_id] = name
    return name


def _serialize(row: AuditLog, db: Session | None = None) -> dict[str, Any]:
    user_display = _resolve_user(db, row.user_id) if db else row.user_id
    return {
        "id": str(row.id),
        "user_id": user_display,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    return str(tenant.get("id") or "")


def _base_stmt(tenant_id: str):
    return select(AuditLog).where(AuditLog.tenant_id == tenant_id)


def _count_stmt(tenant_id: str):
    return select(func.count()).select_from(AuditLog).where(AuditLog.tenant_id == tenant_id)


@router.get("/api/activity/recent", response_model=None)
def list_recent_activity(
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    entity_type: str | None = Query(default=None, max_length=80),
    user_id: str | None = Query(default=None, max_length=64),
) -> dict[str, Any]:
    """Company-wide recent activity, paginated and filterable."""
    ensure_audit_table(db)
    tenant_id = _tenant_id(request)

    stmt = _base_stmt(tenant_id)
    count_stmt = _count_stmt(tenant_id)

    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
        count_stmt = count_stmt.where(AuditLog.entity_type == entity_type)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
        count_stmt = count_stmt.where(AuditLog.user_id == user_id)

    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)

    rows = db.execute(stmt).scalars().all()
    total = int(db.execute(count_stmt).scalar() or 0)
    return {"items": [_serialize(r, db) for r in rows], "total": total}


@router.get("/api/jobs/{job_id}/activity", response_model=None)
def list_job_activity(
    job_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Audit events for a single job, tenant-scoped."""
    ensure_audit_table(db)
    tenant_id = _tenant_id(request)

    stmt = (
        _base_stmt(tenant_id)
        .where(AuditLog.entity_type == "job")
        .where(AuditLog.entity_id == str(job_id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = (
        _count_stmt(tenant_id)
        .where(AuditLog.entity_type == "job")
        .where(AuditLog.entity_id == str(job_id))
    )

    rows = db.execute(stmt).scalars().all()
    total = int(db.execute(count_stmt).scalar() or 0)
    return {"items": [_serialize(r, db) for r in rows], "total": total}


@router.get("/api/customers/{customer_id}/activity", response_model=None)
def list_customer_activity(
    customer_id: str,
    request: Request,
    _: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Audit events for a single customer, tenant-scoped."""
    ensure_audit_table(db)
    tenant_id = _tenant_id(request)

    stmt = (
        _base_stmt(tenant_id)
        .where(AuditLog.entity_type == "customer")
        .where(AuditLog.entity_id == str(customer_id))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    )
    count_stmt = (
        _count_stmt(tenant_id)
        .where(AuditLog.entity_type == "customer")
        .where(AuditLog.entity_id == str(customer_id))
    )

    rows = db.execute(stmt).scalars().all()
    total = int(db.execute(count_stmt).scalar() or 0)
    return {"items": [_serialize(r, db) for r in rows], "total": total}
