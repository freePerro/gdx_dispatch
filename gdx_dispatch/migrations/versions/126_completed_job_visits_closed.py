"""A completed job's open visits are closed, as completion now closes them.

Since 2026-10-05 (#868) every completion — /closeout, /complete and
/close-without-work — closes the job's open visits in the same transaction
(F1, ``services/visit_sync._finish``). A job completed before that kept its
visit at ``scheduled``, so the dispatch board's visit cards, the calendar, the
appointments list and its "unconfirmed" view read it as open or on-site work.
Prod read 36 such visits on 35 completed jobs, all completed 2026-03 to
2026-10-02; demo read none (read-only, 2026-10-15; recount on prod and demo
before deploying, see the PR). This migration is that backfill only.

Existing rows, exactly — F1's rule, with the job's completion day as its
finish day:

- **Closed:** a live visit (``deleted_at`` null, status not ``completed`` or
  ``cancelled``) of a live job with ``lifecycle_stage = 'completed'`` that is
  on site (``arrived_at`` set or status ``arrived``), or open on or before the
  job's completion day in shop time, or open with no completion day on the
  job. It gets ``status = 'completed'``, and ``completed_at`` = the job's
  ``completed_at`` when the visit had none (NULL stays NULL when the job has
  none: no time is invented).
- **Retired:** an open visit on a shop day after the job's completion day
  gets ``deleted_at`` = now, as F1 retires it (prod: none).
- **Left as is:** everything else — visits of open or cancelled jobs (prod:
  none on cancelled ones), of soft-deleted jobs (prod: 1), and visits already
  closed or cancelled. ``arrived_at``, ``tech_id``, ``start_at`` and
  ``updated_at`` never move; the record is the audit event.

Trail: one hash-chained audit event (``completed_job_visits_closed``) through
``log_audit_event_sync``, as 120 does, flushed into this migration's
transaction. It lists every visit changed with what it held before. Nothing
changed, no event.

Portability: ids are bound back in the form the engine returned them;
the downgrade re-encodes per dialect (SQLite keeps a Uuid as 32 hex). The shop
day is computed in Python from ``app_settings.timezone``, the same zone the
app reads (``core.pay_periods.shop_tz_name_from_settings``). No ``%`` in any
SQL. Not a row-refusing migration: it never raises on data. Rerunnable: a
second run finds nothing to change.

Rollback: ``downgrade()`` reads this migration's audit events and puts each
listed visit back to the status and ``completed_at`` it held, and un-retires a
retired one, only where the visit still holds what this migration wrote. By
hand: ``details.visits`` on the ``audit_logs`` row with ``action =
'completed_job_visits_closed'`` holds, per visit id, ``action`` (closed or
retired), ``status`` and ``completed_at`` as they were.

Revision ID: 126_completed_job_visits_closed
Revises: 125_planner_task_reminders
Create Date: 2026-10-15
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import inspect, text

revision = "126_completed_job_visits_closed"
down_revision = "125_planner_task_reminders"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

ACTION = "completed_job_visits_closed"
ACTOR = "migration:126_completed_job_visits_closed"

_CANDIDATES = text(
    """
SELECT a.id, a.status, a.arrived_at, a.start_at, a.completed_at,
       j.completed_at AS job_completed_at, a.company_id
FROM appointments a
JOIN jobs j ON j.id = a.job_id
WHERE a.deleted_at IS NULL
  AND j.deleted_at IS NULL
  AND j.lifecycle_stage = 'completed'
  AND LOWER(COALESCE(a.status, '')) NOT IN ('completed', 'cancelled')
