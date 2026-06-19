from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _iso_z(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_log_entry(
    *,
    level: str,
    logger: str,
    request_id: str,
    tenant_id: str,
    user_id: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    ts = timestamp or datetime.now(UTC)
    return {
        "timestamp": _iso_z(ts),
        "level": level.upper(),
        "logger": logger,
        "request_id": request_id or "-",
        "tenant_id": tenant_id or "-",
        "user_id": user_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "duration_ms": duration_ms,
        "details": details or {},
    }
