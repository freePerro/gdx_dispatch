from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.audit_labels import decorate_rows
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_role
from gdx_dispatch.routers.auth import get_current_user

router = APIRouter(
    prefix="/api/audit",
    tags=["audit"],
    dependencies=[Depends(require_role("admin", "owner"))],
)


def _require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    role = str(user.get("role") or "")
    if role not in {"admin", "owner", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _row_to_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "user_id": row.user_id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "details": row.details or {},
        "ip_address": row.ip_address,
        "request_id": row.request_id,
        "row_hash": row.row_hash,
        "prev_hash": row.prev_hash,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _list_rows(db: Session, *, page: int, page_size: int, where=None):
    q = select(AuditLog)
    if where is not None:
        q = q.where(where)
    # COUNT(*) — the previous implementation was len(...scalars().all()), which
    # materialized every audit row in the tenant on each dashboard load. On the
    # live tenant that is ~60k ORM objects to produce one integer.
    count_q = select(func.count()).select_from(AuditLog)
    if where is not None:
        count_q = count_q.where(where)
    total = int(db.execute(count_q).scalar() or 0)

    offset = (page - 1) * page_size
    rows = (
        db.execute(
            q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(page_size)
        )
        .scalars()
        .all()
    )
    # Actor + subject labels, batched, one query per distinct entity type.
    # Shared with routers/activity.py so both feeds agree on what a row says.
    items = decorate_rows(db, [_row_to_dict(r) for r in rows])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/logs")
def get_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_rows(db, page=page, page_size=page_size)


@router.get("/logs/export")
def export_audit_logs_csv(
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    rows = db.execute(select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())).scalars().all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "id",
            "tenant_id",
            "user_id",
            "action",
            "entity_type",
            "entity_id",
            "ip_address",
            "request_id",
            "row_hash",
            "prev_hash",
            "created_at",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                str(row.id),
                row.tenant_id or "",
                row.user_id or "",
                row.action,
                row.entity_type,
                row.entity_id or "",
                row.ip_address or "",
                row.request_id or "",
                row.row_hash,
                row.prev_hash,
                row.created_at.isoformat() if row.created_at else "",
            ]
        )
    out.seek(0)
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_logs.csv"},
    )


@router.get("/entity/{entity_type}/{entity_id}")
def get_entity_audit_trail(
    entity_type: str,
    entity_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    where = (AuditLog.entity_type == entity_type) & (AuditLog.entity_id == entity_id)
    return _list_rows(db, page=page, page_size=page_size, where=where)


@router.get("/user/{user_id}")
def get_user_audit_trail(
    user_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=200, ge=1, le=500),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    where = AuditLog.user_id == user_id
    return _list_rows(db, page=page, page_size=page_size, where=where)
