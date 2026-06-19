"""SS-34 model stub additions — DR drill + snapshot manifest + verification.

TODO: these definitions live on a separate declarative Base
(``SS34Base``) and are NOT yet mounted on the primary platform Base in
``gdx_dispatch/models/platform.py``. The SS-34 Alembic migration
(``TODO_ss34_dr_XXXX.py``) sits on placeholder
``down_revision = "TODO"``.

Tables:
    - dr_snapshot_manifest    — one row per produced snapshot: id,
                                sha256, size, scope, location.
    - dr_drill_run            — one row per drill: schedule, outcome,
                                linked snapshot + restore + verification
                                report ids.
    - dr_verification_report  — full verification report rows (JSON
                                blob of checks + per-check outcomes);
                                re-runs append new rows, never mutate.

Idempotency note
----------------

``dr_drill_run.drill_run_id`` is the idempotency key matching the
orchestrator's ``run_drill(drill_run_id=…)`` parameter. A re-call with
the same id reads the prior row and returns it verbatim.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    JSON,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import declarative_base

SS34Base = declarative_base()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DrSnapshotManifest(SS34Base):
    """One row per produced backup snapshot.

    ``sha256`` is the content hash computed by
    :func:`gdx_dispatch.core.dr.backup_snapshot.create_snapshot`. ``backup_location``
    is either a local path or a remote URI; for SS-34 we only persist
    the location string — retrieval is the caller's responsibility.
    """

    __tablename__ = "dr_snapshot_manifest"
    __table_args__ = (
        Index("ix_dr_snap_created", "created_at"),
        Index("ix_dr_snap_sha", "sha256"),
    )

    id = Column(String(128), primary_key=True)  # snap-<label>-<hex>
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    size_bytes = Column(BigInteger, nullable=False)
    sha256 = Column(String(64), nullable=False)
    scope_description = Column(String(256), nullable=False)
    backup_location = Column(Text, nullable=False)
    source_db_redacted = Column(Text, nullable=True)


class DrDrillRun(SS34Base):
    """One row per scheduled drill.

    Lifecycle:
        scheduled → started → completed | failed

    ``passed`` is True only when every verification check passed.
    ``failure_reason`` carries the stage-prefixed summary from
    :class:`~gdx_dispatch.core.dr.drill_orchestrator.DrillReport`.
    """

    __tablename__ = "dr_drill_run"
    __table_args__ = (
        Index("ix_dr_drill_scheduled", "scheduled_for"),
        Index("ix_dr_drill_status", "passed"),
    )

    drill_run_id = Column(String(64), primary_key=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    scope = Column(String(32), nullable=False)
    scope_selector = Column(String(256), nullable=True)
    dry_run = Column(Boolean, nullable=False, default=False)
    passed = Column(Boolean, nullable=False, default=False)
    failure_reason = Column(Text, nullable=True)
    snapshot_id = Column(String(128), nullable=True)
    scheduled_by_identity_id = Column(String(64), nullable=True)
    staging_db_redacted = Column(Text, nullable=True)
    report_json = Column(JSON, nullable=True)


class DrVerificationReport(SS34Base):
    """One row per verification run.

    Re-runs (via the admin ``rerun-verification`` endpoint) APPEND a
    new row rather than mutating the prior one — the sequence of
    reports for a drill is itself evidence.
    """

    __tablename__ = "dr_verification_report"
    __table_args__ = (
        Index("ix_dr_verify_drill", "drill_run_id"),
        Index("ix_dr_verify_created", "created_at"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    drill_run_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    passed = Column(Boolean, nullable=False, default=False)
    failed_count = Column(BigInteger, nullable=False, default=0)
    checks_json = Column(JSON, nullable=False)
