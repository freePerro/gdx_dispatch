from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

from redis import from_url as redis_from_url
from sqlalchemy import text

log = logging.getLogger(__name__)

redis = redis_from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379/0"),
    decode_responses=True,
)

_CACHE_TTL = 30  # seconds

_JOBS_SQL = """
SELECT j.id,
       j.status,
       COALESCE(c.name, '') AS customer_name,
       COALESCE(c.address, '') AS address,
       COALESCE(u.name, u.email, '') AS tech_name,
       j.scheduled_at AS scheduled_time,
       COALESCE(j.lifecycle_stage, j.status) AS lifecycle_stage
FROM jobs j
LEFT JOIN customers c ON j.customer_id = c.id
LEFT JOIN users u ON j.tech_id = u.id
WHERE j.company_id = :tid
  AND j.deleted_at IS NULL
  AND j.status NOT IN ('Completed', 'Cancelled')
ORDER BY j.scheduled_at ASC NULLS LAST
LIMIT 100
"""

_TECHS_SQL = """
SELECT u.id,
       COALESCE(u.name, u.email, '') AS name,
       COALESCE(u.availability_status, 'available') AS status,
       u.current_job_id
FROM users u
WHERE u.company_id = :tid
  AND u.deleted_at IS NULL
  AND u.role IN ('technician', 'tech')
ORDER BY u.name ASC
"""


def _empty_board() -> dict:
    return {
        "jobs": [],
        "technicians": [],
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _row_to_dict(row: object) -> dict:
    """Convert a SQLAlchemy RowMapping to a plain dict with JSON-safe values."""
    out = {}
    for key, val in dict(row).items():
        if isinstance(val, datetime):
            out[key] = val.isoformat()
        elif val is None:
            out[key] = None
        else:
            out[key] = str(val) if not isinstance(val, (int, float, bool, str)) else val
    return out


def get_live_board(tenant_id: str) -> dict:
    """Return the live dispatch board for the given tenant.

    Checks Redis cache first (30s TTL). On miss, queries the tenant DB via
    engine_registry and caches the result before returning.
    """
    cache_key = f"live_board:{tenant_id}"

    # --- Cache check ---
    try:
        cached = redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as exc:
        log.exception("get_live_board_failed")
        log.warning("live_dispatch: Redis cache read failed: %s", exc)

    # --- DB query ---
    try:
        from gdx_dispatch.core.tenant import engine_registry  # local import avoids circular deps

        engine = engine_registry._engines.get(tenant_id)
        if engine is None:
            log.debug("live_dispatch: no engine for tenant %s — returning empty board", tenant_id)
            return _empty_board()

        with engine.connect() as conn:
            jobs_rows = conn.execute(text(_JOBS_SQL), {"tid": tenant_id}).mappings().all()
            tech_rows = conn.execute(text(_TECHS_SQL), {"tid": tenant_id}).mappings().all()

        board = {
            "jobs": [_row_to_dict(r) for r in jobs_rows],
            "technicians": [_row_to_dict(r) for r in tech_rows],
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except Exception as exc:
        log.exception("get_live_board_failed")
        log.error("live_dispatch: DB query failed for tenant %s: %s", tenant_id, exc)
        return _empty_board()

    # --- Write to cache ---
    try:
        redis.setex(cache_key, _CACHE_TTL, json.dumps(board))
    except Exception as exc:
        log.exception("get_live_board_failed")
        log.warning("live_dispatch: Redis cache write failed: %s", exc)

    return board


def invalidate_board_cache(tenant_id: str) -> None:
    """Invalidate the cached dispatch board for a tenant (call on job updates)."""
    try:
        redis.delete(f"live_board:{tenant_id}")
    except Exception as exc:
        log.exception("invalidate_board_cache_failed")
        log.warning("live_dispatch: cache invalidation failed for tenant %s: %s", tenant_id, exc)
