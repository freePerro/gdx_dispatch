"""Tests for the observed-recurring detector.

Covers:
- payee normalization (strips bank-feed noise prefixes + card-ref suffixes)
- detection floor (≥3 occurrences, CV ≤ 0.25, distinct-months gate)
- cadence classification (monthly / quarterly / weekly bands)
- amount-cluster split (Service Titan $1395 vs Service Titan $11–15 → two streams)
- idempotency (re-running the detector adds no duplicate hits)
- update path on a second run (occurrences_seen + last_observed_date roll forward)
- existing 'active' stream gets refreshed but cadence isn't clobbered
- skips when fewer than MIN_OCCURRENCES — no premature 'suggested' rows
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_MONTHLY,
    CADENCE_QUARTERLY,
    STREAM_SOURCE_MANUAL,
    STREAM_SOURCE_OBSERVED,
    STREAM_STATUS_ACTIVE,
    STREAM_STATUS_SUGGESTED,
    RecurringStream,
    RecurringStreamHit,
)
from gdx_dispatch.modules.forecasting.observed_recurring import (
    find_candidates,
    normalize_payee,
    run_detector,
    upsert_streams,
)
from gdx_dispatch.modules.quickbooks.banking import QBBankTransaction


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.close()

    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_txn(db, *, qb_txn_id: str, payee: str, amount: float, txn_date: date, txn_type: str = "Cash"):
    db.add(QBBankTransaction(
        qb_txn_id=qb_txn_id,
        payee=payee,
        amount=amount,
        txn_date=txn_date,
        txn_type=txn_type,
    ))


# ─── normalization ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # QBO-cleaned (real GDX): full string preserved, uppercased
    ("Phone.com", "PHONE.COM"),
    ("North Star Insurance", "NORTH STAR INSURANCE"),
    ("Alexandria Tools and More", "ALEXANDRIA TOOLS AND MORE"),
    ("Microsoft ( 352 )", "MICROSOFT ( 352 )"),
    ("Amazon", "AMAZON"),
    # Dirty bank-feed: noise prefix stripped, txn-id fragments stripped
    ("DBT CRD 0925 55200916 PHONE.COM #16374977 PHONE.COM CA C#9043", "PHONE.COM PHONE.COM CA"),
    ("ATM RCR Payment PHONE.COM * #15880884 PHONE.COM CA #9657", "PHONE.COM PHONE.COM CA"),
    ("AUTOMATIC PAYMENT TO LOAN ACCT NO. 6705454", ""),
    # Edge cases
    ("", ""),
    (None, ""),
])
def test_normalize_payee(raw, expected):
    assert normalize_payee(raw) == expected


def test_normalize_payee_dirty_feed_collapses_to_stable_key():
    """Two raw bank-feed strings for the same merchant must normalize identically
    so the detector groups them. This is the load-bearing assumption — if the
    normalizer is unstable, the detector silently splits one subscription into
    several never-matured streams."""
    a = normalize_payee("DBT CRD 0925 55200916 PHONE.COM #16374977 PHONE.COM CA C#9043")
    b = normalize_payee("DBT CRD 1014 99323516 PHONE.COM #16213653 PHONE.COM CA C#9043")
    c = normalize_payee("POS DEB 0028 26663795 PHONE.COM #16052212 PHONE.COM CA C#9657")
    assert a == b == c, (a, b, c)


# ─── detection — happy path ─────────────────────────────────────────────────

def _seed_monthly_stream(db, payee: str, amount: float, start: date, count: int, anchor_day: int = 9, id_prefix: str = ""):
    """Seed `count` monthly txns. Pass id_prefix when seeding the same payee
    twice in one test to avoid qb_txn_id collisions on the unique constraint."""
    for i in range(count):
        approx_date = start.replace(day=anchor_day) + timedelta(days=i * 30 + ((-1) ** i))
        _seed_txn(db, qb_txn_id=f"{id_prefix}{payee}-{i}", payee=payee, amount=amount, txn_date=approx_date)
    db.commit()


def test_detects_monthly_phone_com_subscription(db):
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=12)
    cands = find_candidates(db, today=date(2026, 1, 15))
    assert len(cands) == 1
    c = cands[0]
    assert c.payee_norm == "PHONE.COM"
    assert c.cadence == CADENCE_MONTHLY
    assert c.occurrences == 12
    assert c.median_amount == 44.94
    assert c.amount_min < 44.94 < c.amount_max


def test_below_minimum_occurrences_does_not_match(db):
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 10, 1), count=2)
    cands = find_candidates(db, today=date(2026, 1, 15))
    assert cands == []


def test_high_cv_rejected_as_non_recurring(db):
    # Same payee monthly, but amounts swing wildly — not a subscription, a vendor
    _seed_txn(db, qb_txn_id="amex-1", payee="American Express", amount=500.00, txn_date=date(2025, 1, 5))
    _seed_txn(db, qb_txn_id="amex-2", payee="American Express", amount=2000.00, txn_date=date(2025, 2, 5))
    _seed_txn(db, qb_txn_id="amex-3", payee="American Express", amount=300.00, txn_date=date(2025, 3, 5))
    _seed_txn(db, qb_txn_id="amex-4", payee="American Express", amount=5000.00, txn_date=date(2025, 4, 5))
    db.commit()
    cands = find_candidates(db, today=date(2026, 1, 1))
    # CV too high for any cluster — nothing flagged
    assert cands == []


def test_distinct_months_gate_blocks_clustered_purchases(db):
    # Five hits but all in one week — not recurring, just a buying spree.
    for i in range(5):
        _seed_txn(db, qb_txn_id=f"home-depot-{i}", payee="Home Depot",
                  amount=99.99, txn_date=date(2025, 8, 8) + timedelta(days=i))
    db.commit()
    cands = find_candidates(db, today=date(2026, 1, 1))
    assert cands == []


def test_amount_cluster_split_creates_two_streams(db):
    # Service Titan: monthly $1395.88 subscription + monthly $15 small fee.
    # The two amounts should NOT merge into one stream.
    for i in range(6):
        d = date(2025, 7, 1) + timedelta(days=i * 30)
        _seed_txn(db, qb_txn_id=f"st-big-{i}", payee="Service Titan", amount=1395.88, txn_date=d)
        _seed_txn(db, qb_txn_id=f"st-small-{i}", payee="Service Titan", amount=15.00, txn_date=d + timedelta(days=2))
    db.commit()
    cands = find_candidates(db, today=date(2026, 1, 15))
    amounts = sorted(c.median_amount for c in cands)
    assert amounts == [15.00, 1395.88]


def test_quarterly_cadence_detected(db):
    for i in range(4):
        _seed_txn(db, qb_txn_id=f"quarterly-{i}", payee="Quarterly Service",
                  amount=250.00, txn_date=date(2025, 1, 1) + timedelta(days=i * 91))
    db.commit()
    cands = find_candidates(db, today=date(2026, 1, 1))
    assert len(cands) == 1
    assert cands[0].cadence == CADENCE_QUARTERLY


# ─── upsert + idempotency ───────────────────────────────────────────────────

def test_first_run_inserts_stream_and_hits(db):
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=12)
    stats = run_detector(db, today=date(2026, 1, 15))
    assert stats == {"inserted": 1, "updated": 0, "hits_added": 12}
    streams = db.query(RecurringStream).all()
    assert len(streams) == 1
    assert streams[0].status == STREAM_STATUS_SUGGESTED
    assert streams[0].source == STREAM_SOURCE_OBSERVED
    assert int(streams[0].occurrences_seen) == 12
    assert db.query(RecurringStreamHit).count() == 12


def test_second_run_is_idempotent(db):
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=12)
    run_detector(db, today=date(2026, 1, 15))
    stats2 = run_detector(db, today=date(2026, 1, 15))
    # Same data, second pass: no new inserts, hits already covered by the
    # uniqueness pre-check so 0 added (matches stream still gets updated).
    assert stats2["inserted"] == 0
    assert stats2["updated"] == 1
    assert stats2["hits_added"] == 0
    assert db.query(RecurringStream).count() == 1
    assert db.query(RecurringStreamHit).count() == 12


def test_new_txn_after_first_run_gets_attached_on_second(db):
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=12)
    run_detector(db, today=date(2026, 1, 15))

    # Sync brings in one new monthly hit
    _seed_txn(db, qb_txn_id="Phone.com-12", payee="Phone.com",
              amount=44.94, txn_date=date(2026, 1, 10))
    db.commit()

    stats = run_detector(db, today=date(2026, 1, 20))
    assert stats["updated"] == 1
    assert stats["hits_added"] == 1
    assert db.query(RecurringStreamHit).count() == 13


def test_active_stream_does_not_get_cadence_clobbered(db):
    """An 'active' (user-confirmed) stream's cadence is sacrosanct on re-detect."""
    sid_seed = "stream-1"
    db.add(RecurringStream(
        label="Phone.com",
        source=STREAM_SOURCE_MANUAL,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="PHONE.COM",
        amount_min=40.00,
        amount_max=50.00,
        cadence=CADENCE_QUARTERLY,  # user said quarterly
    ))
    db.commit()
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=12)
    run_detector(db, today=date(2026, 1, 15))

    s = db.query(RecurringStream).one()
    # cadence preserved — user is the source of truth on active streams
    assert s.cadence == CADENCE_QUARTERLY
    # but the observation fields rolled forward
    assert int(s.occurrences_seen) == 12
    assert s.last_observed_date is not None


