from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_format import build_log_entry

router = APIRouter(prefix="/api/admin/webhooks", tags=["webhooks-log"])


def _require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(user.get("role") or "") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def ensure_webhook_delivery_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS webhook_delivery_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                webhook_id TEXT NOT NULL,
                url TEXT NOT NULL,
                status_code INTEGER,
                response_time_ms INTEGER,
                attempt INTEGER NOT NULL,
                error TEXT,
                delivery_status TEXT NOT NULL,
                request_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.commit()


def _delivery_status(status_code: int | None, attempt: int, error: str | None) -> str:
    if status_code is not None and 200 <= int(status_code) < 300 and not error:
        return "sent"
    if attempt >= 3:
        return "dead_letter_queued"
    if attempt > 1:
        return "retried"
    return "failed"


def log_webhook_delivery(
    db: Session,
    tenant_id: str,
    webhook_id: str,
    url: str,
    status_code: int | None,
    response_time_ms: int,
    attempt: int,
    error: str | None,
    request_id: str | None = None,
) -> dict[str, Any]:
    ensure_webhook_delivery_table(db)
    status = _delivery_status(status_code, attempt, error)
    entry = build_log_entry(
        level="INFO" if status == "sent" else "WARNING",
        logger="gdx_dispatch.audit",
        request_id=request_id or "-",
        tenant_id=tenant_id,
        action="webhook_delivery",
        entity_type="webhook",
        entity_id=webhook_id,
        duration_ms=int(response_time_ms),
        details={"url": url, "status": status},
    )
    row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "webhook_id": webhook_id,
        "url": url,
        "status_code": status_code,
        "response_time_ms": int(response_time_ms),
        "attempt": int(attempt),
        "error": error,
        "delivery_status": status,
        "request_id": request_id or "-",
        "details": json.dumps(entry["details"], sort_keys=True),
    }
    db.execute(
        text(
            """
            INSERT INTO webhook_delivery_logs (
                id, tenant_id, webhook_id, url, status_code, response_time_ms,
                attempt, error, delivery_status, request_id, details
            ) VALUES (
                :id, :tenant_id, :webhook_id, :url, :status_code, :response_time_ms,
                :attempt, :error, :delivery_status, :request_id, :details
            )
            """
        ),
        row,
    )
    db.commit()
    return row


@router.get("/deliveries")
def get_webhook_deliveries(
    status: str | None = Query(default=None),
    endpoint: str | None = Query(default=None),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_webhook_delivery_table(db)
    where_parts = ["1=1"]
    params: dict[str, Any] = {}
    if status:
        where_parts.append("delivery_status = :status")
        params["status"] = status
    if endpoint:
        where_parts.append("url = :endpoint")
        params["endpoint"] = endpoint

    rows = db.execute(
        text(
            f"""
            SELECT id, tenant_id, webhook_id, url, status_code, response_time_ms,
                   attempt, error, delivery_status, request_id, created_at
            FROM webhook_delivery_logs
            WHERE {' AND '.join(where_parts)}
            ORDER BY created_at DESC
            LIMIT 500
            """
        ),
        params,
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "count": len(rows)}


@router.get("/health")
def get_webhook_health(
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_webhook_delivery_table(db)
    rows = db.execute(
        text(
            """
            SELECT url,
                   COUNT(*) AS total,
                   SUM(CASE WHEN delivery_status = 'sent' THEN 1 ELSE 0 END) AS success
            FROM webhook_delivery_logs
            GROUP BY url
            ORDER BY total DESC
            """
        )
    ).mappings().all()

    items = []
    for row in rows:
        total = int(row.get("total") or 0)
        success = int(row.get("success") or 0)
        rate = 0.0 if total == 0 else round((success / total) * 100.0, 2)
        items.append({"url": row.get("url"), "total": total, "success": success, "success_rate": rate})
    return {"items": items}
