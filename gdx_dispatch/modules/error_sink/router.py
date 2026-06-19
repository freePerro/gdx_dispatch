"""Server-side error sink — admin read + resolve API."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/errors", tags=["error-sink"])


def _require_admin(user: dict[str, Any]) -> None:
    role = (user.get("role") or "").lower()
    if role not in {"admin", "owner", "platform_admin"}:
        raise HTTPException(status_code=403, detail="admin or owner required")


def _tenant_id(request: Request) -> str | None:
    t = getattr(request.state, "tenant", None)
    if isinstance(t, dict):
        return t.get("id")
    return None


class ErrorListItem(BaseModel):
    id: str
    tenant_id: str | None
    method: str | None
    path: str | None
    status_code: int | None
    exception_class: str | None
    exception_message: str | None
    user_email: str | None
    git_sha: str | None
    group_fingerprint: str | None
    occurred_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class ErrorDetail(ErrorListItem):
    request_id: str | None
    user_id: str | None
    query_string: str | None
    referer: str | None
    user_agent: str | None
    traceback: str | None
    resolution_note: str | None


class ResolvePayload(BaseModel):
    note: str | None = None
    # If true, mark every other row sharing the same group_fingerprint
    # as resolved too — typical pattern after one root-cause fix lands.
    resolve_group: bool = False


@router.get("", response_model=None)
def list_errors(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    path: str | None = None,
    exception_class: str | None = None,
    fingerprint: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    _require_admin(user)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    where = ["1=1"]
    params: dict[str, Any] = {}
    # Scope: platform_admin sees everything, others scoped to their tenant.
    role = (user.get("role") or "").lower()
    if role != "platform_admin":
        tid = _tenant_id(request)
        if tid:
            where.append("tenant_id = :tid")
            params["tid"] = tid
    if status == "open":
        where.append("resolved_at IS NULL")
    elif status == "resolved":
        where.append("resolved_at IS NOT NULL")
    if path:
        where.append("path ILIKE :path")
        params["path"] = f"%{path}%"
    if exception_class:
        where.append("exception_class = :ec")
        params["ec"] = exception_class
    if fingerprint:
        where.append("group_fingerprint = :fp")
        params["fp"] = fingerprint
    where_sql = " AND ".join(where)

    total = db.execute(
        text(f"SELECT COUNT(*) FROM server_errors WHERE {where_sql}"),
        params,
    ).scalar() or 0
    rows = db.execute(
        text(
            f"SELECT id, tenant_id, method, path, status_code, exception_class, "
            f"exception_message, user_email, git_sha, group_fingerprint, "
            f"occurred_at, resolved_at, resolved_by "
            f"FROM server_errors WHERE {where_sql} "
            f"ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    ).mappings().all()
    return {
        "items": [dict(r) for r in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


@router.get("/stats", response_model=None)
def stats(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    where = ["resolved_at IS NULL"]
    params: dict[str, Any] = {}
    role = (user.get("role") or "").lower()
    if role != "platform_admin":
        tid = _tenant_id(request)
        if tid:
            where.append("tenant_id = :tid")
            params["tid"] = tid
    where_sql = " AND ".join(where)

    by_class = db.execute(
        text(
            f"SELECT exception_class, COUNT(*) AS n FROM server_errors WHERE {where_sql} "
            f"GROUP BY exception_class ORDER BY n DESC LIMIT 20"
        ),
        params,
    ).mappings().all()
    by_path = db.execute(
        text(
            f"SELECT path, COUNT(*) AS n FROM server_errors WHERE {where_sql} "
            f"GROUP BY path ORDER BY n DESC LIMIT 20"
        ),
        params,
    ).mappings().all()
    by_group = db.execute(
        text(
            f"SELECT group_fingerprint, "
            f"  MIN(exception_class) AS exception_class, "
            f"  MIN(path) AS path, "
            f"  COUNT(*) AS n, "
            f"  MAX(occurred_at) AS last_seen "
            f"FROM server_errors WHERE {where_sql} "
            f"GROUP BY group_fingerprint ORDER BY n DESC LIMIT 20"
        ),
        params,
    ).mappings().all()
    open_total = db.execute(
        text(f"SELECT COUNT(*) FROM server_errors WHERE {where_sql}"),
        params,
    ).scalar() or 0
    return {
        "open_total": int(open_total),
        "by_class": [dict(r) for r in by_class],
        "by_path": [dict(r) for r in by_path],
        "by_group": [dict(r) for r in by_group],
    }


@router.get("/{error_id}", response_model=None)
def get_error(
    error_id: str,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    role = (user.get("role") or "").lower()
    where = "id = :id"
    params: dict[str, Any] = {"id": error_id}
    if role != "platform_admin":
        tid = _tenant_id(request)
        if tid:
            where += " AND tenant_id = :tid"
            params["tid"] = tid
    row = db.execute(
        text(f"SELECT * FROM server_errors WHERE {where}"),
        params,
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="error not found")
    return dict(row)


@router.patch("/{error_id}/resolve", response_model=None)
def resolve_error(
    error_id: str,
    payload: ResolvePayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    role = (user.get("role") or "").lower()
    now = datetime.now(timezone.utc)
    user_label = user.get("email") or user.get("sub") or "system"

    base_where = "id = :id"
    params: dict[str, Any] = {"id": error_id}
    if role != "platform_admin":
        tid = _tenant_id(request)
        if tid:
            base_where += " AND tenant_id = :tid"
            params["tid"] = tid
    row = db.execute(
        text(f"SELECT id, group_fingerprint FROM server_errors WHERE {base_where}"),
        params,
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="error not found")

    if payload.resolve_group and row["group_fingerprint"]:
        db.execute(
            text(
                "UPDATE server_errors SET resolved_at = :ts, resolved_by = :who, "
                "resolution_note = :note WHERE group_fingerprint = :fp AND resolved_at IS NULL"
            ),
            {"ts": now, "who": user_label, "note": payload.note, "fp": row["group_fingerprint"]},
        )
    else:
        db.execute(
            text(
                "UPDATE server_errors SET resolved_at = :ts, resolved_by = :who, "
                "resolution_note = :note WHERE id = :id"
            ),
            {"ts": now, "who": user_label, "note": payload.note, "id": error_id},
        )
    db.commit()
    return {"ok": True, "resolved_at": now}
