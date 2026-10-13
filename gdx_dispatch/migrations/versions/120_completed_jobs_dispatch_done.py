"""Completed jobs read dispatch_status ``done`` (GDXA-834).

A completed job whose ``dispatch_status`` still says ``unassigned`` or
``assigned`` shows up as open work on the dispatch board and in every reader
that keys on dispatch status. Prod read 47 ``done``, 185 ``unassigned`` and 8
``assigned`` across 240 completed jobs (read-only, 2026-10-05, maintainer's
ledger; recount on prod and demo before deploying, see the PR). The
completion paths already write ``done`` (``routers/jobs.py`` closeout and
complete), the public create refuses a job born completed, and the PATCH stage
guard routes completion through /closeout, so what is left is the historical
rows. This migration is that backfill only.

Existing rows, exactly:

- **Changed:** a job with ``lifecycle_stage = 'completed'`` and
  ``dispatch_status <> 'done'``, soft-deleted or not, as specified. It gets
  ``dispatch_status = 'done'``. Nothing else on the row moves:
  ``lifecycle_stage``, ``completed_at``, ``assigned_to``, billing and money
  are untouched, and so is ``updated_at`` (the technician activity reader
  treats a moved ``updated_at`` as activity; the record is the audit event).
- **Left as is:** every job not in ``completed``, including cancelled ones.

Trail: one hash-chained audit event (``job_dispatch_done_backfill``) through
``log_audit_event_sync``, as 117 does, flushed into this migration's
transaction. It records every changed job id with the value it held before, so
the downgrade can put it back. Nothing changed, no event.

Portability: the ids written are the ids read from the same table, bound back
in the form the engine returned them in; the downgrade re-encodes per dialect
(SQLite keeps a Uuid as 32 hex). ``dispatch_status`` is a native enum on
Postgres and a VARCHAR on SQLite; both take a string literal from the enum's
own value list. No ``%`` in any SQL. Not a row-refusing migration: it never
raises on data. Rerunnable: a second run finds nothing to change.

Rollback: ``downgrade()`` reads this migration's audit events and sets each
listed job back to the ``dispatch_status`` it held, only where the job is
still ``completed`` and still ``done``. A job reopened since carries whatever
its new work wrote and is left alone. A job reopened and completed again
since reads ``done`` from the completion path, cannot be told apart from the
backfill, and goes back with the rest. By hand: take ``details.prior`` (id to
old value) from the ``audit_logs`` row with ``action =
'job_dispatch_done_backfill'`` and run ``UPDATE jobs SET dispatch_status =
:old WHERE id = :id AND lifecycle_stage = 'completed' AND dispatch_status =
'done'`` for each pair.

Revision ID: 120_completed_jobs_dispatch_done
Revises: 119_backfill_job_numbers
Create Date: 2026-10-12
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter

from alembic import op
from sqlalchemy import inspect, text

revision = "120_completed_jobs_dispatch_done"
down_revision = "119_backfill_job_numbers"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

ACTION = "job_dispatch_done_backfill"
ACTOR = "migration:120_completed_jobs_dispatch_done"

_CANDIDATES = text(
    """
SELECT id, dispatch_status, company_id
FROM jobs
WHERE lifecycle_stage = 'completed'
  AND dispatch_status <> 'done'
ORDER BY created_at, id
"""
)
_DONE = text(
    "UPDATE jobs SET dispatch_status = 'done' "
    "WHERE id = :id AND lifecycle_stage = 'completed' AND dispatch_status <> 'done'"
)
_EVENTS = text("SELECT details FROM audit_logs WHERE action = :action")
_RESTORE = text(
    "UPDATE jobs SET dispatch_status = :prior "
    "WHERE id = :id AND lifecycle_stage = 'completed' AND dispatch_status = 'done'"
)
# The enum's own values (tenant_models.Job.dispatch_status); a downgrade never
# writes anything else back, whatever the event holds.
_VALUES = ("unassigned", "assigned", "en_route", "on_site", "done")


def _dashed(raw) -> str:
    return str(raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw)))


def _stored(dialect: str, dashed: str) -> str:
    """The id as this engine stores a Uuid column: SQLite keeps 32 hex."""
    return uuid.UUID(dashed).hex if dialect == "sqlite" else dashed


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"jobs", "audit_logs"} <= tables:
        return
    rows = bind.execute(_CANDIDATES).fetchall()
    for r in rows:
        bind.execute(_DONE, {"id": r.id})
    log.info(
        "120: %d completed job(s) set to dispatch_status done (from %s)",
        len(rows), dict(Counter(r.dispatch_status for r in rows)),
    )
    if not rows:
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
        entity_type="jobs",
        entity_id="batch",
        details={
            "to": "done",
            "reason": "completed jobs left at an open dispatch status (GDXA-834)",
            "prior": {_dashed(r.id): r.dispatch_status for r in rows},
        },
    )
    session.flush()


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if not {"jobs", "audit_logs"} <= tables:
        return
    prior: dict[str, str] = {}
    for (details,) in bind.execute(_EVENTS, {"action": ACTION}).fetchall():
        if isinstance(details, str):
            details = json.loads(details)
        prior.update((details or {}).get("prior") or {})
    prior = {k: v for k, v in prior.items() if v in _VALUES and v != "done"}
    if not prior:
        return
    dialect = bind.dialect.name
    restored = 0
    for job_id, value in prior.items():
        restored += bind.execute(_RESTORE, {"id": _stored(dialect, job_id), "prior": value}).rowcount
    log.info("120 downgrade: %d job(s) set back to their prior dispatch_status", restored)

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION + "_reverted",
        entity_type="jobs",
        entity_id="batch",
        details={"job_ids": sorted(prior), "restored": restored},
    )
    session.flush()
