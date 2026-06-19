"""SS-23 Slice C: event drain worker tests — happy path + failure + retry + checkpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base
from gdx_dispatch.core import event_drain
from gdx_dispatch.core.event_drain import drain_once
from gdx_dispatch.models.platform_extensions import EventOutbox
from gdx_dispatch.models.platform_ss23_additions import (
    EventDrainCheckpoint,
    SS23Base,
)


def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'drain.db'}", future=True)
    Base.metadata.create_all(engine, tables=[EventOutbox.__table__])
    SS23Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def _seed_event(db, **kwargs) -> EventOutbox:
    from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug
    row = EventOutbox(
        event_name=kwargs.get("event_name", "gdx_dispatch.customer.created.v1"),
        source_event_id=kwargs.get("source_event_id", uuid4()),
        tenant_id=kwargs.get("tenant_id", tenant_uuid_from_slug("t1")),
        payload=kwargs.get("payload", {"k": "v"}),
    )
    db.add(row)
    db.commit()
    return row


def test_happy_path_delivers_and_stamps(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)
        calls = []

        def ok_sink(event):
            calls.append(event.id)

        stats = drain_once(db, sinks=[ok_sink])
        db.commit()

        assert stats.scanned == 1
        assert stats.delivered == 1
        assert stats.retried == 0
        assert calls == [e.id]

        reloaded = db.scalar(select(EventOutbox).where(EventOutbox.id == e.id))
        assert reloaded.delivered_at is not None

        cp = db.scalar(
            select(EventDrainCheckpoint).where(
                EventDrainCheckpoint.event_outbox_id == e.id
            )
        )
        assert cp.status == "delivered"
        assert cp.retry_count == 0
    finally:
        db.close()


def test_sink_failure_parks_for_retry(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)

        def boom(event):
            raise RuntimeError("sink boom")

        stats = drain_once(db, sinks=[boom])
        db.commit()

        assert stats.scanned == 1
        assert stats.delivered == 0
        assert stats.retried == 1

        reloaded = db.scalar(select(EventOutbox).where(EventOutbox.id == e.id))
        assert reloaded.delivered_at is None, "failing event must not be stamped delivered"

        cp = db.scalar(
            select(EventDrainCheckpoint).where(
                EventDrainCheckpoint.event_outbox_id == e.id
            )
        )
        assert cp.status == "retry"
        assert cp.retry_count == 1
        assert cp.last_error and "sink boom" in cp.last_error
        assert cp.retry_after is not None
        ra = cp.retry_after
        if ra.tzinfo is None:
            ra = ra.replace(tzinfo=timezone.utc)
        assert ra > datetime.now(timezone.utc)
    finally:
        db.close()


def test_retry_cooldown_is_respected(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)
        # Pre-seed a checkpoint in cooldown
        cp = EventDrainCheckpoint(
            event_outbox_id=e.id,
            status="retry",
            retry_count=1,
            last_error="prior",
            retry_after=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(cp)
        db.commit()

        calls = []

        def ok_sink(event):
            calls.append(event.id)

        stats = drain_once(db, sinks=[ok_sink])
        db.commit()

        assert stats.skipped == 1
        assert stats.delivered == 0
        assert calls == []  # cooldown respected
    finally:
        db.close()


def test_idempotent_rerun_on_delivered_row(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)

        def ok_sink(event):
            pass

        drain_once(db, sinks=[ok_sink])
        db.commit()

        # Second pass: delivered_at is set, row excluded from scan
        stats2 = drain_once(db, sinks=[ok_sink])
        db.commit()
        assert stats2.scanned == 0
        assert stats2.delivered == 0
    finally:
        db.close()


def test_dead_letter_after_max_retries(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)
        cp = EventDrainCheckpoint(
            event_outbox_id=e.id,
            status="retry",
            retry_count=event_drain.MAX_RETRIES,  # next failure → dead_letter
            last_error="prior",
            retry_after=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(cp)
        db.commit()

        def boom(event):
            raise RuntimeError("still bad")

        drain_once(db, sinks=[boom])
        db.commit()

        cp_reload = db.scalar(
            select(EventDrainCheckpoint).where(
                EventDrainCheckpoint.event_outbox_id == e.id
            )
        )
        assert cp_reload.status == "dead_letter"
        assert cp_reload.retry_after is None
    finally:
        db.close()


def test_all_sinks_must_succeed_before_delivered(tmp_path):
    db = _make_session(tmp_path)
    try:
        e = _seed_event(db)

        def ok(event):
            pass

        def boom(event):
            raise RuntimeError("nope")

        drain_once(db, sinks=[ok, boom])
        db.commit()

        reloaded = db.scalar(select(EventOutbox).where(EventOutbox.id == e.id))
        assert reloaded.delivered_at is None
    finally:
        db.close()