def test_suggested_stream_cadence_updates_on_re_detect(db):
    """Inverse: a still-suggested stream's cadence can be refined."""
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=4, id_prefix="early-")
    run_detector(db, today=date(2026, 1, 15))
    # add 8 more monthly hits — same cadence, more data
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 5, 1), count=8, id_prefix="late-")
    run_detector(db, today=date(2026, 1, 15))
    s = db.query(RecurringStream).one()
    assert s.cadence == CADENCE_MONTHLY
    assert s.status == STREAM_STATUS_SUGGESTED


def test_deleted_txns_are_ignored(db):
    """Tombstoned bank txns must NOT count toward recurring detection.
    Otherwise QB-side deletions silently linger as ghost suggested streams."""
    from datetime import datetime
    for i in range(6):
        approx_date = date(2025, 1, 9) + timedelta(days=i * 30)
        db.add(QBBankTransaction(
            qb_txn_id=f"deleted-{i}",
            payee="Ghost Vendor",
            amount=99.99,
            txn_date=approx_date,
            deleted_at=datetime(2025, 6, 1),
        ))
    db.commit()
    cands = find_candidates(db, today=date(2026, 1, 15))
    assert cands == []


def test_multiple_overlapping_streams_does_not_crash(db):
    """User can manually create overlapping streams. find/upsert must pick
    one deterministically (most recently created) rather than crashing
    with MultipleResultsFound."""
    from gdx_dispatch.modules.forecasting.observed_recurring import StreamCandidate

    # Two overlapping manual streams
    db.add(RecurringStream(
        label="Phone primary",
        source=STREAM_SOURCE_MANUAL,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="PHONE.COM",
        amount_min=40.00, amount_max=50.00,
        cadence=CADENCE_MONTHLY,
    ))
    db.add(RecurringStream(
        label="Phone secondary",
        source=STREAM_SOURCE_MANUAL,
        status=STREAM_STATUS_ACTIVE,
        payee_pattern="PHONE.COM",
        amount_min=42.00, amount_max=48.00,  # overlaps the first
        cadence=CADENCE_MONTHLY,
    ))
    db.commit()
    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=6)
    # Should not raise — picks one and updates it.
    stats = run_detector(db, today=date(2026, 1, 15))
    assert stats["updated"] == 1
    assert db.query(RecurringStream).count() == 2  # neither was deleted


def test_no_double_count_on_duplicate_candidate(db):
    """If find_candidates returns the same txn twice (shouldn't, but defensive),
    upsert_streams must not violate the unique constraint."""
    from gdx_dispatch.modules.forecasting.observed_recurring import StreamCandidate

    _seed_monthly_stream(db, "Phone.com", 44.94, date(2025, 1, 1), count=4)
    cands = find_candidates(db, today=date(2026, 1, 15))
    # Synthesize a duplicate
    dupe = StreamCandidate(
        payee_norm=cands[0].payee_norm,
        median_amount=cands[0].median_amount,
        amount_min=cands[0].amount_min,
        amount_max=cands[0].amount_max,
        cadence=cands[0].cadence,
        occurrences=cands[0].occurrences,
        first_seen=cands[0].first_seen,
        last_seen=cands[0].last_seen,
        next_expected=cands[0].next_expected,
        txn_ids=cands[0].txn_ids,
    )
    stats = upsert_streams(db, [cands[0], dupe])
    # First candidate inserts the stream + 4 hits.
    # Second candidate matches the existing stream → update, no new hits.
    assert stats["inserted"] == 1
    assert stats["updated"] == 1
    assert stats["hits_added"] == 4
    assert db.query(RecurringStreamHit).count() == 4
