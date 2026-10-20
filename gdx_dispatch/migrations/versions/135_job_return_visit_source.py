"""A return-visit job records which path created it (GDXA-1946).

* ``jobs.return_visit_source`` — varchar(20), nullable. Set by the three
  writers of a parent-linked return-visit child in ``routers/jobs.py``:
  ``closeout`` (the closeout's needs-return-visit child, a day-2
  continuation), ``callback`` (spawn-return-visit, offered as "Add warranty /
  callback") and ``follow_up`` (the follow-up endpoint). All three set
  ``is_return_visit`` True, so that flag cannot tell a continuation from a
  callback, and ``get_job`` labelled every closeout continuation CALLBACK.
  ``get_job`` now skips ``closeout`` children; NULL counts as a callback, as
  every child did before 135 (Doug, 2026-10-19).

Existing rows, exactly:

- **Changed:** a live or deleted job with ``is_return_visit`` true and
  ``return_visit_source IS NULL`` whose id is the ``entity_id`` of an
  ``audit_logs`` row its writer logged: ``return_visit_spawned`` with
  ``details.source = 'closeout'`` gives ``closeout`` (the closeout has logged
  that key since it began spawning children, 98127ac8, 2026-08-04), any other
  ``return_visit_spawned`` gives ``callback``, ``follow_up_job_created`` gives
  ``follow_up``. Nothing else on the row moves; ``updated_at`` is not touched.
- **Left NULL:** a child with no such audit row. It keeps reading as a
  callback, which is what it showed before 135. Logged at INFO with a count.

Trail: one hash-chained audit event (``return_visit_source_backfill``)
through ``log_audit_event_sync``, as 119 does, flushed into this migration's
transaction. It lists every job id per source. Nothing set, no event.

Portability: ``batch_alter_table`` as in 111: the upgrade is a plain
``ALTER TABLE ... ADD COLUMN`` on both engines (nullable, no default); only
the downgrade makes SQLite rebuild ``jobs``. The backfill reads ``details``
in Python (JSON comes back as text on SQLite) and binds each id in the form
the engine stores a Uuid (SQLite: 32 dashless hex). No ``%`` in any SQL. Not
a row-refusing migration: it never raises on data. Rerunnable: the column add
is skipped when present, and the backfill only writes NULL rows.

Rollback: ``downgrade()`` drops the column, and the older code labels every
parent-linked child within 90 days a callback again, as before 135.

Revision ID: 135_job_return_visit_source
Revises: 134_drop_tech_unavailability
Create Date: 2026-10-20
"""
from __future__ import annotations

import json
import logging
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision = "135_job_return_visit_source"
down_revision = "134_drop_tech_unavailability"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_TABLE = "jobs"
_COLUMN = "return_visit_source"
ACTION = "return_visit_source_backfill"
ACTOR = "migration:135_job_return_visit_source"

_EVENTS = text(
    "SELECT action, entity_id, details FROM audit_logs "
    "WHERE action IN ('return_visit_spawned', 'follow_up_job_created') "
    "ORDER BY created_at"
)
_SET = text(
    "UPDATE jobs SET return_visit_source = :source "
    "WHERE id = :id AND return_visit_source IS NULL AND is_return_visit = :yes"
)
_STILL_NULL = text(
    "SELECT COUNT(*) FROM jobs WHERE is_return_visit = :yes AND return_visit_source IS NULL"
)


def _columns(bind) -> set[str] | None:
    """The jobs table's column names, or None when the table is absent."""
    insp = inspect(bind)
    if not insp.has_table(_TABLE):
        return None
    return {col["name"] for col in insp.get_columns(_TABLE)}


def _source(action: str, details) -> str:
    if action == "follow_up_job_created":
        return "follow_up"
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = None
    if isinstance(details, dict) and details.get("source") == "closeout":
        return "closeout"
    return "callback"


def _backfill(bind) -> None:
    if not inspect(bind).has_table("audit_logs"):
        return
    dialect = bind.dialect.name
    # First audit row per job wins: a job is created once.
    sources: dict[str, str] = {}
    for action, entity_id, details in bind.execute(_EVENTS).fetchall():
        try:
            dashed = str(uuid.UUID(str(entity_id)))
        except (TypeError, ValueError):
            continue
        sources.setdefault(dashed, _source(action, details))

    changed: dict[str, list[str]] = {}
    for dashed, source in sources.items():
        stored = uuid.UUID(dashed).hex if dialect == "sqlite" else dashed
        if bind.execute(_SET, {"source": source, "id": stored, "yes": True}).rowcount:
            changed.setdefault(source, []).append(dashed)
    left = bind.execute(_STILL_NULL, {"yes": True}).scalar() or 0
    log.info(
        "135: return_visit_source set on %s; %d return visit(s) left NULL (read as callback)",
        {k: len(v) for k, v in changed.items()}, left,
    )
    if not changed:
        return

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION,
        entity_type="jobs",
        entity_id="batch",
        details={
            "reason": "record which path created each return visit, from its creation audit row (GDXA-1946)",
            "jobs": {k: sorted(v) for k, v in changed.items()},
            "left_null": left,
        },
    )
    session.flush()


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    if cols is None:
        return
    if _COLUMN not in cols:
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column(_COLUMN, sa.String(20), nullable=True))
    _backfill(bind)


def downgrade() -> None:
    cols = _columns(op.get_bind())
    if cols is None or _COLUMN not in cols:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
