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
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.routers.timeclock import MAX_SHIFT_HOURS

log = logging.getLogger(__name__)


def _close_stale_for_tenant(tenant_id: str) -> dict[str, int]:
    """Close every open shift older than MAX_SHIFT_HOURS.

    2026-07-08 — rewritten after the task's FIRST prod run failed: the
    raw audit INSERT used `:details::jsonb` (SQLAlchemy text() sends the
    bind through as a literal `:details` when a double-colon cast
    follows it → Postgres syntax error) and also omitted the NOT NULL
    row_hash/prev_hash hash-chain columns; the single wrapping
    transaction then rolled the shift close back with it. Now: audit
    rows go through log_audit_event_sync (the hash-chained canonical
    path), each shift commits independently so one bad row can't undo
    the others, and the staleness cutoff is computed in Python — the
    column is ISO-8601 text, so this is the same lexicographic compare
    the old NOW()::text form did, and it makes the path testable on
    sqlite.
    """
    closed = 0
    failures = 0
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=MAX_SHIFT_HOURS)
    ).isoformat()
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                """
                SELECT id, clock_in_at
                FROM timeclock_entries_router
                WHERE clock_out_at IS NULL
                  AND deleted_at IS NULL
                  AND clock_in_at < :cutoff
                """
            ),
            {"cutoff": cutoff},
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
                db.execute(
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
                log_audit_event_sync(
                    db,
                    tenant_id=tenant_id,
                    user_id="system",
                    action="timeclock_auto_close",
                    entity_type="timeclock_entry",
                    entity_id=str(entry_id),
                    details={
                        "minutes": minutes,
                        "reason": "sweep_max_shift",
                        "max_shift_hours": MAX_SHIFT_HOURS,
                    },
                )
                db.commit()
                closed += 1
            except Exception:
                db.rollback()
                log.exception(
                    "timeclock_sweep_close_failed tenant=%s entry=%s",
                    tenant_id, entry_id,
                )
                failures += 1
        return {"closed": closed, "failures": failures}
    except Exception:
        log.exception("timeclock_sweep_tenant_failed tenant=%s", tenant_id)
        return {"closed": closed, "failures": failures + 1}
    finally:
        db.close()


@celery_app.task(
    name="gdx_dispatch.tasks.timeclock_sweep.sweep_stuck_shifts_for_all_tenants",
    queue="priority:low",
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
