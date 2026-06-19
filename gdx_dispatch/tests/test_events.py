"""SS-10 Slice B: emit_event helper tests.

Covers field mapping, Python-side source_event_id generation, and the
transactional contract that the helper never commits on the caller's behalf.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base
from gdx_dispatch.core.events import emit_event
from gdx_dispatch.models.platform_extensions import EventOutbox
from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug


def _make_engine(tmp_path):
    # File-based SQLite so two sessions hold independent transactional state.
    db_path = tmp_path / "events_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine, tables=[EventOutbox.__table__])
    return engine


def test_emit_event_maps_all_fields(tmp_path):
    engine = _make_engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        installation_id = uuid4()
        source_event_id = uuid4()
        row = emit_event(
            db,
            "install.created",
            {"foo": "bar", "n": 1},
            tenant_id=tenant_uuid_from_slug("tenant-x"),
            installation_id=installation_id,
            source_event_id=source_event_id,
        )
        db.commit()
        assert row.event_name == "install.created"
        assert row.payload == {"foo": "bar", "n": 1}
        assert row.tenant_id == tenant_uuid_from_slug("tenant-x")
        assert row.installation_id == installation_id
        assert row.source_event_id == source_event_id

        reloaded = db.scalar(select(EventOutbox).where(EventOutbox.id == row.id))
        assert reloaded is not None
        assert reloaded.event_name == "install.created"
        assert reloaded.payload == {"foo": "bar", "n": 1}
        assert reloaded.tenant_id == tenant_uuid_from_slug("tenant-x")
        assert reloaded.installation_id == installation_id
        assert reloaded.source_event_id == source_event_id
    finally:
        db.close()


def test_emit_event_generates_source_event_id_when_missing(tmp_path):
    engine = _make_engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        row_a = emit_event(db, "a", {})
        row_b = emit_event(db, "b", {})
        assert isinstance(row_a.source_event_id, UUID)
        assert isinstance(row_b.source_event_id, UUID)
        assert row_a.source_event_id != row_b.source_event_id
        db.commit()
    finally:
        db.close()


def test_emit_event_does_not_commit(tmp_path):
    """Row inserted by emit_event must not be visible to a separate session
    until the caller commits — proves no implicit commit inside the helper."""
    engine = _make_engine(tmp_path)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    writer = SessionLocal()
    reader = SessionLocal()
    try:
        source_event_id = uuid4()
        emit_event(
            writer,
            "no.commit",
            {"k": "v"},
            tenant_id=tenant_uuid_from_slug("t1"),
            source_event_id=source_event_id,
        )
        # writer has not committed; reader must not see the row yet
        found_before = reader.scalar(
            select(EventOutbox).where(EventOutbox.source_event_id == source_event_id)
        )
        assert found_before is None, "emit_event leaked a row before caller commit"
        # release reader's read txn so SQLite lets the writer acquire the write lock
        reader.rollback()

        writer.commit()

        # fresh reader txn now sees the row
        reader.rollback()
        found_after = reader.scalar(
            select(EventOutbox).where(EventOutbox.source_event_id == source_event_id)
        )
        assert found_after is not None
        assert found_after.event_name == "no.commit"
        assert found_after.payload == {"k": "v"}
        assert found_after.tenant_id == tenant_uuid_from_slug("t1")
    finally:
        writer.close()
        reader.close()


def test_emit_event_helper_source_has_no_commit_call():
    """Static guard: helper source must not call db.commit() itself."""
    from pathlib import Path

    import gdx_dispatch.core.events as events_mod

    src = Path(events_mod.__file__).read_text()
    assert ".commit(" not in src, "emit_event must not call commit()"
