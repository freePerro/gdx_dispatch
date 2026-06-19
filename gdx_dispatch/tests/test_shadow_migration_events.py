"""SS-29 slice C tests — event emitters + schema validation."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import event_catalog
from gdx_dispatch.core import shadow_migration_events as e
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


def test_enabled_schema_registered():
    types = {t["event_type"] for t in event_catalog.list_event_types()}
    assert e.EVENT_ENABLED in types
    assert e.EVENT_CUTOVER in types
    assert e.EVENT_ROLLBACK in types
    assert e.EVENT_DRIFT in types


def test_emit_enabled_validates(db):
    row = e.emit_shadow_enabled(
        db, tenant_id="t1", old_table="customers_v1", new_table="customers_v2",
        actor_identity_id="u7",
    )
    db.flush()
    event_catalog.validate_event(e.EVENT_ENABLED, row.payload)


def test_emit_cutover_payload(db):
    row = e.emit_shadow_cutover(
        db, tenant_id="t1", old_table="customers_v1", new_table="customers_v2",
    )
    event_catalog.validate_event(e.EVENT_CUTOVER, row.payload)
    assert row.payload["tenant_id"] == "t1"


def test_emit_rollback_payload(db):
    row = e.emit_shadow_rollback(
        db, tenant_id="t1", old_table="customers_v1", new_table="customers_v2",
        cutover_at=datetime.now(timezone.utc),
        reason="test",
    )
    event_catalog.validate_event(e.EVENT_ROLLBACK, row.payload)


def test_emit_drift_payload(db):
    row = e.emit_shadow_drift_detected(
        db, tenant_id="t1", old_table="customers_v1",
        reason="hash_mismatch",
        old_hash="a" * 64, new_hash="b" * 64,
    )
    event_catalog.validate_event(e.EVENT_DRIFT, row.payload)


def test_invalid_drift_reason_rejected(db):
    with pytest.raises(ValueError, match="invalid reason"):
        e.emit_shadow_drift_detected(
            db, tenant_id="t1", old_table="customers_v1", reason="bogus",
        )


def test_missing_required_args_rejected(db):
    with pytest.raises(ValueError):
        e.emit_shadow_enabled(db, tenant_id="", old_table="a", new_table="b")
