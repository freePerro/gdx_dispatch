from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_format import build_log_entry

router = APIRouter(prefix="/api/admin", tags=["security"])

ALLOWED_SECURITY_EVENTS = {
    "password_changed",
    "mfa_enabled",
    "mfa_disabled",
    "api_key_created",
    "api_key_revoked",
    "role_changed",
    "login_success",
    "login_failed",
    "account_locked",
    "session_expired",
    "permission_denied",
    "security_alert",
}


def _require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(user.get("role") or "") not in {"admin", "owner", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def ensure_security_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS security_events (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT,
                event_type TEXT NOT NULL,
                details TEXT,
                ip_address TEXT,
                request_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.commit()


def _insert_event(
    db: Session,
    *,
    tenant_id: str,
    user_id: str | None,
    event_type: str,
    details: dict[str, Any],
    ip_address: str | None,
    request_id: str,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "event_type": event_type,
        "details": json.dumps(details or {}, sort_keys=True),
        "ip_address": ip_address,
        "request_id": request_id or "-",
        "created_at": now.isoformat(),
    }
    db.execute(
        text(
            """
            INSERT INTO security_events (
                id, tenant_id, user_id, event_type, details, ip_address, request_id, created_at
            ) VALUES (
                :id, :tenant_id, :user_id, :event_type, :details, :ip_address, :request_id, :created_at
            )
            """
        ),
        row,
    )
    db.commit()
    return row


def _maybe_alert(
    db: Session,
    *,
    tenant_id: str,
    user_id: str | None,
    event_type: str,
    details: dict[str, Any],
    ip_address: str | None,
    request_id: str,
) -> None:
    if event_type == "login_failed":
        threshold_time = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
        count = db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM security_events
                WHERE tenant_id = :tenant_id
                  AND event_type = 'login_failed'
                  AND ip_address = :ip_address
                  AND created_at >= :threshold_time
                """
            ),
            {"tenant_id": tenant_id, "ip_address": ip_address, "threshold_time": threshold_time},
        ).scalar() or 0
        if int(count) >= 5:
            _insert_event(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                event_type="security_alert",
                details={"pattern": "5_failed_logins", "ip_address": ip_address},
                ip_address=ip_address,
                request_id=request_id,
            )
        return

    if event_type == "role_changed" and str(details.get("new_role", "")).lower() == "admin":
        _insert_event(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="security_alert",
            details={"pattern": "role_change_to_admin", "details": details},
            ip_address=ip_address,
            request_id=request_id,
        )
        return

    if event_type == "api_key_created":
        _insert_event(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="security_alert",
            details={"pattern": "api_key_created", "details": details},
            ip_address=ip_address,
            request_id=request_id,
        )


def log_security_event(
    db: Session,
    tenant_id: str,
    user_id: str | None,
    event_type: str,
    details: dict[str, Any] | None,
    ip_address: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    ensure_security_table(db)
    if event_type not in ALLOWED_SECURITY_EVENTS:
        raise ValueError(f"Unsupported event_type: {event_type}")

    entry = build_log_entry(
        level="INFO",
        logger="gdx_dispatch.audit",
        request_id=request_id or "-",
        tenant_id=tenant_id,
        user_id=user_id,
        action=event_type,
        entity_type="security",
        details=details or {},
    )
    row = _insert_event(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        event_type=event_type,
        details=entry["details"],
        ip_address=ip_address,
        request_id=request_id or "-",
    )
    _maybe_alert(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        event_type=event_type,
        details=details or {},
        ip_address=ip_address,
        request_id=request_id or "-",
    )
    return row


@router.get("/security-log")
def get_security_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_security_table(db)
    total = int(db.execute(text("SELECT COUNT(*) FROM security_events")).scalar() or 0)
    offset = (page - 1) * page_size
    rows = db.execute(
        text(
            """
            SELECT id, tenant_id, user_id, event_type, details, ip_address, request_id, created_at
            FROM security_events
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"limit": page_size, "offset": offset},
    ).mappings().all()

    items = []
    for row in rows:
        details = {}
        raw = row.get("details")
        if isinstance(raw, str) and raw:
            try:
                details = json.loads(raw)
            except Exception:
                logging.getLogger(__name__).exception("get_security_log caught exception")
                details = {}
        items.append(
            {
                "id": row.get("id"),
                "tenant_id": row.get("tenant_id"),
                "user_id": row.get("user_id"),
                "event_type": row.get("event_type"),
                "details": details,
                "ip_address": row.get("ip_address"),
                "request_id": row.get("request_id"),
                "created_at": row.get("created_at"),
            }
        )
    return {"items": items, "total": total, "page": page, "page_size": page_size}
