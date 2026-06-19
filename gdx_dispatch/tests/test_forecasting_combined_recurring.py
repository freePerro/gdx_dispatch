"""Tests for the slice-5 forecasting service integration.

Covers:
- _observed_stream_projection walks active streams forward by cadence step.
- Term gates: stops at term_end_date; stops once occurrences_seen reaches
  term_total_occurrences.
- Excludes suggested/ended/soft-deleted streams.
- _combined_recurring dedups a QBO template against an observed stream
  (same payee + same calendar month) so cash flow isn't double counted.
- include_recurring=False produces an empty merged shape with the new
  `sources` envelope (so the FE never sees an undefined .sources key).
- Full revenue_projection still adds AR + scheduled + recurring totals.
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_MONTHLY,
    STREAM_SOURCE_MANUAL,
    STREAM_SOURCE_OBSERVED,
    STREAM_STATUS_ACTIVE,
    STREAM_STATUS_SUGGESTED,
    QBRecurringTransaction,
    RecurringStream,
)
from gdx_dispatch.modules.forecasting.service import (
    _combined_recurring,
    _observed_stream_projection,
    revenue_projection,
)


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


# ─── observed projection ────────────────────────────────────────────────────

def test_monthly_stream_projects_through_window(db):
    """30-day window from a stream with next_expected_date in 5 days → 1 hit."""
    db.add(RecurringStream(
        label="Phone.com",
        source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="PHONE.COM",
        amount_min=43.00, amount_max=46.00,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 5, 25),
        last_observed_date=date(2026, 4, 25),
        occurrences_seen=12,
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=30)
    assert out["count"] == 1
    assert out["items"][0]["next_date"] == "2026-05-25"
    assert out["items"][0]["amount"] == pytest.approx((43.00 + 46.00) / 2)


def test_monthly_stream_projects_multiple_hits_in_90_day_window(db):
    db.add(RecurringStream(
        label="Phone.com",
        source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="PHONE.COM",
        amount_min=43.00, amount_max=46.00,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 5, 25),
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=90)
    # 5/25, 6/24, 7/24 — 3 hits in 90-day window.
    assert out["count"] == 3


def test_term_end_date_stops_projection(db):
    db.add(RecurringStream(
        label="Insurance with end date",
        source=STREAM_SOURCE_MANUAL, status=STREAM_STATUS_ACTIVE,
        payee_pattern="X",
        amount_min=100, amount_max=100,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
        term_end_date=date(2026, 7, 1),  # only July hit, June hit is past it? no — June 1 fits, July 1 fits, Aug 1 cut
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=180)
    # June 1 included; July 1 included; Aug 1 > term_end → stop. So 2 items.
    assert out["count"] == 2


def test_term_total_occurrences_caps_projection(db):
    """A 36-payment loan with 33 already seen has only 3 left, regardless of cadence."""
    db.add(RecurringStream(
        label="Loan",
        source=STREAM_SOURCE_MANUAL, status=STREAM_STATUS_ACTIVE,
        payee_pattern="LOAN",
        amount_min=376.56, amount_max=376.56,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
        term_total_occurrences=36,
        occurrences_seen=33,
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=365)
    assert out["count"] == 3


def test_suggested_and_ended_streams_excluded(db):
    db.add(RecurringStream(
        label="Suggested",
        source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_SUGGESTED,
        payee_pattern="X", amount_min=1, amount_max=2, cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
    ))
    db.add(RecurringStream(
        label="Paid off",
        source=STREAM_SOURCE_MANUAL, status="paid_off",
        payee_pattern="Y", amount_min=1, amount_max=2, cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
        ended_at=date(2026, 5, 1), ended_reason="paid_off",
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=60)
    assert out["count"] == 0


def test_soft_deleted_stream_excluded(db):
    from datetime import UTC, datetime
    s = RecurringStream(
        label="Dismissed",
        source=STREAM_SOURCE_MANUAL, status=STREAM_STATUS_ACTIVE,
        payee_pattern="X", amount_min=1, amount_max=2, cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
    )
    db.add(s); db.commit()
    s.deleted_at = datetime.now(UTC)
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=60)
    assert out["count"] == 0


# ─── dedup ──────────────────────────────────────────────────────────────────

def test_combined_dedups_when_user_typed_payee_into_qbo_name(db):
    """Realistic dedup case: user named the QBO template after the merchant.
    Substring match (observed.payee_pattern IN qbo.name) catches this."""
    observed = {
        "count": 1, "expected_total": 44.94,
        "items": [{
            "stream_id": "s1", "label": "Phone.com", "payee_pattern": "PHONE.COM",
            "source": "observed", "cadence": "monthly",
            "amount": 44.94, "next_date": "2026-06-09",
        }],
    }
    qbo = {
        "count": 1, "expected_total": 44.94,
        "items": [{
            "qb_id": "qbo-1", "name": "Phone.com monthly service",
            "amount": 44.94, "next_date": "2026-06-15",
        }],
    }
    merged = _combined_recurring(qbo, observed)
    assert merged["count"] == 1
    assert merged["qbo_overridden"] == 1


def test_combined_does_not_dedup_amount_too_far_apart(db):
    """Even matching name, amount must be within ±15% to dedup. A $44 stream
    and a $99 template are different cash flows."""
    observed = {"count": 1, "expected_total": 44.94, "items": [{
        "stream_id": "s1", "label": "Phone.com", "payee_pattern": "PHONE.COM",
        "source": "observed", "cadence": "monthly", "amount": 44.94, "next_date": "2026-06-09",
    }]}
    qbo = {"count": 1, "expected_total": 99.00, "items": [{
        "qb_id": "q1", "name": "PHONE.COM business plan",
        "amount": 99.00, "next_date": "2026-06-15",
    }]}
    merged = _combined_recurring(qbo, observed)
    assert merged["count"] == 2
    assert merged["qbo_overridden"] == 0


def test_combined_does_not_dedup_when_qbo_name_unrelated(db):
    """AUDITOR-FLAGGED REALITY: observed.payee_pattern='PHONE.COM' vs
    QBO.name='Monthly Comm Svc' will NOT dedup. Both contribute. This is
    intentional — false dedup hides cash flows. qbo_overridden=0 signals
    to the UI that nothing was merged so user can manually reconcile."""
    observed = {"count": 1, "expected_total": 44.94, "items": [{
        "stream_id": "s1", "label": "Phone.com", "payee_pattern": "PHONE.COM",
        "source": "observed", "cadence": "monthly", "amount": 44.00, "next_date": "2026-06-09",
    }]}
    qbo = {"count": 1, "expected_total": 44.00, "items": [{
        "qb_id": "q1", "name": "Monthly Comm Svc",
        "amount": 44.00, "next_date": "2026-06-15",
    }]}
    merged = _combined_recurring(qbo, observed)
    assert merged["count"] == 2
    assert merged["qbo_overridden"] == 0


def test_combined_keeps_disjoint_qbo_and_observed(db):
    """Non-overlapping payees: both contribute."""
    observed = {"count": 1, "expected_total": 100, "items": [
        {"stream_id": "s1", "label": "L", "payee_pattern": "PHONE.COM",
         "source": "observed", "cadence": "monthly", "amount": 100, "next_date": "2026-06-09"},
    ]}
    qbo = {"count": 1, "expected_total": 200, "items": [
        {"qb_id": "q1", "name": "SOME OTHER VENDOR", "amount": 200, "next_date": "2026-06-15"},
    ]}
    merged = _combined_recurring(qbo, observed)
    assert merged["count"] == 2
    assert merged["qbo_overridden"] == 0
    assert merged["expected_total"] == 300


def test_calendar_month_cadence_does_not_drift(db):
    """Monthly stream anchored on 25th must land on 25th each month, not
    drift to 24th → 23rd as timedelta(days=30) would."""
    db.add(RecurringStream(
        label="Anchor 25", source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="X", amount_min=10, amount_max=10,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 5, 25),
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=125)
    dates = [it["next_date"] for it in out["items"]]
    assert "2026-05-25" in dates
    assert "2026-06-25" in dates
    assert "2026-07-25" in dates
    assert "2026-08-25" in dates


def test_annual_cadence_lands_on_anniversary(db):
    """Annual stream anchored 2026-05-25 → next 2027-05-25 (not 2027-05-20
    that timedelta(days=365) would give in a leap year)."""
    db.add(RecurringStream(
        label="Annual", source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="X", amount_min=100, amount_max=100,
        cadence="annual",
        next_expected_date=date(2026, 5, 25),
    ))
    db.commit()
    out = _observed_stream_projection(db, today=date(2026, 5, 20), window_days=2 * 365)
    dates = [it["next_date"] for it in out["items"]]
    assert "2026-05-25" in dates
    assert "2027-05-25" in dates


# ─── full revenue_projection ────────────────────────────────────────────────

def test_revenue_projection_envelope_has_new_sources_shape(db):
    out = revenue_projection(db, window_days=30, today=date(2026, 5, 20))
    assert "recurring" in out
    assert "sources" in out["recurring"]
    assert "qbo_templates" in out["recurring"]["sources"]
    assert "observed" in out["recurring"]["sources"]
    # No data → all counts zero
    assert out["recurring"]["count"] == 0
    assert out["recurring"]["expected_total"] == 0


def test_revenue_projection_respects_include_recurring_false(db):
    """When include_recurring is OFF, recurring contributes 0 + emits empty
    envelope so the frontend never reads an undefined .sources."""
    from gdx_dispatch.modules.forecasting.service import get_or_create_settings
    s = get_or_create_settings(db)
    s.include_recurring = False
    db.commit()
    # Seed a stream that WOULD project if it were on
    db.add(RecurringStream(
        label="Phone", source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="P", amount_min=10, amount_max=10, cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
    ))
    db.commit()
    out = revenue_projection(db, window_days=60, today=date(2026, 5, 20))
    assert out["recurring"]["count"] == 0
    assert out["recurring"]["sources"]["observed"]["count"] == 0


def test_revenue_projection_sums_observed_into_total(db):
    db.add(RecurringStream(
        label="Phone", source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern="P", amount_min=40, amount_max=50,
        cadence=CADENCE_MONTHLY,
        next_expected_date=date(2026, 6, 1),
    ))
    db.commit()
    out = revenue_projection(db, window_days=30, today=date(2026, 5, 20))
    assert out["recurring"]["count"] == 1
    # median of 40-50 = 45
    assert out["recurring"]["expected_total"] == pytest.approx(45.0)
    # AR + scheduled both empty → grand total equals recurring
    assert out["expected_total"] == pytest.approx(45.0)
