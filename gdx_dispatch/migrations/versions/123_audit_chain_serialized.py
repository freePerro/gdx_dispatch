"""Mark the moment the audit chain's writers became serialized (GDXA-979).

Before GDXA-979, ``_log_audit_event_impl`` read the chain tip with a plain
SELECT and nothing held it until commit, so concurrent audited writes named the
same predecessor: a fork. Those forks are in ``audit_logs`` and stay there:
audit rows are never rewritten (append-only triggers, invariant #1), and the
chain is not re-anchored.

The verifier has to tell that history from a fork the locked writer could not
have made. A date in code cannot: a fork written between that date and the
deploy would page every night, and a deploy before it would silence real ones.
This migration runs as the code that holds the lock starts, so the row it
writes is the boundary. ``core.audit.audit_chain_report`` reads the
``created_at`` of the earliest ``audit_chain.serialized_writer`` row: a fork or
restart whose newest row is older is history (counted, not alerted); one at or
after it alerts. With no marker, nothing is history.

Existing rows: none changed. The migration appends one hash-chained audit row
(through ``log_audit_event_sync``, flushed into this migration's transaction)
that also records the dated baseline: how many parents had more than one child
and how many rows besides the first started the chain, as of this run.

Portability: one aggregate query with no literal ``%``; no Uuid comparison.
Rerunnable: an existing marker means no second row, so the boundary never
moves forward over forks that were already history.

Empty table: no marker. Nothing the unlocked writer made exists, so nothing is
history, which is what no marker means. This is a fresh install, and it is
also ``tools/pave_tenant_db.py`` step 4b, which upgrades an empty schema
before step 5 reloads the rows, the original marker among them.

Rollback: ``downgrade()`` writes nothing and deletes nothing. The marker is an
audit row and stays (the table refuses deletes). Code before GDXA-979 never
reads it. After a downgrade and a fresh upgrade the first marker still holds,
so any fork the unlocked writer made in between alerts, as it should.

Revision ID: 123_audit_chain_serialized
Revises: 122_device_alert_registrations
Create Date: 2026-10-14
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import inspect, text

revision = "123_audit_chain_serialized"
down_revision = "122_device_alert_registrations"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# Must equal core.audit.AUDIT_CHAIN_SERIALIZED_ACTION (pinned by the test).
ACTION = "audit_chain.serialized_writer"
ACTOR = "migration:123_audit_chain_serialized"

_EXISTING = text("SELECT 1 FROM audit_logs WHERE action = :action")
_ANY_ROW = text("SELECT 1 FROM audit_logs LIMIT 1")
_FORKED_PARENTS = text(
    "SELECT COUNT(*) FROM (SELECT prev_hash FROM audit_logs "
    "WHERE row_hash <> '' AND prev_hash <> '' GROUP BY prev_hash HAVING COUNT(*) > 1) forked"
)
_STARTS = text("SELECT COUNT(*) FROM audit_logs WHERE row_hash <> '' AND prev_hash = ''")


def upgrade() -> None:
    bind = op.get_bind()
    if "audit_logs" not in inspect(bind).get_table_names():
        return
    if bind.execute(_EXISTING, {"action": ACTION}).first() is not None:
        log.info("123: the audit-chain marker is already written; left as is")
        return
    if bind.execute(_ANY_ROW).first() is None:
        # No row, so no history for a boundary to separate. This is also the
        # pave's step 4b (empty schema, rows reloaded after): a marker written
        # here would sit beside the reloaded one and fail its row count.
        log.info("123: audit_logs is empty; no unlocked-writer history, no marker")
        return

    forked_parents = int(bind.execute(_FORKED_PARENTS).scalar() or 0)
    restarts = max(int(bind.execute(_STARTS).scalar() or 0) - 1, 0)

    from sqlalchemy.orm import Session

    from gdx_dispatch.core.audit import log_audit_event_sync

    session = Session(bind=bind)
    log_audit_event_sync(
        session,
        user_id=ACTOR,
        action=ACTION,
        entity_type="audit_logs",
        entity_id="chain",
        details={
            "reason": (
                "audit writers now serialize the chain-tip read to commit (GDXA-979); forks and "
                "restarts older than this row were made by the unlocked read and are history"
            ),
            "forked_parents_before": forked_parents,
            "restarts_before": restarts,
        },
    )
    session.flush()
    log.info("123: audit-chain marker written; %d forked parent(s), %d restart(s) are history", forked_parents, restarts)


def downgrade() -> None:
    # The marker is an audit row: never deleted. See the module docstring.
    pass
