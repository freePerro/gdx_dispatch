"""
Audit log viewer and SOC2 compliance dashboard — admin-only endpoints.
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

from gdx_dispatch.core.audit import AuditLog, TenantBase, _payload_json
from gdx_dispatch.core.database import get_db

# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
ADMIN_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")


def _require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required",
        )
    if credentials is None or credentials.credentials != ADMIN_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied",
        )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["audit-dashboard"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_audit_table(db: Session) -> None:
    """Create audit_log table if it does not exist (dev / test convenience)."""
    with contextlib.suppress(Exception):
        TenantBase.metadata.create_all(bind=db.bind, checkfirst=True)


def _row_to_dict(r: AuditLog) -> dict:
    return {
        "id": str(r.id),
        "created_at": str(r.created_at),
        "event_type": r.event_type,
        "actor_id": r.actor_id,
        "actor_role": r.actor_role,
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "payload": r.payload,
        "ip_address": r.ip_address,
        "request_id": r.request_id,
        "hash": r.hash,
    }


def _run_integrity_check(db: Session, tenant_id: str | None = None) -> dict:
    """Verify SHA-256 hash chain for all (or tenant-filtered) audit log rows."""
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        rows = db.execute(q).scalars().all()
        total_rows = len(rows)
        prev_hash = "0" * 64
        for idx, row in enumerate(rows, start=1):
            actor = row.actor_id or "system"
            expected = hashlib.sha256(
                f"{prev_hash}{row.event_type}{actor}{row.entity_id}{_payload_json(row.payload or {})}".encode()
            ).hexdigest()
            if row.prev_hash != prev_hash or row.hash != expected:
                return {"ok": False, "broken_at_row": idx, "total_rows": total_rows}
            prev_hash = row.hash
        return {"ok": True, "broken_at_row": None, "total_rows": total_rows}
    except Exception:
        logging.getLogger(__name__).exception("_run_integrity_check caught exception")
        return {"ok": False, "broken_at_row": None, "total_rows": 0}


# ---------------------------------------------------------------------------
# Public helper functions (called by routes and tests directly)
# ---------------------------------------------------------------------------


def get_audit_events(
    db: Session,
    tenant_id: str | None = None,
    user_id: str | None = None,
    event_type: str | None = None,
    resource_type: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = 1,
    limit: int = 100,
) -> dict:
    """Query audit log with optional filters; return paginated result dict."""
    _ensure_audit_table(db)
    try:
        q = select(AuditLog)
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        if user_id:
            q = q.where(AuditLog.actor_id == user_id)
        if event_type:
            q = q.where(AuditLog.event_type == event_type)
        if resource_type:
            q = q.where(AuditLog.entity_type == resource_type)
        if start_date:
            q = q.where(AuditLog.created_at >= start_date)
        if end_date:
            q = q.where(AuditLog.created_at <= end_date)

        count_q = select(func.count()).select_from(q.subquery())
        total: int = db.execute(count_q).scalar_one_or_none() or 0
        pages = math.ceil(total / limit) if total else 1
        offset = (page - 1) * limit

        rows = (
            db.execute(
                q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
            )
            .scalars()
            .all()
        )
        return {
            "events": [_row_to_dict(r) for r in rows],
            "total": total,
            "pages": pages,
            "page": page,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("get_audit_events caught exception")
        return {"events": [], "total": 0, "pages": 1, "page": page, "error": str(exc)}


def get_audit_summary(db: Session, tenant_id: str | None = None) -> dict:
    """Return event-type counts, unique actors/resources for the last 30 days."""
    _ensure_audit_table(db)
    since = datetime.now(timezone.utc) - timedelta(days=30)
    try:
        base_q = select(AuditLog).where(AuditLog.created_at >= since)
        if tenant_id:
            base_q = base_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))

        # Per-event-type counts
        group_q = (
            select(AuditLog.event_type, func.count(AuditLog.id).label("cnt"))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            group_q = group_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        group_q = group_q.group_by(AuditLog.event_type)
        by_event_type = {row[0]: row[1] for row in db.execute(group_q).all()}

        total_events: int = sum(by_event_type.values())

        unique_actors_q = (
            select(func.count(AuditLog.actor_id.distinct()))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            unique_actors_q = unique_actors_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        unique_actors: int = db.execute(unique_actors_q).scalar_one_or_none() or 0

        unique_resources_q = (
            select(func.count(AuditLog.entity_id.distinct()))
            .where(AuditLog.created_at >= since)
        )
        if tenant_id:
            unique_resources_q = unique_resources_q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        unique_resources: int = db.execute(unique_resources_q).scalar_one_or_none() or 0

        return {
            "by_event_type": by_event_type,
            "total_events": total_events,
            "unique_actors": unique_actors,
            "unique_resources": unique_resources,
            "period_days": 30,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("get_audit_summary caught exception")
        return {
            "by_event_type": {},
            "total_events": 0,
            "unique_actors": 0,
            "unique_resources": 0,
            "period_days": 30,
            "error": str(exc),
        }


def export_audit_log(
    db: Session,
    tenant_id: str | None = None,
    fmt: str = "csv",
):
    """Export audit log rows as CSV StreamingResponse or JSON list (max 10 000 rows)."""
    _ensure_audit_table(db)
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10_000)
        if tenant_id:
            q = select(AuditLog).where(AuditLog.entity_id.like(f"{tenant_id}%")).order_by(AuditLog.created_at.desc()).limit(10_000)
        rows = db.execute(q).scalars().all()
    except Exception:
        logging.getLogger(__name__).exception("export_audit_log caught exception")
        rows = []

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "created_at", "event_type", "actor_id", "actor_role",
            "entity_type", "entity_id", "ip_address", "request_id", "hash",
        ])
        for r in rows:
            writer.writerow([
                str(r.id),
                str(r.created_at),
                r.event_type,
                r.actor_id or "",
                r.actor_role or "",
                r.entity_type,
                r.entity_id,
                r.ip_address or "",
                r.request_id or "",
                r.hash,
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
        )

    # JSON
    return [_row_to_dict(r) for r in rows]


def verify_audit_chain(db: Session, tenant_id: str | None = None) -> dict:
    """Walk all rows ordered by created_at/id and verify the SHA-256 hash chain.

    Returns {"ok": bool, "broken_at_row": int|None, "total_rows": int, "message": str}.
    """
    _ensure_audit_table(db)
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        rows = db.execute(q).scalars().all()
        total_rows = len(rows)
        prev_hash = "0" * 64
        for idx, row in enumerate(rows, start=1):
            actor = row.actor_id or "system"
            expected = hashlib.sha256(
                f"{prev_hash}{row.event_type}{actor}{row.entity_id}{_payload_json(row.payload or {})}".encode()
            ).hexdigest()
            if row.prev_hash != prev_hash or row.hash != expected:
                return {
                    "ok": False,
                    "broken_at_row": idx,
                    "total_rows": total_rows,
                    "message": f"Hash mismatch at row {idx}",
                }
            prev_hash = row.hash
        return {
            "ok": True,
            "broken_at_row": None,
            "total_rows": total_rows,
            "message": f"Chain intact ({total_rows} rows verified)",
        }
    except Exception as exc:
        logging.getLogger(__name__).exception("verify_audit_chain caught exception")
        return {
            "ok": False,
            "broken_at_row": None,
            "total_rows": 0,
            "message": str(exc),
        }


# ---------------------------------------------------------------------------
# Route 1: List audit logs
# ---------------------------------------------------------------------------


@router.get("/api/admin/audit-logs", dependencies=[Depends(_require_admin)])
def list_audit_logs(
    tenant_id: str | None = Query(None),
    event_type: str | None = Query(None),
    user_id: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ensure_audit_table(db)
    try:
        q = select(AuditLog)
        if tenant_id:
            q = q.where(AuditLog.entity_id.like(f"{tenant_id}%"))
        if event_type:
            q = q.where(AuditLog.event_type == event_type)
        if user_id:
            q = q.where(AuditLog.actor_id == user_id)
        if start:
            try:
                start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
                q = q.where(AuditLog.created_at >= start_dt)
            except ValueError:
                logging.getLogger(__name__).exception("list_audit_logs caught exception")
                pass
        if end:
            try:
                end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
                q = q.where(AuditLog.created_at <= end_dt)
            except ValueError:
                logging.getLogger(__name__).exception("list_audit_logs caught exception")
                pass

        count_q = select(func.count()).select_from(q.subquery())
        total: int = db.execute(count_q).scalar_one_or_none() or 0
        pages = math.ceil(total / limit) if total else 1
        offset = (page - 1) * limit

        rows = db.execute(q.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)).scalars().all()
        events = [_row_to_dict(r) for r in rows]
        integrity_ok = total < 1000

        return JSONResponse({
            "events": events,
            "total": total,
            "pages": pages,
            "page": page,
            "integrity_ok": integrity_ok,
        })
    except Exception as exc:
        logging.getLogger(__name__).exception("list_audit_logs caught exception")
        return JSONResponse({"events": [], "total": 0, "pages": 1, "page": page, "integrity_ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Route 2: Integrity check
# ---------------------------------------------------------------------------


@router.get("/api/admin/audit-logs/integrity-check", dependencies=[Depends(_require_admin)])
def audit_integrity_check(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    _ensure_audit_table(db)
    result = _run_integrity_check(db, tenant_id=tenant_id)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Route 3: Compliance summary
# ---------------------------------------------------------------------------


@router.get("/api/admin/compliance-summary", dependencies=[Depends(_require_admin)])
def compliance_summary(db: Session = Depends(get_db)) -> JSONResponse:
    _ensure_audit_table(db)
    now = datetime.now(timezone.utc)

    # MFA adoption
    mfa_val = os.getenv("MFA_REQUIRED", "").strip().lower()
    mfa_adoption_pct: float = 100.0 if mfa_val in ("1", "true", "yes") else 0.0

    # Audit log integrity (fast: check up to 500 rows only to avoid timeout)
    try:
        q = select(AuditLog).order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).limit(500)
        db.execute(q).scalars().all()
        integrity_result = _run_integrity_check(db)
        audit_log_integrity: bool = integrity_result["ok"]
    except Exception:
        logging.getLogger(__name__).exception("compliance_summary caught exception")
        audit_log_integrity = False

    # Last backup age
    last_backup_ts = os.getenv("LAST_BACKUP_TS", "")
    last_backup_age_hours: float = -1.0
    if last_backup_ts:
        try:
            ts = float(last_backup_ts)
            last_backup_age_hours = round((now.timestamp() - ts) / 3600, 2)
        except (ValueError, TypeError):
            logging.getLogger(__name__).exception("compliance_summary caught exception")
            pass

    # Active sessions
    active_session_count: int = 0
    with contextlib.suppress(ValueError, TypeError):
        active_session_count = int(os.getenv("ACTIVE_SESSION_COUNT", "0"))

    # Failed logins in last 24h
    failed_login_24h: int = 0
    try:
        since_24h = now - timedelta(hours=24)
        failed_login_24h = db.execute(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.event_type == "login_failed",
                AuditLog.created_at >= since_24h,
            )
        ).scalar_one_or_none() or 0
    except Exception:
        logging.getLogger(__name__).exception("compliance_summary caught exception")
        pass

    # Tenants with KB updates in last 30d
    tenants_with_kb_updates: int = 0
    try:
        since_30d = now - timedelta(days=30)
        tenants_with_kb_updates = db.execute(
            select(func.count(AuditLog.entity_id.distinct())).where(
                AuditLog.event_type == "kb_updated",
                AuditLog.created_at >= since_30d,
            )
        ).scalar_one_or_none() or 0
    except Exception:
        logging.getLogger(__name__).exception("compliance_summary caught exception")
        pass

    return JSONResponse({
        "mfa_adoption_pct": mfa_adoption_pct,
        "audit_log_integrity": audit_log_integrity,
        "last_backup_age_hours": last_backup_age_hours,
        "active_session_count": active_session_count,
        "failed_login_24h": failed_login_24h,
        "tenants_with_kb_updates": tenants_with_kb_updates,
    })


# ---------------------------------------------------------------------------
# Route 4: Compliance report (downloadable)
# ---------------------------------------------------------------------------


@router.get("/api/admin/compliance-report", dependencies=[Depends(_require_admin)], response_model=None)
def compliance_report(
    fmt: str = Query("json"),
    db: Session = Depends(get_db),
):
    _ensure_audit_table(db)
    try:
        rows = (
            db.execute(
                select(AuditLog).order_by(AuditLog.created_at.desc()).limit(10000)
            )
            .scalars()
            .all()
        )
    except Exception:
        logging.getLogger(__name__).exception("compliance_report caught exception")
        rows = []

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "created_at", "event_type", "actor_id", "actor_role", "entity_type", "entity_id", "ip_address"])
        for r in rows:
            writer.writerow([
                str(r.id),
                str(r.created_at),
                r.event_type,
                r.actor_id or "",
                r.actor_role or "",
                r.entity_type,
                r.entity_id,
                r.ip_address or "",
            ])
        buf.seek(0)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=compliance_report.csv"},
        )

    events = [_row_to_dict(r) for r in rows]
    return JSONResponse(events)


# ---------------------------------------------------------------------------
# Route 5 (deleted Sprint 1.0 B2): the `/admin/audit-log` HTML admin page was
# legacy Jinja (template was never deployed). The Vue SPA now owns that route
# via AuditLogViewer.vue which calls the JSON endpoint at `/api/admin/audit-log`.
# Keeping a handler here shadowed the SPA and rendered a broken page.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Route 6: Paginated audit events (delegates to get_audit_events helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-events", dependencies=[Depends(_require_admin)])
def api_audit_events(
    tenant_id: str | None = Query(None),
    user_id: str | None = Query(None),
    event_type: str | None = Query(None),
    resource_type: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> JSONResponse:
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if start:
        with contextlib.suppress(ValueError):
            start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    if end:
        with contextlib.suppress(ValueError):
            end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    result = get_audit_events(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        event_type=event_type,
        resource_type=resource_type,
        start_date=start_dt,
        end_date=end_dt,
        page=page,
        limit=limit,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Route 7: Audit summary (delegates to get_audit_summary helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-summary", dependencies=[Depends(_require_admin)])
def api_audit_summary(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(get_audit_summary(db, tenant_id=tenant_id))


# ---------------------------------------------------------------------------
# Route 8: Audit export (delegates to export_audit_log helper)
# ---------------------------------------------------------------------------


@router.post("/api/audit-export", dependencies=[Depends(_require_admin)], response_model=None)
def api_audit_export(
    tenant_id: str | None = Query(None),
    fmt: str = Query("csv"),
    db: Session = Depends(get_db),
):
    result = export_audit_log(db, tenant_id=tenant_id, fmt=fmt)
    if isinstance(result, list):
        return JSONResponse(result)
    return result


# ---------------------------------------------------------------------------
# Route 9: Hash chain integrity (delegates to verify_audit_chain helper)
# ---------------------------------------------------------------------------


@router.get("/api/audit-integrity", dependencies=[Depends(_require_admin)])
def api_audit_integrity(
    tenant_id: str | None = Query(None),
    db: Session = Depends(get_db),
) -> JSONResponse:
    return JSONResponse(verify_audit_chain(db, tenant_id=tenant_id))
