"""SS-30 slice C tests — cutover event emitters + schema validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import cutover_events as ce
from gdx_dispatch.core import event_catalog
from gdx_dispatch.models.platform_extensions import Base as OutboxBase


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def test_all_four_schemas_registered():
    types = {t["event_type"] for t in event_catalog.list_event_types()}
    assert ce.EVENT_SCHEDULED in types
    assert ce.EVENT_EXECUTED in types
    assert ce.EVENT_CANCELLED in types
    assert ce.EVENT_DEPRECATED_TABLE_DROPPED in types


def test_emit_scheduled_validates(db):
    row = ce.emit_cutover_scheduled(
        db, tenant_id="t1", old_table="customers_v1",
        deprecated_table="customers_v1_v1_deprecated",
        scheduled_drop_at=_now() + timedelta(days=30),
        grace_period_days=30,
        actor_identity_id="u7",
    )
    db.flush()
    event_catalog.validate_event(ce.EVENT_SCHEDULED, row.payload)


def test_emit_executed_validates(db):
    row = ce.emit_cutover_executed(
        db, tenant_id="t1", old_table="customers_v1",
        new_table="customers_v2",
        deprecated_table="customers_v1_v1_deprecated",
        dry_run=False,
    )
    event_catalog.validate_event(ce.EVENT_EXECUTED, row.payload)
    assert row.payload["dry_run"] is False


def test_emit_executed_dry_run(db):
    row = ce.emit_cutover_executed(
        db, tenant_id="t1", old_table="customers_v1",
        new_table="customers_v2",
        deprecated_table="customers_v1_v1_deprecated",
        dry_run=True,
    )
    event_catalog.validate_event(ce.EVENT_EXECUTED, row.payload)
    assert row.payload["dry_run"] is True


def test_emit_cancelled_validates(db):
    row = ce.emit_cutover_cancelled(
        db, tenant_id="t1", old_table="customers_v1",
        reason="DDL failure",
        new_table="customers_v2",
        error_class="OperationalError",
    )
    event_catalog.validate_event(ce.EVENT_CANCELLED, row.payload)
    assert row.payload["reason"] == "DDL failure"
    assert row.payload["error_class"] == "OperationalError"


def test_emit_dropped_validates(db):
    row = ce.emit_deprecated_table_dropped(
        db, tenant_id="t1",
        deprecated_table="customers_v1_v1_deprecated",
        scheduled_drop_at=_now() - timedelta(days=1),
        old_table="customers_v1",
    )
    event_catalog.validate_event(ce.EVENT_DEPRECATED_TABLE_DROPPED, row.payload)


def test_scheduled_missing_required(db):
    with pytest.raises(ValueError):
        ce.emit_cutover_scheduled(
            db, tenant_id="", old_table="customers_v1",
            deprecated_table="x", scheduled_drop_at=_now(),
        )


def test_executed_missing_required(db):
    with pytest.raises(ValueError):
        ce.emit_cutover_executed(
            db, tenant_id="t1", old_table="",
            new_table="x", deprecated_table="y",
        )


def test_cancelled_missing_reason(db):
    with pytest.raises(ValueError):
        ce.emit_cutover_cancelled(
            db, tenant_id="t1", old_table="customers_v1", reason="",
        )


def test_dropped_missing_required(db):
    with pytest.raises(ValueError):
        ce.emit_deprecated_table_dropped(
            db, tenant_id="", deprecated_table="x",
            scheduled_drop_at=_now(),
        )


def test_schema_files_exist_on_disk():
    """SS-30 slice C — the four JSON files must ship on disk for
    event_catalog auto-discovery."""
    from pathlib import Path

    schema_dir = Path(event_catalog.SCHEMA_DIR)
    for name in (
        "gdx_dispatch.cutover.scheduled.v1.json",
        "gdx_dispatch.cutover.executed.v1.json",
        "gdx_dispatch.cutover.cancelled.v1.json",
        "gdx_dispatch.cutover.deprecated_table_dropped.v1.json",
    ):
        assert (schema_dir / name).is_file(), f"missing schema file: {name}"
