"""Migration 123: one hash-chained marker row is the boundary between the
forks the unlocked writer made and a fork that must alert (GDXA-979).

On both engines, against an ORM-built schema: a chain with a fork and a
restart dated before the run reads as broken with no marker; after the upgrade
the marker chains onto the tip, records the baseline, and both breaks are
history; a rerun writes no second marker; the downgrade writes and deletes
nothing; a fork written after the marker is new.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import uuid
from datetime import UTC, datetime

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations/versions"
MIGRATION = VERSIONS / "123_audit_chain_serialized.py"
REV = "123_audit_chain_serialized"


def _load(conn):
    spec = importlib.util.spec_from_file_location(MIGRATION.stem, MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if conn is not None:
        mod.op = Operations(MigrationContext.configure(conn))
    return mod


def test_it_chains_onto_122_and_is_the_only_reviser() -> None:
    source = MIGRATION.read_text()
    assert f'revision = "{REV}"' in source
    assert 'down_revision = "122_device_alert_registrations"' in source
    assert len(REV) <= 32  # alembic_version.version_num is VARCHAR(32)
    revising = [
        p.name for p in VERSIONS.glob("*.py")
        if re.search(r'^down_revision = "122_device_alert_registrations"', p.read_text(), re.M)
    ]
    assert revising == [MIGRATION.name], revising


def test_its_action_is_the_one_the_verifier_reads() -> None:
    from gdx_dispatch.core.audit import AUDIT_CHAIN_SERIALIZED_ACTION

    assert _load(None).ACTION == AUDIT_CHAIN_SERIALIZED_ACTION


def test_no_unescaped_percent_signs_in_its_sql() -> None:
    m = _load(None)
    for stmt in (m._EXISTING, m._FORKED_PARENTS, m._STARTS):
        assert "%" not in stmt.text.replace("%%", ""), stmt.text


def _hashed(s: Session, *, prev_hash: str, entity_id: str, at: datetime):
    """A correctly hashed row with a chosen predecessor and time: what the
    unlocked writer left behind."""
    from gdx_dispatch.core.audit import AuditLog, _expected_row_hash

    row = AuditLog(
        id=uuid.uuid4(), tenant_id="t", user_id="u", action="old", entity_type="job",
        entity_id=entity_id, details={}, prev_hash=prev_hash, created_at=at,
        event_type="old", actor_id="u", payload={},
    )
    row.row_hash = row.hash = _expected_row_hash(row, prev_hash)
    s.add(row)
    s.flush()
    return row


def _markers(s: Session):
    from gdx_dispatch.core.audit import AUDIT_CHAIN_SERIALIZED_ACTION, AuditLog

    return s.scalars(select(AuditLog).where(AuditLog.action == AUDIT_CHAIN_SERIALIZED_ACTION)).all()


def _exercise(engine) -> None:
    from gdx_dispatch.core.audit import AuditLog, audit_chain_report

    with Session(engine) as s:
        root = _hashed(s, prev_hash="", entity_id="root", at=datetime(2026, 9, 1, tzinfo=UTC))
        _hashed(s, prev_hash=root.row_hash, entity_id="a", at=datetime(2026, 9, 2, tzinfo=UTC))
        _hashed(s, prev_hash=root.row_hash, entity_id="b", at=datetime(2026, 9, 2, tzinfo=UTC))
        _hashed(s, prev_hash="", entity_id="restart", at=datetime(2026, 9, 3, tzinfo=UTC))
        s.commit()
        before = audit_chain_report(s)
        assert (len(before["forks"]), before["restarts"]) == (1, 1)
        assert before["known_breaks_before"] is None and before["broken"] is True
        tip = s.scalar(select(AuditLog.row_hash).where(AuditLog.entity_id == "restart"))

    with engine.begin() as c:
        _load(c).upgrade()

    with Session(engine) as s:
        (marker,) = _markers(s)
        assert marker.user_id == "migration:123_audit_chain_serialized"
        assert marker.prev_hash == tip, "the marker is a chained row on the tip"
        assert marker.details["forked_parents_before"] == 1
        assert marker.details["restarts_before"] == 1
        after = audit_chain_report(s)
        assert after["bad_hash"] == 0 and after["dangling"] == 0
        assert after["new_forks"] == 0 and after["new_restarts"] == 0
        assert after["known_breaks"] == 2 and after["broken"] is False
        assert after["known_breaks_before"] is not None
        count = s.scalar(select(func.count()).select_from(AuditLog))

    with engine.begin() as c:
        _load(c).upgrade()
    with engine.begin() as c:
        _load(c).downgrade()
    with Session(engine) as s:
        assert len(_markers(s)) == 1, "a rerun keeps the first boundary"
        assert s.scalar(select(func.count()).select_from(AuditLog)) == count

        # A fork the locked writer could not have made: after the marker.
        _hashed(s, prev_hash=marker.row_hash, entity_id="late-1", at=datetime.now(UTC))
        _hashed(s, prev_hash=marker.row_hash, entity_id="late-2", at=datetime.now(UTC))
        s.commit()
        late = audit_chain_report(s)
        assert late["new_forks"] == 1 and late["broken"] is True


def test_sqlite(tmp_path) -> None:
    from gdx_dispatch.core.audit import TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'm122.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    _exercise(engine)


def test_postgres(pg_test_db) -> None:
    """In a schema of its own built from the ORM, as 121's test does."""
    from gdx_dispatch.core.audit import TenantBase

    schema = "m122_test"
    engine = create_engine(pg_test_db, future=True, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            c.exec_driver_sql(f"CREATE SCHEMA {schema}")
        TenantBase.metadata.create_all(engine)
        _exercise(engine)
    finally:
        with engine.begin() as c:
            c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.dispose()


def test_on_an_empty_audit_log_it_writes_no_marker(tmp_path) -> None:
    """A fresh install, and the pave's step 4b: the pave upgrades an empty
    schema, then reloads the rows with the original marker among them, so a
    marker written here is one row too many (test_pave_rebuild_pg's
    `MISMATCH audit_logs: 3 -> 4`)."""
    from gdx_dispatch.core.audit import AuditLog, TenantBase

    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    TenantBase.metadata.create_all(bind=engine)
    with engine.begin() as c:
        _load(c).upgrade()
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == 0


def test_on_a_database_without_the_table_both_directions_are_no_ops(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    with engine.begin() as c:
        m = _load(c)
        m.upgrade()
        m.downgrade()
