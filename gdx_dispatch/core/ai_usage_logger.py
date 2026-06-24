from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.log_format import build_log_entry

router = APIRouter(prefix="/api/ai", tags=["ai-usage"])


def ensure_ai_usage_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ai_usage_logs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT,
                task TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                total_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                latency_ms INTEGER NOT NULL,
                request_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.commit()


def _tenant_id_from_request(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None) or {}
    tenant_id = str(tenant.get("id") or request.headers.get("x-tenant-id", "")).strip()
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Missing tenant context")
    return tenant_id


def _row_to_dict(row: Any) -> dict[str, Any]:
    details_raw = row.get("details")
    details = {}
    if isinstance(details_raw, str) and details_raw:
        try:
            details = json.loads(details_raw)
        except Exception:
            logging.getLogger(__name__).exception("_row_to_dict caught exception")
            details = {}
    return {
        "id": row.get("id"),
        "tenant_id": row.get("tenant_id"),
        "user_id": row.get("user_id"),
        "task": row.get("task"),
        "model": row.get("model"),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "cost_usd": float(row.get("cost_usd") or 0.0),
        "latency_ms": int(row.get("latency_ms") or 0),
        "request_id": row.get("request_id") or "-",
        "details": details,
        "created_at": str(row.get("created_at") or ""),
    }


def log_ai_usage(
    db: Session,
    tenant_id: str,
    user_id: str | None,
    task: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    latency_ms: int,
    request_id: str | None = None,
) -> dict[str, Any]:
    ensure_ai_usage_table(db)
    now = datetime.now(UTC)
    entry = build_log_entry(
        level="INFO",
        logger="gdx_dispatch.audit",
        request_id=request_id or "-",
        tenant_id=tenant_id,
        user_id=user_id,
        action="ai_usage_logged",
        entity_type="ai_usage",
        entity_id=None,
        duration_ms=int(latency_ms),
        details={"task": task, "model": model},
        timestamp=now,
    )
    row = {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "task": task,
        "model": model,
        "input_tokens": max(0, int(input_tokens)),
        "output_tokens": max(0, int(output_tokens)),
        "total_tokens": max(0, int(input_tokens)) + max(0, int(output_tokens)),
        "cost_usd": round(max(0.0, float(cost_usd)), 8),
        "latency_ms": max(0, int(latency_ms)),
        "request_id": request_id or "-",
        "details": json.dumps(entry["details"], sort_keys=True),
        "created_at": now.isoformat(),
    }
    db.execute(
        text(
            """
            INSERT INTO ai_usage_logs (
                id, tenant_id, user_id, task, model, input_tokens, output_tokens,
                total_tokens, cost_usd, latency_ms, request_id, details, created_at
            ) VALUES (
                :id, :tenant_id, :user_id, :task, :model, :input_tokens, :output_tokens,
                :total_tokens, :cost_usd, :latency_ms, :request_id, :details, :created_at
            )
            """
        ),
        row,
    )
    db.commit()
    return row


def _usage_stats(db: Session, tenant_id: str) -> dict[str, Any]:
    ensure_ai_usage_table(db)
    totals = db.execute(
        text(
            """
            SELECT
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0) AS total_cost
            FROM ai_usage_logs
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().first() or {}

    by_model_rows = db.execute(
        text(
            """
            SELECT model,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0) AS total_cost
            FROM ai_usage_logs
            WHERE tenant_id = :tenant_id
            GROUP BY model
            ORDER BY total_tokens DESC
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()

    # Day bucket: Postgres needs `created_at::date` (created_at is timestamptz —
    # SUBSTR errors on it); SQLite (tests) has no `::date` cast but SUBSTR works
    # on its text timestamps. Pick per dialect.
    is_sqlite = db.bind is not None and db.bind.dialect.name == "sqlite"
    day_expr = "SUBSTR(created_at, 1, 10)" if is_sqlite else "created_at::date"
    by_day_rows = db.execute(
        text(
            f"""
            SELECT {day_expr} AS day,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(cost_usd), 0) AS total_cost
            FROM ai_usage_logs
            WHERE tenant_id = :tenant_id
            GROUP BY {day_expr}
            ORDER BY day DESC
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()

    return {
        "tenant_id": tenant_id,
        "totals": {
            "input_tokens": int(totals.get("input_tokens") or 0),
            "output_tokens": int(totals.get("output_tokens") or 0),
            "tokens": int(totals.get("total_tokens") or 0),
            "cost_usd": round(float(totals.get("total_cost") or 0.0), 8),
        },
        "by_model": [
            {
                "model": r.get("model"),
                "tokens": int(r.get("total_tokens") or 0),
                "cost_usd": round(float(r.get("total_cost") or 0.0), 8),
            }
            for r in by_model_rows
        ],
        "by_day": [
            {
                "day": r.get("day"),
                "tokens": int(r.get("total_tokens") or 0),
                "cost_usd": round(float(r.get("total_cost") or 0.0), 8),
            }
            for r in by_day_rows
        ],
    }


@router.get("/usage")
def get_ai_usage(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    tenant_id = _tenant_id_from_request(request)
    return _usage_stats(db, tenant_id)


@router.get("/usage/export")
def export_ai_usage_csv(request: Request, db: Session = Depends(get_db)):
    tenant_id = _tenant_id_from_request(request)
    ensure_ai_usage_table(db)
    rows = db.execute(
        text(
            """
            SELECT tenant_id, user_id, task, model, input_tokens, output_tokens,
                   total_tokens, cost_usd, latency_ms, request_id, created_at
            FROM ai_usage_logs
            WHERE tenant_id = :tenant_id
            ORDER BY created_at ASC
            """
        ),
        {"tenant_id": tenant_id},
    ).mappings().all()

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "tenant_id",
            "user_id",
            "task",
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "latency_ms",
            "request_id",
            "created_at",
        ]
    )
    for row in rows:
        r = _row_to_dict(row)
        writer.writerow(
            [
                r["tenant_id"],
                r["user_id"] or "",
                r["task"],
                r["model"],
                r["input_tokens"],
                r["output_tokens"],
                r["total_tokens"],
                r["cost_usd"],
                r["latency_ms"],
                r["request_id"],
                r["created_at"],
            ]
        )

    out.seek(0)
    return StreamingResponse(
        iter([out.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=ai_usage.csv"},
    )


class AIUsageLogger:
    async def log(
        self,
        tenant_id: str,
        task: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost: float,
        latency_ms: int,
    ) -> None:
        # DB-backed logging requires request-scoped db session.
        # The AI router endpoints call log_ai_usage directly with tenant db.
        _ = (tenant_id, task, model, input_tokens, output_tokens, cost, latency_ms)