ORDER BY a.start_at, a.id
"""
)
_TZ = text("SELECT timezone FROM app_settings LIMIT 1")
_CLOSE = text(
    "UPDATE appointments SET status = 'completed', "
    "completed_at = COALESCE(completed_at, :completed_at) "
    "WHERE id = :id AND deleted_at IS NULL "
    "AND LOWER(COALESCE(status, '')) NOT IN ('completed', 'cancelled')"
)
_RETIRE = text(
    "UPDATE appointments SET deleted_at = :now "
    "WHERE id = :id AND deleted_at IS NULL "
    "AND LOWER(COALESCE(status, '')) NOT IN ('completed', 'cancelled')"
)
_EVENTS = text("SELECT details FROM audit_logs WHERE action = :action")
_UNCLOSE = text(
    "UPDATE appointments SET status = :status, completed_at = :completed_at "
    "WHERE id = :id AND deleted_at IS NULL AND status = 'completed'"
)
_UNRETIRE = text(
    "UPDATE appointments SET deleted_at = NULL WHERE id = :id AND deleted_at = :retired_at"
)


def _dashed(raw) -> str:
    return str(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))


def _stored(dialect: str, dashed: str) -> str:
    """The id as this engine stores a Uuid column: SQLite keeps 32 hex."""
    return uuid.UUID(dashed).hex if dialect == "sqlite" else dashed


def _iso(value):
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _when(value):
    """A stored instant as an aware datetime (SQLite hands back naive UTC text)."""
    if value is None or value == "":
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace(" ", "T"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"appointments", "jobs", "audit_logs"} <= tables:
        return
    rows = bind.execute(_CANDIDATES).fetchall()
    if not rows:
        log.info("126: no open visits on completed jobs")
        return

    from gdx_dispatch.core.pay_periods import shop_day_of

    tz_name = (bind.execute(_TZ).scalar() if "app_settings" in tables else None) or "America/New_York"
    now = datetime.now(UTC)
    changed: dict[str, dict] = {}
    for r in rows:
        on_site = r.arrived_at is not None or (r.status or "").lower() == "arrived"
        finish_day = shop_day_of(_when(r.job_completed_at), tz_name)
        day = shop_day_of(_when(r.start_at), tz_name)
        retire = not on_site and finish_day is not None and day is not None and day > finish_day
        if retire:
            n = bind.execute(_RETIRE, {"id": r.id, "now": now}).rowcount
        else:
            n = bind.execute(_CLOSE, {"id": r.id, "completed_at": _when(r.job_completed_at)}).rowcount
        if n:
            changed[_dashed(r.id)] = {
                "action": "retired" if retire else "closed",
                "status": r.status,
                "completed_at": _iso(r.completed_at),
            }
    closed = sum(1 for v in changed.values() if v["action"] == "closed")
    log.info("126: %d visit(s) closed, %d retired, on completed jobs", closed, len(changed) - closed)
    if not changed:
        return

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    tenants = sorted({r.company_id for r in rows if r.company_id})
    log_audit_event_sync(
        session,
        tenant_id=(tenants[0] if len(tenants) == 1 else None),
        user_id=ACTOR,
        action=ACTION,
        entity_type="appointments",
        entity_id="batch",
        details={
            "reason": "visits of jobs completed before completion closed them (F1, #868)",
            "retired_at": now.isoformat(),
            "visits": changed,
        },
    )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"appointments", "audit_logs"} <= tables:
        return
    dialect = bind.dialect.name
    restored = 0
    ids: list[str] = []
    for (details,) in bind.execute(_EVENTS, {"action": ACTION}).fetchall():
        if isinstance(details, str):
            details = json.loads(details)
        details = details or {}
        retired_at = _when(details.get("retired_at"))
        for visit_id, prior in (details.get("visits") or {}).items():
            stored = _stored(dialect, visit_id)
            if prior.get("action") == "retired":
                if retired_at is None:
                    continue
                restored += bind.execute(_UNRETIRE, {"id": stored, "retired_at": retired_at}).rowcount
            else:
                restored += bind.execute(_UNCLOSE, {
                    "id": stored,
                    "status": prior.get("status") or "scheduled",
                    "completed_at": _when(prior.get("completed_at")),
                }).rowcount
            ids.append(visit_id)
    if not ids:
        return
    log.info("126 downgrade: %d visit(s) put back", restored)

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION + "_reverted",
        entity_type="appointments",
        entity_id="batch",
        details={"visit_ids": sorted(ids), "restored": restored},
    )
    session.flush()
