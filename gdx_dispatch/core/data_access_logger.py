from __future__ import annotations

import contextlib
import csv
import inspect
import io
import json
import logging
from collections.abc import Generator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse

from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.core.database import SessionLocal, get_db
from gdx_dispatch.core.log_format import build_log_entry

router = APIRouter(prefix="/api/admin/gdpr", tags=["gdpr-access-log"])

SENSITIVE_ENTITIES = {"customers", "payments", "invoices"}


def _require_admin(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    if str(user.get("role") or "") not in {"admin", "owner", "superadmin"}:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def ensure_gdpr_access_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS gdpr_data_access_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                access_type TEXT NOT NULL,
                fields_accessed TEXT,
                request_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.commit()


def log_data_access(
    db: Session,
    tenant_id: str,
    user_id: str | None,
    entity_type: str,
    entity_id: str | None,
    access_type: str,
    fields_accessed: list[str],
    request_id: str | None = None,
) -> dict[str, Any]:
    ensure_gdpr_access_table(db)
    entry = build_log_entry(
        level="INFO",
        logger="gdx_dispatch.audit",
        request_id=request_id or "-",
        tenant_id=tenant_id,
        user_id=user_id,
        action=f"gdpr_{access_type}",
        entity_type=entity_type,
        entity_id=entity_id,
        details={"fields_accessed": fields_accessed},
    )
    row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "access_type": access_type,
        "fields_accessed": json.dumps(fields_accessed),
        "request_id": request_id or "-",
        "details": json.dumps(entry["details"], sort_keys=True),
    }
    db.execute(
        text(
            """
            INSERT INTO gdpr_data_access_logs (
                id, tenant_id, user_id, entity_type, entity_id, access_type,
                fields_accessed, request_id, details
            ) VALUES (
                :id, :tenant_id, :user_id, :entity_type, :entity_id, :access_type,
                :fields_accessed, :request_id, :details
            )
            """
        ),
        row,
    )
    db.commit()
    return row


def _path_to_entity(path: str) -> tuple[str | None, str | None, str]:
    if path.startswith("/api/customers"):
        prefix = "/api/customers"
        entity = "customers"
    elif path.startswith("/api/invoices"):
        prefix = "/api/invoices"
        entity = "invoices"
    elif path.startswith("/api/payments") or path.startswith("/payments"):
        prefix = "/api/payments" if path.startswith("/api/payments") else "/payments"
        entity = "payments"
    else:
        return None, None, "view"

    suffix = path[len(prefix):].strip("/")
    if not suffix or suffix == "search":
        return entity, None, "search"
    entity_id = suffix.split("/", 1)[0]
    return entity, entity_id, "view"


def _open_db_from_request(request: Request) -> tuple[Session | None, Generator | None]:
    override = request.app.dependency_overrides.get(get_db)
    if override is not None:
        candidate = override()
        if inspect.isgenerator(candidate):
            gen = candidate
            db = next(gen)
            return db, gen
        return candidate, None

    tenant = getattr(request.state, "tenant", None) or {}
    if not tenant.get("id"):
        return None, None
    return SessionLocal(), None


class GDPRDataAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.method.upper() != "GET" or response.status_code >= 400:
            return response

        entity_type, entity_id, access_type = _path_to_entity(request.url.path)
        if not entity_type or entity_type not in SENSITIVE_ENTITIES:
            return response

        db, gen = _open_db_from_request(request)
        if db is None:
            return response

        try:
            tenant = getattr(request.state, "tenant", None) or {}
            # Only use server-verified tenant state. Falling back to the
            # x-tenant-id header would let clients forge tenant entries in the
            # audit log, which breaks tenant-scoped compliance reports.
            tenant_id = str(tenant.get("id") or "-").strip() or "-"
            user_id = str(getattr(request.state, "current_user", {}).get("user_id") or "-")
            request_id = str(getattr(request.state, "request_id", request.headers.get("x-request-id", "-")))
            log_data_access(
                db,
                tenant_id=tenant_id,
                user_id=user_id,
                entity_type=entity_type,
                entity_id=entity_id,
                access_type=access_type,
                fields_accessed=[],
                request_id=request_id,
            )
        finally:
            if gen is not None:
                with contextlib.suppress(Exception):
                    gen.close()
            elif db is not None:
                db.close()
        return response


@router.get("/access-log")
def get_access_log(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    entity_type: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ensure_gdpr_access_table(db)
    where = ["1=1"]
    params: dict[str, Any] = {}
    if entity_type:
        where.append("entity_type = :entity_type")
        params["entity_type"] = entity_type
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id

    where_sql = " AND ".join(where)
    total = int(db.execute(text(f"SELECT COUNT(*) FROM gdpr_data_access_logs WHERE {where_sql}"), params).scalar() or 0)
    offset = (page - 1) * page_size
    params.update({"limit": page_size, "offset": offset})
    rows = db.execute(
        text(
            f"""
            SELECT id, tenant_id, user_id, entity_type, entity_id, access_type,
                   fields_accessed, request_id, created_at
            FROM gdpr_data_access_logs
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = []
    for row in rows:
        fields = []
        raw = row.get("fields_accessed")
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    fields = [str(x) for x in parsed]
            except Exception:
                logging.getLogger(__name__).exception("get_access_log caught exception")
                fields = []
        items.append(
            {
                "id": row.get("id"),
                "tenant_id": row.get("tenant_id"),
                "user_id": row.get("user_id"),
                "entity_type": row.get("entity_type"),
                "entity_id": row.get("entity_id"),
                "access_type": row.get("access_type"),
                "fields_accessed": fields,
                "request_id": row.get("request_id"),
                "created_at": row.get("created_at"),
            }
        )

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/access-log/export")
def export_access_log_csv(
    _: dict[str, Any] = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    ensure_gdpr_access_table(db)
    rows = db.execute(
        text(
            """
            SELECT tenant_id, user_id, entity_type, entity_id, access_type,
                   fields_accessed, request_id, created_at
            FROM gdpr_data_access_logs
            ORDER BY created_at ASC
            """
        )
    ).mappings().all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["tenant_id", "user_id", "entity_type", "entity_id", "access_type", "fields_accessed", "request_id", "created_at"])
    for row in rows:
        writer.writerow(
            [
                row.get("tenant_id") or "",
                row.get("user_id") or "",
                row.get("entity_type") or "",
                row.get("entity_id") or "",
                row.get("access_type") or "",
                row.get("fields_accessed") or "[]",
                row.get("request_id") or "",
                row.get("created_at") or "",
            ]
        )

    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=gdpr_access_log.csv"},
    )
