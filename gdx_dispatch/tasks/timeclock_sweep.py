"""MH-7b — sweep timeclock entries that have been open longer than
`MAX_SHIFT_HOURS` and auto-close them.

Beat-scheduled (every 30 min). Closes any shifts past the policy threshold.
The auto-close is audited as `timeclock_auto_close` with
`reason=sweep_max_shift`. The clock-in router has the same guard for the
case where the same tech tries to clock back in (so a manual flow doesn't
have to wait for the next beat).

Pre-fix: the back-end accepted indefinite open shifts. A 2026-06-04
mobile walk found one auditor session at 781:14:00 (32+ days). The UI
warned but nothing closed the shift.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text

from gdx_dispatch.celery_app import celery_app
from gdx_dispatch.core.database import app_engine
from gdx_dispatch.routers.timeclock import MAX_SHIFT_HOURS

log = logging.getLogger(__name__)


def _close_stale_for_tenant(tenant_id: str) -> dict[str, int]:
    """Close every open shift older than MAX_SHIFT_HOURS."""
    closed = 0
    failures = 0
    try:
        with app_engine.begin() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, clock_in_at
                    FROM timeclock_entries_router
                    WHERE clock_out_at IS NULL
                      AND deleted_at IS NULL
                      AND clock_in_at < (NOW() - (:hours || ' hours')::interval)::text
                    """
                ),
                {"hours": MAX_SHIFT_HOURS},
            ).mappings().all()
            for row in rows:
                entry_id = row["id"]
                clock_in_iso = row["clock_in_at"]
                try:
                    parsed = datetime.fromisoformat(
                        str(clock_in_iso).replace("Z", "+00:00")
                    )
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    minutes = max(0, int((now - parsed).total_seconds() // 60))
                    conn.execute(
                        text(
                            """
                            UPDATE timeclock_entries_router
                               SET clock_out_at = :now,
                                   minutes      = :minutes,
                                   updated_at   = :now
                             WHERE id = :id
                            """
                        ),
                        {"now": now.isoformat(), "minutes": minutes, "id": entry_id},
                    )
                    conn.execute(
                        text(
                            """
                            INSERT INTO audit_logs
                              (id, tenant_id, user_id, action, entity_type,
                               entity_id, details, created_at)
                            VALUES
                              (gen_random_uuid(), :tid, 'system',
                               'timeclock_auto_close', 'timeclock_entry',
                               :eid, :details::jsonb, :now)
                            """
                        ),
                        {
                            "tid": tenant_id,
                            "eid": entry_id,
                            "details": (
                                f'{{"minutes": {minutes}, '
                                f'"reason": "sweep_max_shift", '
                                f'"max_shift_hours": {MAX_SHIFT_HOURS}}}'
                            ),
                            "now": now.isoformat(),
                        },
                    )
                    closed += 1
                except Exception:
                    log.exception(
                        "timeclock_sweep_close_failed tenant=%s entry=%s",
                        tenant_id, entry_id,
                    )
                    failures += 1
        return {"closed": closed, "failures": failures}
    except Exception:
        log.exception("timeclock_sweep_tenant_failed tenant=%s", tenant_id)
        return {"closed": closed, "failures": failures + 1}


@celery_app.task(
    name="gdx_dispatch.tasks.timeclock_sweep.sweep_stuck_shifts_for_all_tenants",
    queue="priority.low",
)
def sweep_stuck_shifts_for_all_tenants() -> dict[str, int]:
    """Close every shift open longer than MAX_SHIFT_HOURS."""
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    result = _close_stale_for_tenant(tenant_id)
    if result["closed"]:
        log.info("timeclock_sweep closed=%d", result["closed"])
    return {
        "closed": result["closed"],
        "failures": result["failures"],
        "tenants": 1,
        "max_shift_hours": MAX_SHIFT_HOURS,
    }
