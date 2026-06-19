"""SS-30 slice B tests — cutover_orchestrator."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import cutover_orchestrator as co
from gdx_dispatch.models.platform_extensions import Base as OutboxBase
from gdx_dispatch.models.platform_ss29_additions import (
    SS29Base,
    ShadowMigrationState,
)
from gdx_dispatch.models.platform_ss30_additions import (
    CutoverSchedule,
    SS30Base,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    SS30Base.metadata.create_all(engine)
    OutboxBase.metadata.create_all(engine)
    # Create two real tables so ALTER TABLE renames actually work.
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers_v1 (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE customers_v2 (id INTEGER PRIMARY KEY)"))
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _now():
    return datetime.now(timezone.utc)


def _mk_state(db, tenant="t1", table="customers_v1", mode="shadow"):
    row = ShadowMigrationState(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        new_table=table.replace("_v1", "_v2"),
        mode=mode,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def test_happy_path_renames_and_schedules(db):
    _mk_state(db)
    result = co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    assert result.dry_run is False
    assert result.deprecated_table == "customers_v1_v1_deprecated"
    assert result.new_table == "customers_v2"
    db.commit()

    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1_v1_deprecated" in tables
    # new_table got renamed to old_table (customers_v2 → customers_v1).
    assert "customers_v1" in tables
    assert "customers_v2" not in tables

    sched = db.query(CutoverSchedule).one()
    assert sched.tenant_id == "t1"
    assert sched.deprecated_table == "customers_v1_v1_deprecated"
    assert sched.scheduled_drop_at > sched.executed_at


def test_state_mode_flipped_to_cutover(db):
    state = _mk_state(db)
    co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    db.refresh(state)
    assert state.mode == "cutover"
    assert state.cutover_at is not None


def test_dry_run_does_not_alter(db):
    _mk_state(db)
    result = co.run_cutover(
        db, tenant_id="t1", old_table="customers_v1", dry_run=True
    )
    assert result.dry_run is True
    db.commit()

    engine = db.get_bind()
    tables = set(inspect(engine).get_table_names())
    assert "customers_v1" in tables
    assert "customers_v2" in tables
    assert "customers_v1_v1_deprecated" not in tables
    assert db.query(CutoverSchedule).count() == 0


def test_dry_run_still_emits_executed_event(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    _mk_state(db)
    co.run_cutover(
        db, tenant_id="t1", old_table="customers_v1", dry_run=True
    )
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.executed.v1"
    ).all()
    assert len(events) == 1
    assert events[0].payload["dry_run"] is True


def test_executed_event_emitted(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    _mk_state(db)
    co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.executed.v1"
    ).all()
    assert len(events) == 1
    p = events[0].payload
    assert p["dry_run"] is False
    assert p["deprecated_table"] == "customers_v1_v1_deprecated"


def test_scheduled_event_emitted(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    _mk_state(db)
    co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.scheduled.v1"
    ).all()
    assert len(events) == 1


def test_requires_shadow_mode(db):
    _mk_state(db, mode="off")
    with pytest.raises(co.CutoverError):
        co.run_cutover(db, tenant_id="t1", old_table="customers_v1")


def test_no_state_row_raises(db):
    with pytest.raises(co.CutoverError):
        co.run_cutover(db, tenant_id="t1", old_table="customers_v1")


def test_idempotent_when_already_cutover(db):
    _mk_state(db)
    r1 = co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    assert r1.already_cut_over is False

    r2 = co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    assert r2.already_cut_over is True
    assert r2.deprecated_table == r1.deprecated_table
    # Only one schedule row.
    assert db.query(CutoverSchedule).count() == 1


def test_rename_failure_rolls_back_and_emits_cancelled(db):
    from gdx_dispatch.models.platform_extensions import EventOutbox

    state = _mk_state(db)

    def bad_rename(db_, src, dst):
        raise RuntimeError("simulated DDL failure")

    with pytest.raises(co.CutoverError):
        co.run_cutover(
            db, tenant_id="t1", old_table="customers_v1",
            rename_fn=bad_rename,
        )

    # Mode flip rolled back along with the rename attempt.
    db.expire_all()
    state = db.query(ShadowMigrationState).one()
    assert state.mode == "shadow"

    # cancelled event was emitted.
    events = db.query(EventOutbox).filter(
        EventOutbox.event_name == "gdx_dispatch.cutover.cancelled.v1"
    ).all()
    assert len(events) == 1
    assert "simulated" in events[0].payload["reason"]
    assert events[0].payload["error_class"] == "RuntimeError"

    # No schedule row persisted.
    assert db.query(CutoverSchedule).count() == 0


def test_invalid_grace_period(db):
    _mk_state(db)
    with pytest.raises(co.CutoverError):
        co.run_cutover(
            db, tenant_id="t1", old_table="customers_v1",
            grace_period_days=0,
        )
    with pytest.raises(co.CutoverError):
        co.run_cutover(
            db, tenant_id="t1", old_table="customers_v1",
            grace_period_days=co.MAX_GRACE_PERIOD_DAYS + 1,
        )


def test_explicit_new_table_override(db):
    # New table not in the shadow_map.
    engine = db.get_bind()
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widgets_v2 (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE widgets_v1 (id INTEGER PRIMARY KEY)"))

    row = ShadowMigrationState(
        id=uuid4(),
        tenant_id="t1",
        old_table="widgets_v1",
        new_table="widgets_v2",
        mode="shadow",
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.flush()

    result = co.run_cutover(
        db, tenant_id="t1", old_table="widgets_v1",
        new_table="widgets_v2",
    )
    assert result.new_table == "widgets_v2"


def test_extend_deprecation(db):
    _mk_state(db)
    co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    before = db.query(CutoverSchedule).one().scheduled_drop_at

    row = co.extend_deprecation(
        db, tenant_id="t1", old_table="customers_v1",
        additional_days=10, actor_identity_id="u-super",
    )
    assert (row.scheduled_drop_at - before).days == 10
    assert row.extended_count == "1"


def test_extend_deprecation_exceeds_cap(db):
    _mk_state(db)
    co.run_cutover(db, tenant_id="t1", old_table="customers_v1")
    with pytest.raises(co.CutoverError, match="MAX_GRACE_PERIOD_DAYS"):
        co.extend_deprecation(
            db, tenant_id="t1", old_table="customers_v1",
            additional_days=co.MAX_GRACE_PERIOD_DAYS,
        )


def test_extend_deprecation_no_row(db):
    with pytest.raises(co.CutoverError, match="no cutover_schedule"):
        co.extend_deprecation(
            db, tenant_id="t1", old_table="customers_v1", additional_days=1,
        )


def test_extend_deprecation_requires_positive(db):
    with pytest.raises(co.CutoverError):
        co.extend_deprecation(
            db, tenant_id="t1", old_table="customers_v1", additional_days=0,
        )
