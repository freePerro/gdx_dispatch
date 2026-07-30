from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.responses import Response

from gdx_dispatch.core.activity_feed import collapse_runs, feed_filter, wanted_fetch_size
from gdx_dispatch.core.audit import AuditLog, verify_audit_chain
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


def _list_rows(db: Session, *, page: int, page_size: int, where=None, feed: bool = False):
    # `feed is True`, not `if feed`: called outside FastAPI's dependency
    # resolution (as the in-process tests do) the parameter is still a
    # fastapi.params.Query instance, and FieldInfo has no __bool__, so it is
    # truthy — `if feed` would silently turn the feed on for every direct
    # caller.
    feed = feed is True
    q = select(AuditLog)
    if where is not None:
        q = q.where(where)
    if feed:
        # Filter in SQL, before the LIMIT. Filtering client-side cannot work:
        # 47 of the live tenant's most recent 50 rows are auth noise, so the
        # page budget is spent before the browser ever sees it.
        q = q.where(feed_filter())
    # COUNT(*) — the previous implementation was len(...scalars().all()), which
    # materialized every audit row in the tenant on each dashboard load. On the
    # live tenant that is ~60k ORM objects to produce one integer.
    count_q = select(func.count()).select_from(AuditLog)
    if where is not None:
        count_q = count_q.where(where)
    if feed:
        count_q = count_q.where(feed_filter())
    total = int(db.execute(count_q).scalar() or 0)

    offset = (page - 1) * page_size
    # Over-fetch when collapsing: a run of 20 edits to one record becomes a
    # single entry, so asking for exactly page_size would hand back a nearly
    # empty page — the starved-feed bug this phase exists to fix, reintroduced
    # by its own fix.
    fetch = wanted_fetch_size(page_size, feed=feed)
    rows = (
        db.execute(
            q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(fetch)
        )
        .scalars()
        .all()
    )
    # Actor + subject labels, batched, one query per distinct entity type.
    # Shared with routers/activity.py so both feeds agree on what a row says.
    items = decorate_rows(db, [_row_to_dict(r) for r in rows])
    if feed:
        items = collapse_runs(items)[:page_size]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/logs")
def get_audit_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    feed: bool = Query(
        default=False,
        description=(
            "Human-facing activity feed: drop session/token churn and collapse "
            "consecutive edits to the same record. Off by default so the "
            "compliance view still sees every row."
        ),
    ),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_rows(db, page=page, page_size=page_size, feed=feed)


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


@router.get("/verify-chain")
def verify_audit_chain_endpoint(
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Plan §13: exercise the tamper-evidence that was NEVER run outside tests.

    The audit log is hash-chained (prev_hash/row_hash), but verify_audit_chain
    was referenced only from the test suite — there was no endpoint and no
    scheduled check, so a broken chain would have gone unnoticed indefinitely.
    This is the admin-facing verifier; a periodic Celery beat calls it too
    (tasks/audit_chain_verify). Optional entity_type/entity_id scope the check
    to one record's trail. `ok: false` means the chain is broken — a tampered,
    reordered, or deleted row — and is a compliance incident, not a soft
    warning."""
    ok = verify_audit_chain(db, entity_type=entity_type, entity_id=entity_id)

    # Count rows written OUTSIDE the hash chain (empty row_hash) — the GL
    # ledger writers build AuditLog directly and legacy rows predate hashing.
    # A non-zero count is why an all-scope `ok:false` is a DATA-HYGIENE signal
    # (writers bypass the chain), NOT proof of tampering; a real tamper is
    # ok:false WITH unchained_rows == 0. Callers key their alerting on that.
    q = select(func.count()).select_from(AuditLog).where(
        or_(AuditLog.row_hash.is_(None), AuditLog.row_hash == "")
    )
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
        if entity_id:
            q = q.where(AuditLog.entity_id == entity_id)
    unchained = int(db.execute(q).scalar() or 0)

    cq = select(func.count()).select_from(AuditLog)
    if entity_type:
        cq = cq.where(AuditLog.entity_type == entity_type)
        if entity_id:
            cq = cq.where(AuditLog.entity_id == entity_id)
    count = int(db.execute(cq).scalar() or 0)

    scope: Any = "all"
    if entity_type:
        scope = {"entity_type": entity_type, "entity_id": entity_id}
    return {
        "ok": bool(ok),
        "scope": scope,
        "rows_checked": count,
        "unchained_rows": unchained,
        # A tamper is a break with NOTHING legitimately unchained.
        "tamper_suspected": bool(not ok and unchained == 0),
    }
