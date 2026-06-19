"""SS-30 slice E tests — cutover_cleanup_cron CLI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.models.platform_extensions import Base as OutboxBase
from gdx_dispatch.models.platform_ss30_additions import (
    CutoverSchedule,
    DeprecatedTableRecord,
    SS30Base,
)
from gdx_dispatch.tools import cutover_cleanup_cron as cron


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS30Base.metadata.create_all(engine)
    OutboxBase.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _now():
    return datetime.now(timezone.utc)


def _mk_schedule(db, tenant="t1", table="customers_v1", drop_in_days=-1):
    """Create a cutover_schedule row. drop_in_days<0 means it's eligible."""
    exec_at = _now() - timedelta(days=31)
    sched = CutoverSchedule(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        new_table=table.replace("_v1", "_v2"),
        deprecated_table=f"{table}_v1_deprecated",
        executed_at=exec_at,
        scheduled_drop_at=_now() + timedelta(days=drop_in_days),
        extended_count="0",
        dry_run=False,
        dropped_at=None,
        actor_identity_id=None,
        notes=None,
        created_at=exec_at,
        updated_at=exec_at,
    )
    db.add(sched)
    db.flush()
    db.commit()

    # Create the actual table to be dropped.
    engine = db.get_bind()
    with engine.begin() as conn:
        conn.execute(
            text(f"CREATE TABLE IF NOT EXISTS {sched.deprecated_table} "
                 "(id INTEGER PRIMARY KEY)")
        )
    return sched


def test_dry_run_does_not_drop(db):
    _mk_schedule(db)
    result = cron.run_cleanup(db, confirm=False)
    assert result.dry_run is True
    assert result.would_drop_count == 1
    assert result.dropped_count == 0

    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" in tables

    sched = db.query(CutoverSchedule).one()
    assert sched.dropped_at is None


def test_confirm_drops_real_table(db):
    _mk_schedule(db)
    result = cron.run_cleanup(db, confirm=True)
    assert result.dropped_count == 1
    assert result.dry_run is False

    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" not in tables

    sched = db.query(CutoverSchedule).one()
    assert sched.dropped_at is not None

    # Ledger row written.
    rec = db.query(DeprecatedTableRecord).one()
    assert rec.deprecated_table == "customers_v1_v1_deprecated"
    assert rec.dry_run is False


def test_hard_guard_blocks_future_drop(db):
    # scheduled 5 days out → must NOT drop even with --confirm.
    _mk_schedule(db, drop_in_days=5)
    result = cron.run_cleanup(db, confirm=True)
    assert result.dropped_count == 0
    assert result.skipped_count == 1

    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" in tables


def test_already_dropped_row_skipped(db):
    sched = _mk_schedule(db)
    sched.dropped_at = _now()
    db.flush()
    db.commit()

    result = cron.run_cleanup(db, confirm=True)
    assert len(result.rows) == 0  # filter excludes dropped rows


def test_tenant_filter(db):
    _mk_schedule(db, tenant="t1")
    _mk_schedule(db, tenant="t2", table="jobs_v1")

    result = cron.run_cleanup(db, confirm=False, tenant_filter="t1")
    assert len(result.rows) == 1
    assert result.rows[0].tenant_id == "t1"


def test_table_filter(db):
    _mk_schedule(db, tenant="t1", table="customers_v1")
    _mk_schedule(db, tenant="t1", table="jobs_v1")

    result = cron.run_cleanup(db, confirm=False, table_filter="jobs_v1")
    assert len(result.rows) == 1
    assert result.rows[0].old_table == "jobs_v1"


def test_limit(db):
    _mk_schedule(db, tenant="t1", table="customers_v1")
    _mk_schedule(db, tenant="t1", table="jobs_v1")
    _mk_schedule(db, tenant="t1", table="appts_v1")

    result = cron.run_cleanup(db, confirm=False, limit=2)
    assert len(result.rows) == 2


def test_drop_failure_does_not_crash_whole_run(db):
    _mk_schedule(db, tenant="t1", table="customers_v1")
    _mk_schedule(db, tenant="t1", table="jobs_v1")

    calls = {"n": 0}

    def flaky_drop(db_, tbl):
        calls["n"] += 1
        if tbl == "customers_v1_v1_deprecated":
            raise RuntimeError("simulated DROP failure")
        cron._default_drop(db_, tbl)

    result = cron.run_cleanup(db, confirm=True, drop_fn=flaky_drop)
    # One failed, one succeeded.
    assert result.dropped_count == 1
    assert result.skipped_count == 1
    assert any("drop failed" in r.reason for r in result.rows)


def test_dry_run_emits_event_with_dry_run_flag(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    _mk_schedule(db)
    cron.run_cleanup(db, confirm=False)
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.deprecated_table_dropped.v1"
    ).all()
    assert len(events) == 1
    assert events[0].payload["dry_run"] is True


def test_real_drop_emits_event(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    _mk_schedule(db)
    cron.run_cleanup(db, confirm=True)
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.deprecated_table_dropped.v1"
    ).all()
    assert len(events) == 1
    assert events[0].payload["dry_run"] is False


def test_to_dict():
    result = cron.CleanupResult(dry_run=True)
    result.rows.append(
        cron.CleanupRow(
            tenant_id="t1",
            old_table="customers_v1",
            deprecated_table="customers_v1_v1_deprecated",
            scheduled_drop_at=_now(),
            action="would_drop",
            reason="ready",
        )
    )
    d = result.to_dict()
    assert d["dry_run"] is True
    assert d["would_drop"] == 1
    assert len(d["rows"]) == 1


def test_cli_without_session_factory_exits_nonzero():
    rc = cron.main([], session_factory=None)
    assert rc == 2


def test_cli_dry_run_default(db):
    calls = []

    def factory():
        calls.append("created")
        return db

    _mk_schedule(db)
    rc = cron.main([], session_factory=factory)
    assert rc == 0
    # No drops since we didn't pass --confirm.
    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" in tables


def test_cli_confirm_drops(db):
    def factory():
        return db

    _mk_schedule(db)
    rc = cron.main(["--confirm"], session_factory=factory)
    assert rc == 0
    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" not in tables
