"""Tests for Stage B — window-rate calibration and its use in the forecast.

Covers the cold-start threshold, face-weighted aggregation, window matching,
the AR projection switching from prior to calibrated rate, and the Celery
beat/task wiring.
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice
from gdx_dispatch.modules.forecasting import calibration, service
from gdx_dispatch.modules.forecasting.models import (
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


def _reconciled(db, *, window_days: int, bucket_results: dict, as_of: date = AS_OF) -> ForecastSnapshot:
    snap = ForecastSnapshot(
        as_of=as_of,
        window_days=window_days,
        horizon_end=as_of + timedelta(days=window_days),
        open_ar_face=sum(r.get("face", 0) for r in bucket_results.values()),
        status=SNAPSHOT_STATUS_RECONCILED,
        bucket_results=bucket_results,
    )
    db.add(snap)
    db.commit()
    return snap


def _br(face, collected):
    return {"face": face, "collected_in_window": collected}


def _seed_open_invoice(db, *, balance_due, due_date):
    cid = uuid4()
    db.add(Customer(id=cid, name="Acme", company_id="t"))
    db.add(Invoice(
        id=uuid4(), customer_id=cid, company_id="t",
        invoice_number=f"INV-{uuid4().hex[:6]}", public_token=uuid4().hex,
        subtotal=balance_due, total=balance_due, balance_due=balance_due,
        status="sent", due_date=due_date,
    ))
    db.commit()


def test_below_threshold_not_calibrated(session):
    # Only 2 reconciled snapshots (< default min of 3) → not calibrated.
    for _ in range(2):
        _reconciled(session, window_days=30, bucket_results={"0_30": _br(1000, 400)})
    rates = calibration.calibrated_window_rates(session, 30, AS_OF)
    assert rates["0_30"]["sample_size"] == 2
    assert rates["0_30"]["rate"] == pytest.approx(0.40)  # rate still computed
    assert rates["0_30"]["calibrated"] is False           # but not trusted yet


def test_at_threshold_is_face_weighted(session):
    # 3 snapshots, different faces → face-weighted, not a naive mean of rates.
    _reconciled(session, window_days=30, bucket_results={"0_30": _br(1000, 1000)})  # 1.0
    _reconciled(session, window_days=30, bucket_results={"0_30": _br(3000, 0)})     # 0.0
    _reconciled(session, window_days=30, bucket_results={"0_30": _br(1000, 200)})   # 0.2
    rates = calibration.calibrated_window_rates(session, 30, AS_OF)
    info = rates["0_30"]
    assert info["sample_size"] == 3
    assert info["calibrated"] is True
    # (1000 + 0 + 200) / (1000 + 3000 + 1000) = 1200/5000 = 0.24
    assert info["rate"] == pytest.approx(0.24)


def test_window_mismatch_ignored(session):
    # Snapshots at window=60 must not calibrate the 30-day forecast.
    for _ in range(3):
        _reconciled(session, window_days=60, bucket_results={"0_30": _br(1000, 500)})
    rates = calibration.calibrated_window_rates(session, 30, AS_OF)
    assert rates["0_30"]["sample_size"] == 0
    assert rates["0_30"]["calibrated"] is False
    assert rates["0_30"]["rate"] is None


def test_ar_projection_uses_prior_when_uncalibrated(session):
    # No snapshots → AR uses the configured rate (default 0.95 for 0-30).
    _seed_open_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=10))
    proj = service.revenue_projection(session, window_days=30, today=AS_OF)
    ar = proj["open_ar"]
    assert ar["uses_calibration"] is False
    b = ar["by_bucket"]["0_30"]
    assert b["rate_source"] == "configured"
    assert b["expected_total"] == pytest.approx(950.0)  # 1000 * 0.95


def test_ar_projection_uses_calibrated_rate_when_available(session):
    # 3 reconciled 30-day snapshots establish a 0-30 window rate of 0.40,
    # which the forecast must use INSTEAD of the 0.95 prior.
    for _ in range(3):
        _reconciled(session, window_days=30, bucket_results={"0_30": _br(1000, 400)})
    _seed_open_invoice(session, balance_due=1000, due_date=AS_OF - timedelta(days=10))

    proj = service.revenue_projection(session, window_days=30, today=AS_OF)
    ar = proj["open_ar"]
    b = ar["by_bucket"]["0_30"]
    assert ar["uses_calibration"] is True
    assert b["rate_source"] == "calibrated"
    assert b["rate_used"] == pytest.approx(0.40)
    assert b["expected_total"] == pytest.approx(400.0)  # 1000 * 0.40, not 950


def test_calibration_status_reports_source(session):
    for _ in range(3):
        _reconciled(session, window_days=30, bucket_results={"61_90": _br(2000, 200)})
    settings = service.get_or_create_settings(session)
    status = calibration.calibration_status(session, settings, 30, AS_OF)

    assert status["any_calibrated"] is True
    b = status["buckets"]["61_90"]
    assert b["calibrated"] is True
    assert b["rate"] == pytest.approx(0.10)            # 200/2000
    assert b["configured_rate"] == pytest.approx(0.60)  # the prior, shown for reference
    assert b["rate_in_use"] == pytest.approx(0.10)
    assert b["source_in_use"] == "calibrated"
    # An untouched bucket stays on its prior.
    assert status["buckets"]["0_30"]["source_in_use"] == "configured"


def test_snapshots_outside_lookback_ignored(session):
    # 3 reconciled snapshots, but all older than the lookback window → not used,
    # so a stale collection regime can't keep calibrating the forecast forever.
    old = AS_OF - timedelta(days=calibration.CALIBRATION_LOOKBACK_DAYS + 5)
    for _ in range(3):
        _reconciled(session, window_days=30, bucket_results={"0_30": _br(1000, 400)}, as_of=old)
    rates = calibration.calibrated_window_rates(session, 30, AS_OF)
    assert rates["0_30"]["sample_size"] == 0
    assert rates["0_30"]["calibrated"] is False


def test_celery_measurement_task_and_beat_are_wired():
    from gdx_dispatch.core.celery_app import celery_app
    from gdx_dispatch.core.scheduler import build_beat_schedule

    # Tasks registered on the app.
    assert "gdx_dispatch.modules.forecasting.tasks.advance_forecast_measurement_dispatcher" in celery_app.tasks
    assert "gdx_dispatch.modules.forecasting.tasks.advance_forecast_measurement_task" in celery_app.tasks
    # Beat entry present and pointing at the dispatcher.
    sched = build_beat_schedule()
    entry = sched["forecasting-measurement-tick-daily"]
    assert entry["task"] == "gdx_dispatch.modules.forecasting.tasks.advance_forecast_measurement_dispatcher"
    assert entry["options"]["queue"] == "priority:low"
