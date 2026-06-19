"""Schema tests for RecurringStream + RecurringStreamHit.

Verifies the dual-term model (occurrences OR end_date OR neither), the
status/source enums round-trip correctly, the cascade on hits, and that
expected indexes are present so detector lookups + forecast projection
queries stay fast.
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    STREAM_SOURCE_MANUAL,
    STREAM_SOURCE_OBSERVED,
    STREAM_STATUS_ACTIVE,
    STREAM_STATUS_PAID_OFF,
    STREAM_STATUS_SUGGESTED,
    RecurringStream,
    RecurringStreamHit,
)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite doesn't enforce CHECK constraints unless PRAGMA is on per connection.
    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        yield s, engine
    finally:
        s.close()
        engine.dispose()


def test_tables_and_indexes_present(session):
    _, engine = session
    insp = inspect(engine)
    assert "recurring_streams" in insp.get_table_names()
    assert "recurring_stream_hits" in insp.get_table_names()

    stream_idx = {i["name"] for i in insp.get_indexes("recurring_streams")}
    assert "ix_recurring_streams_status_next" in stream_idx
    assert "ix_recurring_streams_source_status" in stream_idx

    hit_idx = {i["name"] for i in insp.get_indexes("recurring_stream_hits")}
    assert "ix_recurring_stream_hits_stream_date" in hit_idx


def test_open_ended_stream_has_no_term(session):
    s, _ = session
    stream = RecurringStream(
        label="Phone.com",
        source=STREAM_SOURCE_OBSERVED,
        status=STREAM_STATUS_SUGGESTED,
        payee_pattern="PHONE.COM",
        amount_min=43.00,
        amount_max=46.00,
        cadence=CADENCE_MONTHLY,
        cadence_anchor_day=9,
    )
    s.add(stream)
    s.commit()
    s.refresh(stream)
    assert stream.term_total_occurrences is None
    assert stream.term_end_date is None
    assert int(stream.occurrences_seen) == 0


def test_occurrences_term_stream(session):
    s, _ = session
    stream = RecurringStream(
        label="Midwest Bank loan 6705454",
        source=STREAM_SOURCE_MANUAL,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="MIDWEST BANK",
        amount_min=376.50,
        amount_max=376.60,
        cadence=CADENCE_MONTHLY,
        cadence_anchor_day=1,
        term_total_occurrences=36,
        occurrences_seen=14,
        start_date=date(2024, 1, 1),
    )
    s.add(stream)
    s.commit()
    s.refresh(stream)
    assert int(stream.term_total_occurrences) == 36
    assert stream.term_end_date is None
    assert int(stream.occurrences_seen) == 14


def test_end_date_term_stream(session):
    s, _ = session
    stream = RecurringStream(
        label="North Star Insurance policy CM63003",
        source=STREAM_SOURCE_MANUAL,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="NORTH STAR INS",
        amount_min=115.00,
        amount_max=116.00,
        cadence=CADENCE_MONTHLY,
        term_end_date=date(2028, 9, 1),
    )
    s.add(stream)
    s.commit()
    s.refresh(stream)
    assert stream.term_end_date == date(2028, 9, 1)
    assert stream.term_total_occurrences is None


def test_ending_a_stream_preserves_hits(session):
    s, _ = session
    sid = uuid4()
    stream = RecurringStream(
        id=sid,
        label="Service Titan",
        source=STREAM_SOURCE_OBSERVED,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="SERVICETIT",
        amount_min=1395.00,
        amount_max=1396.00,
        cadence=CADENCE_MONTHLY,
    )
    s.add(stream)
    s.add_all([
        RecurringStreamHit(stream_id=sid, qb_txn_id="qb-1", txn_date=date(2025, 9, 15), amount=1395.88, confirmed=True),
        RecurringStreamHit(stream_id=sid, qb_txn_id="qb-2", txn_date=date(2025, 12, 15), amount=1395.88, confirmed=True),
        RecurringStreamHit(stream_id=sid, qb_txn_id="qb-3", txn_date=date(2026, 2, 13), amount=1395.88, confirmed=True),
    ])
    s.commit()

    # End it (paid off / cancelled)
    stream.status = STREAM_STATUS_PAID_OFF
    stream.ended_at = date(2026, 3, 1)
    stream.ended_reason = "paid_off"
    s.commit()
    s.refresh(stream)

    assert stream.status == STREAM_STATUS_PAID_OFF
    assert stream.ended_at == date(2026, 3, 1)
    assert len(stream.hits) == 3, "ending must not delete historical hits"


def test_dual_term_mutual_exclusion_rejected(session):
    s, _ = session
    bad = RecurringStream(
        label="Both fields set",
        source=STREAM_SOURCE_MANUAL,
        payee_pattern="X",
        amount_min=10,
        amount_max=20,
        cadence=CADENCE_MONTHLY,
        term_total_occurrences=12,
        term_end_date=date(2027, 1, 1),
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()


def test_amount_window_inverted_rejected(session):
    s, _ = session
    bad = RecurringStream(
        label="Inverted window",
        source=STREAM_SOURCE_MANUAL,
        payee_pattern="X",
        amount_min=500,
        amount_max=100,
        cadence=CADENCE_MONTHLY,
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()


def test_anchor_day_out_of_range_rejected(session):
    s, _ = session
    bad = RecurringStream(
        label="Anchor 99",
        source=STREAM_SOURCE_MANUAL,
        payee_pattern="X",
        amount_min=1,
        amount_max=2,
        cadence=CADENCE_MONTHLY,
        cadence_anchor_day=99,
    )
    s.add(bad)
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()


def test_duplicate_hit_blocked_by_unique_constraint(session):
    """Detector retry must not double-count same bank txn against a stream.

    Without the (stream_id, qb_txn_id) uniqueness guard a cron retry inflates
    occurrences_seen and a term-bounded loan declares paid_off early.
    """
    s, _ = session
    sid = uuid4()
    s.add(RecurringStream(
        id=sid,
        label="Loan",
        source=STREAM_SOURCE_OBSERVED,
        payee_pattern="LOAN",
        amount_min=300,
        amount_max=400,
        cadence=CADENCE_MONTHLY,
    ))
    s.add(RecurringStreamHit(stream_id=sid, qb_txn_id="loan-payment-1", txn_date=date(2025, 1, 1), amount=376.56))
    s.commit()

    s.add(RecurringStreamHit(stream_id=sid, qb_txn_id="loan-payment-1", txn_date=date(2025, 1, 1), amount=376.56))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()


def test_integer_term_round_trips_as_int(session):
    """occurrences_seen and term_total_occurrences are Integer columns —
    Decimal round-tripping would force int() coercion in every consumer."""
    s, _ = session
    stream = RecurringStream(
        label="Int check",
        source=STREAM_SOURCE_MANUAL,
        payee_pattern="X",
        amount_min=1,
        amount_max=2,
        cadence=CADENCE_MONTHLY,
        term_total_occurrences=24,
        occurrences_seen=5,
    )
    s.add(stream)
    s.commit()
    s.refresh(stream)
    # Direct int math must work without coercion.
    assert stream.term_total_occurrences - stream.occurrences_seen == 19
    assert isinstance(stream.term_total_occurrences, int)
    assert isinstance(stream.occurrences_seen, int)


def test_cascade_delete_removes_hits(session):
    s, _ = session
    sid = uuid4()
    stream = RecurringStream(
        id=sid,
        label="Temp",
        source=STREAM_SOURCE_MANUAL,
        payee_pattern="X",
        amount_min=1,
        amount_max=2,
        cadence=CADENCE_QUARTERLY,
    )
    s.add(stream)
    s.add(RecurringStreamHit(stream_id=sid, qb_txn_id="q", txn_date=date(2026, 1, 1), amount=1.50))
    s.commit()

    s.delete(stream)
    s.commit()
    assert s.query(RecurringStreamHit).count() == 0
