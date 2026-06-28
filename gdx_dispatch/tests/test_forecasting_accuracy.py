"""Tests for the forecast measurement loop (Stage A).

Covers capture → reconcile → summary, the per-invoice collection cap, and
out-of-window exclusion — across ALL FOUR aging buckets (the first design's
single 0-30 test hid the horizon defect the audit caught).
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Payment
from gdx_dispatch.modules.forecasting import accuracy, calibration
from gdx_dispatch.modules.forecasting.models import (
    SNAPSHOT_STATUS_PENDING,
    SNAPSHOT_STATUS_RECONCILED,
    ForecastSnapshot,
)

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    yield db
    db.close()
    engine.dispose()


def _seed_invoice(db, *, balance_due: float, due_date: date, status: str = "sent") -> Invoice:
    cid = uuid4()
    db.add(Customer(id=cid, name="Acme", company_id="t"))
    inv = Invoice(
        id=uuid4(),
        customer_id=cid,
        company_id="t",
        invoice_number=f"INV-{uuid4().hex[:6]}",
        public_token=uuid4().hex,
        subtotal=balance_due,
        total=balance_due,
        balance_due=balance_due,
        status=status,
        due_date=due_date,
    )
    db.add(inv)
    db.commit()
    return inv


def _pay(db, invoice_id, *, amount: float, payment_date: date) -> None:
    db.add(
        Payment(
            id=uuid4(),
            invoice_id=invoice_id,
            company_id="t",
            amount=amount,
            method="cash",
            payment_date=payment_date,
        )
    )
    db.commit()


def _bucket_invoices(db):
    """One $1000 invoice in each aging bucket, relative to AS_OF."""
    return {
        "0_30": _seed_invoice(db, balance_due=1000, due_date=AS_OF - timedelta(days=10)),
        "31_60": _seed_invoice(db, balance_due=1000, due_date=AS_OF - timedelta(days=45)),
        "61_90": _seed_invoice(db, balance_due=1000, due_date=AS_OF - timedelta(days=75)),
        "90_plus": _seed_invoice(db, balance_due=1000, due_date=AS_OF - timedelta(days=120)),
    }


def test_capture_buckets_population_with_frozen_face(session):
    _bucket_invoices(session)
    snap = accuracy.capture_snapshot(session, window_days=30, today=AS_OF)

    assert snap.status == SNAPSHOT_STATUS_PENDING
    assert snap.horizon_end == AS_OF + timedelta(days=30)
    assert float(snap.open_ar_face) == pytest.approx(4000.0)
    assert len(snap.invoices) == 4
    # Each invoice landed in the right bucket, face frozen.
    by_bucket = {r.bucket: r for r in snap.invoices}
    assert set(by_bucket) == {"0_30", "31_60", "61_90", "90_plus"}
    assert all(float(r.face_at_snapshot) == 1000.0 for r in snap.invoices)
    # Assumed (lifetime) rates captured for the calibration comparison.
    assert snap.assumed_rates == {"0_30": 0.95, "31_60": 0.80, "61_90": 0.60, "90_plus": 0.30}


def test_reconcile_measures_observed_window_rate_per_bucket(session):
    inv = _bucket_invoices(session)
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)

    # Collect a different fraction in each bucket, all inside the window.
    _pay(session, inv["0_30"].id, amount=900, payment_date=AS_OF + timedelta(days=5))    # 0.90
    _pay(session, inv["31_60"].id, amount=500, payment_date=AS_OF + timedelta(days=10))  # 0.50
    _pay(session, inv["61_90"].id, amount=200, payment_date=AS_OF + timedelta(days=20))  # 0.20
    _pay(session, inv["90_plus"].id, amount=50, payment_date=AS_OF + timedelta(days=29)) # 0.05

    done = accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    assert len(done) == 1
    res = done[0].bucket_results

    assert done[0].status == SNAPSHOT_STATUS_RECONCILED
    assert res["0_30"]["observed_window_rate"] == pytest.approx(0.90)
    assert res["31_60"]["observed_window_rate"] == pytest.approx(0.50)
    assert res["61_90"]["observed_window_rate"] == pytest.approx(0.20)
    assert res["90_plus"]["observed_window_rate"] == pytest.approx(0.05)
    # lifetime rate carried alongside for reference, NOT differenced
    assert res["90_plus"]["assumed_lifetime_rate"] == pytest.approx(0.30)
    assert "calibration_gap" not in res["90_plus"]


def test_collection_is_capped_at_frozen_face(session):
    inv = _seed_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=10))
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    # Overpayment: 1500 against a 1000 face — realization must cap at 100%.
    _pay(session, inv.id, amount=1500, payment_date=AS_OF + timedelta(days=5))

    done = accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    res = done[0].bucket_results["0_30"]
    assert res["collected_in_window"] == pytest.approx(1000.0)  # capped, not 1500
    assert res["observed_window_rate"] == pytest.approx(1.0)


def test_refund_does_not_produce_negative_rate(session):
    # A payment then a larger refund (negative amount) nets the window below
    # zero. observed_window_rate must floor at 0, never go negative (a negative
    # calibrated rate would poison Stage B).
    inv = _seed_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=10))
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    _pay(session, inv.id, amount=300, payment_date=AS_OF + timedelta(days=3))
    _pay(session, inv.id, amount=-500, payment_date=AS_OF + timedelta(days=8))  # refund/credit

    done = accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    res = done[0].bucket_results["0_30"]
    assert res["collected_in_window"] == pytest.approx(0.0)  # clamped, not -200
    assert res["observed_window_rate"] == pytest.approx(0.0)


def test_payment_outside_window_excluded(session):
    inv = _seed_invoice(session, balance_due=500, due_date=AS_OF - timedelta(days=10))
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    _pay(session, inv.id, amount=500, payment_date=AS_OF + timedelta(days=35))  # after horizon

    done = accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    res = done[0].bucket_results["0_30"]
    assert res["collected_in_window"] == pytest.approx(0.0)
    assert res["observed_window_rate"] == pytest.approx(0.0)


def test_payment_on_invoice_not_open_at_snapshot_excluded(session):
    inv = _bucket_invoices(session)
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    # A brand-new invoice created after the snapshot, paid in-window — must NOT
    # be attributed to this snapshot's measurement.
    later = _seed_invoice(session, balance_due=800, due_date=AS_OF + timedelta(days=2))
    _pay(session, later.id, amount=800, payment_date=AS_OF + timedelta(days=5))

    done = accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    # All four original buckets collected nothing; the stray payment is ignored.
    for b in ("0_30", "31_60", "61_90", "90_plus"):
        assert done[0].bucket_results[b]["collected_in_window"] == pytest.approx(0.0)


def test_does_not_reconcile_before_window_closes(session):
    _bucket_invoices(session)
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    # Horizon is AS_OF+30; today is only +15.
    assert accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=15)) == []


def test_accuracy_summary_calibration_table(session):
    inv = _bucket_invoices(session)
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    _pay(session, inv["0_30"].id, amount=900, payment_date=AS_OF + timedelta(days=5))
    _pay(session, inv["90_plus"].id, amount=50, payment_date=AS_OF + timedelta(days=5))
    accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))

    summary = accuracy.accuracy_summary(session)
    assert summary["sample_size"] == 1
    assert summary["pending"] == 0
    assert summary["window_days"] == 30

    b0 = summary["buckets"]["0_30"]
    assert b0["observed_window_rate"] == pytest.approx(0.90)
    assert b0["assumed_lifetime_rate"] == pytest.approx(0.95)
    # No gap field — observed and assumed are different horizons, not differenced.
    assert "calibration_gap" not in b0

    # The aged bucket: observed within-window 0.05 next to lifetime 0.30. Both
    # reported, neither subtracted. The divergence is signal for Stage B.
    b90 = summary["buckets"]["90_plus"]
    assert b90["observed_window_rate"] == pytest.approx(0.05)
    assert b90["assumed_lifetime_rate"] == pytest.approx(0.30)


def test_summary_face_weights_across_snapshots(session):
    # Two reconciled snapshots, same bucket, different faces and collection.
    # Aggregate observed rate must be FACE-WEIGHTED (total collected / total
    # face), not a naive mean of the two per-snapshot rates.
    inv1 = _seed_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=5))
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    _pay(session, inv1.id, amount=1000, payment_date=AS_OF + timedelta(days=2))  # rate 1.0 on face 1000
    accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))
    # inv1 is now settled — take it out of the open population so it doesn't
    # re-enter the second snapshot in a different (aged) bucket.
    inv1.status = "paid"
    inv1.balance_due = 0
    session.commit()

    later = AS_OF + timedelta(days=60)
    inv2 = _seed_invoice(session, balance_due=3000, due_date=later - timedelta(days=5))
    accuracy.capture_snapshot(session, window_days=30, today=later)
    _pay(session, inv2.id, amount=0, payment_date=later + timedelta(days=2))     # rate 0.0 on face 3000
    accuracy.reconcile_due_snapshots(session, today=later + timedelta(days=31))

    summary = accuracy.accuracy_summary(session)
    assert summary["sample_size"] == 2
    b0 = summary["buckets"]["0_30"]
    # face-weighted: (1000 + 0) / (1000 + 3000) = 0.25, NOT mean(1.0, 0.0)=0.5
    assert b0["face"] == pytest.approx(4000.0)
    assert b0["observed_window_rate"] == pytest.approx(0.25)


def test_summary_empty_when_nothing_reconciled(session):
    summary = accuracy.accuracy_summary(session)
    assert summary["sample_size"] == 0
    assert summary["window_days"] is None
    assert summary["buckets"]["0_30"]["observed_window_rate"] is None
    assert summary["buckets"]["0_30"]["assumed_lifetime_rate"] is None


def test_end_to_end_real_capture_reconcile_feeds_calibration(session):
    # The REAL chain (not hand-built bucket_results): capture freezes the
    # population, an actual payment lands in the window, reconcile measures it,
    # and calibration reads the result back out.
    inv = _seed_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=10))
    accuracy.capture_snapshot(session, window_days=30, today=AS_OF)
    _pay(session, inv.id, amount=400, payment_date=AS_OF + timedelta(days=7))
    accuracy.reconcile_due_snapshots(session, today=AS_OF + timedelta(days=31))

    # min_snapshots=1 so one real snapshot is enough to assert the chain.
    rates = calibration.calibrated_window_rates(session, 30, AS_OF + timedelta(days=31), min_snapshots=1)
    info = rates["0_30"]
    assert info["calibrated"] is True
    assert info["rate"] == pytest.approx(0.40)  # 400 collected / 1000 face, measured end-to-end


def test_prune_removes_old_reconciled_only(session):
    # Old reconciled snapshot → pruned; recent reconciled → kept; pending →
    # never pruned however old.
    old = _reconciled_snap(session, as_of=AS_OF - timedelta(days=200), status=SNAPSHOT_STATUS_RECONCILED)
    recent = _reconciled_snap(session, as_of=AS_OF - timedelta(days=10), status=SNAPSHOT_STATUS_RECONCILED)
    old_pending = _reconciled_snap(session, as_of=AS_OF - timedelta(days=300), status=SNAPSHOT_STATUS_PENDING)

    pruned = accuracy.prune_reconciled_snapshots(session, today=AS_OF, retention_days=180)
    assert pruned == 1
    remaining = {s.id for s in session.query(ForecastSnapshot).all()}
    assert old.id not in remaining
    assert recent.id in remaining
    assert old_pending.id in remaining  # pending is never pruned


def _reconciled_snap(db, *, as_of, status):
    snap = ForecastSnapshot(
        as_of=as_of, window_days=30, horizon_end=as_of + timedelta(days=30),
        open_ar_face=0, status=status, bucket_results={},
    )
    db.add(snap)
    db.commit()
    return snap
