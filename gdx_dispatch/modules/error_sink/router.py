"""Server-side error sink — admin read + resolve API."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import (
    ensure_audit_table,
    log_audit_event_sync,
    resolve_audit_actor,
)
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models.tenant_models import User
from gdx_dispatch.routers.auth import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/errors", tags=["error-sink"])


def _require_admin(user: dict[str, Any]) -> None:
    role = (user.get("role") or "").lower()
    if role not in {"admin", "owner"}:
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


def _as_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def _fill_user_labels(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Who hit it and who resolved it, from the users row (#701).

    The sink stores ids only — it used to copy an ``email`` claim no login
    carries (the User column was blank for every error) and a resolver label
    built the same way ("system" for a person). The email comes from
    ``user_id``, which fills the rows written before the fix too; a
    ``resolved_by`` that is a user's id reads as their name. Labels that are
    not an id ("claude", "triage-2026-07-02") are left as they are.
    """
    wanted = {
        uid
        for item in items
        for uid in (_as_uuid(item.get("user_id")), _as_uuid(item.get("resolved_by")))
        if uid is not None
    }
    if not wanted:
        return items
    # User.id is a Uuid column (dashless on SQLite) — bind UUIDs, not text.
    users = {
        row.id: row
        for row in db.execute(select(User).where(User.id.in_(list(wanted)))).scalars()
    }
    for item in items:
        who = users.get(_as_uuid(item.get("user_id")))
        if who is not None and not item.get("user_email"):
            item["user_email"] = who.email
        resolver = users.get(_as_uuid(item.get("resolved_by")))
        if resolver is not None:
            # Same order as core.user_display.resolve_author_name, off the row
            # already loaded rather than one more query per item.
            names = (resolver.name, resolver.full_name, resolver.username, resolver.email)
            item["resolved_by"] = next((v.strip() for v in names if isinstance(v, str) and v.strip()), item["resolved_by"])
    return items


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
    # Scope: always the one tenant. A "platform_admin" role used to bypass
    # this scoping; no role by that name is defined anywhere (removed 2026-09-04).
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
            f"exception_message, user_id, user_email, git_sha, group_fingerprint, "
            f"occurred_at, resolved_at, resolved_by "
            f"FROM server_errors WHERE {where_sql} "
            f"ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    ).mappings().all()
    return {
        "items": _fill_user_labels(db, [dict(r) for r in rows]),
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
    where = "id = :id"
    params: dict[str, Any] = {"id": error_id}
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
    return _fill_user_labels(db, [dict(row)])[0]


@router.patch("/{error_id}/resolve", response_model=None)
def resolve_error(
    error_id: str,
    payload: ResolvePayload,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    # `ensure_audit_table` here, before anything is staged: its first call for
    # an engine COMMITS the guard DDL, and fired lazily from inside the audit
    # write below it would harden the staged UPDATE before its audit row
    # exists. NOT `Depends(audit_ready_db)` — that resolves its own session via
    # `_get_db_dep`, which calls `get_db()` imperatively instead of declaring
    # `Depends(get_db)`, so it bypasses every `dependency_overrides[get_db]` in
    # the suite (routers/customers.py records two tests that went 404 that way;
    # during #558 it silently pointed two more at the real database).
    ensure_audit_table(db)
    now = datetime.now(timezone.utc)
    # Who resolved it (#701): the id, which is unique and fits the column's 64
    # characters. It read an email/sub the login dict never carries, so it
    # recorded "system" for a person (1 row on prod). The list and detail
    # endpoints show the name — see _fill_user_labels.
    user_label = resolve_audit_actor(user)

    base_where = "id = :id"
    params: dict[str, Any] = {"id": error_id}
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

    grouped = bool(payload.resolve_group and row["group_fingerprint"])
    if grouped:
        result = db.execute(
            text(
                "UPDATE server_errors SET resolved_at = :ts, resolved_by = :who, "
                "resolution_note = :note WHERE group_fingerprint = :fp AND resolved_at IS NULL"
            ),
            {"ts": now, "who": user_label, "note": payload.note, "fp": row["group_fingerprint"]},
        )
    else:
        result = db.execute(
            text(
                "UPDATE server_errors SET resolved_at = :ts, resolved_by = :who, "
                "resolution_note = :note WHERE id = :id"
            ),
            {"ts": now, "who": user_label, "note": payload.note, "id": error_id},
        )
    # The ledger row (#558, invariant #1). The rows already carry resolved_by /
    # resolved_at, so this is not about attribution — it is about the BULK
    # case: `resolve_group=True` closes every open row sharing a fingerprint,
    # and the per-row columns cannot tell you that one request closed N of
    # them, nor which fingerprint was swept. `rowcount` is that number.
    # Staged before the commit, so the sweep and its record land together.
    log_audit_event_sync(
        db,
        user_id=resolve_audit_actor(user, request),
        action="server_error_resolved",
        entity_type="server_error",
        entity_id=error_id,
        details={
            "rows_affected": int(result.rowcount or 0),
            "resolve_group": bool(payload.resolve_group),
            "group_fingerprint": row["group_fingerprint"],
            "swept_group": grouped,
            "note": payload.note,
        },
        request=request,
    )
    db.commit()
    return {"ok": True, "resolved_at": now}
