"""GDXA-506 — the books' "today" is the shop's calendar day, not UTC's.

The clock is frozen at 03:00 UTC on 2026-11-01, which is 22:00 on
2026-10-31 in America/Chicago: past UTC midnight AND past the month end,
before shop midnight. Every test here fails on the old code, which took the
UTC date and so moved the shop's Halloween evening into November — a paid
stamp, a payment date, a budget month, a forecast snapshot, a stream's end.

Route functions are called directly rather than through TestClient: freezegun
swaps ``date`` inside the router modules, and FastAPI then cannot analyse a
``date``-typed parameter.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.quickbooks import QBEntityMap
from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Invoice,
    Job,
    Payment,
    QBPnlMonthly,
)
from gdx_dispatch.modules.forecasting import accuracy
from gdx_dispatch.modules.forecasting import router as forecasting_router
from gdx_dispatch.modules.forecasting import service as forecast_service
from gdx_dispatch.modules.forecasting.models import (
    SNAPSHOT_STATUS_PENDING,
    SNAPSHOT_STATUS_RECONCILED,
    ForecastSnapshot,
    RecurringStream,
)
from gdx_dispatch.modules.forecasting.observed_recurring import find_candidates
from gdx_dispatch.modules.quickbooks import sync
from gdx_dispatch.modules.quickbooks.banking import QBBankTransaction
from gdx_dispatch.routers import budgets
from gdx_dispatch.tests.conftest import make_fresh_db
from gdx_dispatch.tests.test_qb_full_sync import (  # noqa: F401 — pytest fixtures
    _seed_customer,
    _seed_invoice,
    db_session,
    mock_qb,
    qb_connection,
)

SHOP_TZ = "America/Chicago"
FROZEN_NOW = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)  # 2026-10-31 22:00 CDT
SHOP_TODAY = date(2026, 10, 31)


@pytest.fixture
def shop_evening():
    """Pin the clock to the shop's Halloween evening (not ticking)."""
    import freezegun

    # As in conftest.shop_midday_clock: keep pytest's own timers real.
    freezegun.configure(extend_ignore_list=["_pytest"])
    with freeze_time(FROZEN_NOW):
        yield


@pytest.fixture
def db(shop_evening):
    engine = make_fresh_db()
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    s.add(AppSettings(timezone=SHOP_TZ))
    s.commit()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def qb_db(shop_evening, db_session, qb_connection):  # noqa: F811 — fixture names
    """The QB-sync fixtures from test_qb_full_sync, with the shop's zone."""
    db_session.add(AppSettings(timezone=SHOP_TZ))
    db_session.commit()
    return db_session


def _day(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


# ── QuickBooks pull ──────────────────────────────────────────────────────


def test_pull_invoices_mapped_update_stamps_paid_on_the_shop_day(qb_db, mock_qb):  # noqa: F811
    """A mapped invoice QB reports paid, with no TxnDate and no local
    invoice_date, is stamped paid on the shop's day — not on UTC's 1 November."""
    inv = _seed_invoice(qb_db, _seed_customer(qb_db))
    assert inv.invoice_date is None and inv.paid_at is None
    qb_db.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice",
                          local_id=str(inv.id), qb_id="QB-I-PAID"))
    qb_db.commit()
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-I-PAID", "DocNumber": "INV-100", "TotalAmt": 110, "Balance": 0},
    ])

    out = asyncio.run(sync.pull_invoices("tenant-1", qb_db, mock_qb))

    assert out["updated"] == 1
    fresh = qb_db.get(Invoice, inv.id)
    assert fresh.status == "paid"
    assert _day(fresh.paid_at) == SHOP_TODAY


def test_pull_invoices_adoption_stamps_paid_on_the_shop_day(qb_db, mock_qb):  # noqa: F811
    """The adoption path (local invoice with QB's DocNumber, unmapped) uses the
    same shop-day stamp when QB gives no TxnDate."""
    cust = _seed_customer(qb_db, "Adopted Customer")
    job = Job(customer_id=cust.id, title="Legacy", company_id="tenant-1")
    qb_db.add(job)
    qb_db.flush()
    local = Invoice(
        customer_id=uuid4(), job_id=job.id, invoice_number="777001",
        subtotal=100, tax_amount=0, total=100, balance_due=100, status="sent",
        public_token="pub-777001", company_id="tenant-1",
    )
    qb_db.add(local)
    qb_db.commit()
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-I-777001", "DocNumber": "777001", "TotalAmt": 100, "Balance": 0},
    ])

    out = asyncio.run(sync.pull_invoices("tenant-1", qb_db, mock_qb))

    assert out["adopted"] == 1
    fresh = qb_db.get(Invoice, local.id)
    assert fresh.status == "paid"
    assert _day(fresh.paid_at) == SHOP_TODAY


def test_pull_payments_without_txn_date_is_dated_the_shop_day(qb_db, mock_qb):  # noqa: F811
    inv = _seed_invoice(qb_db, _seed_customer(qb_db))
    qb_db.add(QBEntityMap(tenant_id="tenant-1", entity_type="invoice",
                          local_id=str(inv.id), qb_id="QB-I-10"))
    qb_db.commit()
    mock_qb.query = AsyncMock(return_value=[
        {"Id": "QB-P-NODATE", "TotalAmt": 55,
         "Line": [{"LinkedTxn": [{"TxnType": "Invoice", "TxnId": "QB-I-10"}]}]},
    ])

    out = asyncio.run(sync.pull_payments("tenant-1", qb_db, mock_qb))

    assert out["created"] == 1
    pay = qb_db.execute(select(Payment).where(Payment.invoice_id == inv.id)).scalar_one()
    assert pay.payment_date == SHOP_TODAY


# ── Monthly budget ───────────────────────────────────────────────────────


def _pnl(db, *, year: int, month: int, account: str, account_type: str,
         amount: str, name: str | None = None) -> None:
    db.add(QBPnlMonthly(year=year, month=month, qb_account_id=account,
                        account_name=name or account, account_type=account_type,
                        amount=Decimal(amount)))


def test_budget_revenue_basis_treats_october_as_the_current_month(db, monkeypatch):
    """At the shop's 22:00 on 31 October, October is the current month and is
    forecast; UTC's November made it a closed month read from the P&L cache."""
    _pnl(db, year=2026, month=10, account="inc-1", account_type="Income", amount="999.00")
    db.commit()
    monkeypatch.setattr(budgets, "revenue_projection",
                        lambda _db, window_days=None: {"expected_total": 1234})

    basis = budgets._revenue_basis_for_month(db, year=2026, month=10)

    assert basis == Decimal("1234")


def test_spending_trends_window_ends_at_the_shop_month(db, monkeypatch):
    """A three-month window on the shop's 31 October is Aug–Oct; UTC's
    November dropped August."""
    # SQLite hands MAX(synced_at) back as TEXT, which the freshness stamp
    # cannot isoformat; it is not what this test is about.
    monkeypatch.setattr(budgets, "_pnl_last_synced_at", lambda _db: None)
    _pnl(db, year=2026, month=8, account="exp-aug", account_type="Expense", amount="10.00")
    db.commit()

    body = budgets.spending_trends(months=3, account_kind="spending", account_type=None, db=db)

    assert body["as_of"] == SHOP_TODAY.isoformat()
    assert [a["qb_account_id"] for a in body["accounts"]] == ["exp-aug"]


def test_classify_accounts_lookback_excludes_the_open_shop_month(db):
    """The classifier reads closed months only. On the shop's 31 October,
    October is still open: the lookback is Sep, Aug, Jul — UTC's November
    pulled the open October in as history."""
    _pnl(db, year=2026, month=7, account="exp-jul", account_type="Expense",
         amount="100.00", name="Widgets")
    _pnl(db, year=2026, month=10, account="exp-oct", account_type="Expense",
         amount="50.00", name="Gadgets")
    db.commit()

    body = budgets.classify_accounts(lookback_months=3, db=db)

    assert [p["qb_account_id"] for p in body["proposals"]] == ["exp-jul"]


# ── Forecast accuracy loop ───────────────────────────────────────────────


def test_capture_snapshot_is_as_of_the_shop_day(db):
    snap = accuracy.capture_snapshot(db)
    assert snap.as_of == SHOP_TODAY


def _snapshot(db, *, as_of: date, window_days: int, status: str) -> ForecastSnapshot:
    snap = ForecastSnapshot(as_of=as_of, window_days=window_days,
                            horizon_end=as_of + timedelta(days=window_days),
                            open_ar_face=0, assumed_rates={}, status=status)
    db.add(snap)
    db.commit()
    return snap


def test_reconcile_waits_for_the_shop_day_to_close(db):
    """A window ending today (shop time) has not closed yet; on UTC's
    1 November it looked closed and was scored a day early."""
    snap = _snapshot(db, as_of=date(2026, 10, 1), window_days=30, status=SNAPSHOT_STATUS_PENDING)
    assert snap.horizon_end == SHOP_TODAY

    reconciled = accuracy.reconcile_due_snapshots(db)

    assert reconciled == []
    db.refresh(snap)
    assert snap.status == SNAPSHOT_STATUS_PENDING


def test_prune_keeps_a_snapshot_exactly_at_the_retention_edge(db):
    """Retention is 180 shop days: a snapshot as_of exactly 180 days before
    the shop's today is kept. UTC's tomorrow pruned it a day early."""
    snap = _snapshot(db, as_of=SHOP_TODAY - timedelta(days=180), window_days=30,
                     status=SNAPSHOT_STATUS_RECONCILED)
    snap_id = snap.id

    pruned = accuracy.prune_reconciled_snapshots(db)

    assert pruned == 0
    assert db.get(ForecastSnapshot, snap_id) is not None


# ── Revenue projection ───────────────────────────────────────────────────


def test_ar_reference_date_reads_sent_at_as_the_shop_day():
    """An invoice sent at 21:00 CDT on 31 October (02:00 UTC, 1 November)
    ages from 31 October."""
    inv = SimpleNamespace(due_date=None, sent_at=datetime(2026, 11, 1, 2, 0, tzinfo=UTC),
                          created_at=None)
    assert forecast_service._ar_reference_date(inv, SHOP_TZ, SHOP_TODAY) == SHOP_TODAY


def test_ar_reference_date_reads_a_qb_midnight_utc_sent_at_as_its_date():
    """QB sync stamps ``sent_at`` as TxnDate at 00:00 UTC — a plain date. It
    ages from that date, not the shop's previous evening."""
    inv = SimpleNamespace(due_date=None, sent_at=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
                          created_at=None)
    assert forecast_service._ar_reference_date(inv, SHOP_TZ, SHOP_TODAY) == date(2026, 10, 1)


def test_revenue_projection_defaults_to_the_shop_day(db, monkeypatch):
    seen: list[date] = []
    real = forecast_service._open_ar_projection

    def _capture(db_, settings, today, window_days):
        seen.append(today)
        return real(db_, settings, today, window_days)

    monkeypatch.setattr(forecast_service, "_open_ar_projection", _capture)

    body = forecast_service.revenue_projection(db)

    assert seen == [SHOP_TODAY]
    assert body["as_of"] == SHOP_TODAY.isoformat()


# ── Recurring streams ────────────────────────────────────────────────────


def test_detector_window_is_counted_from_the_shop_day(db):
    """The detector reaches back 24×31 days from the shop's today, to
    2024-10-17. A monthly charge whose first hit is on that cutoff has three
    occurrences; counted from UTC's 1 November it had two and was no stream."""
    for n, day in enumerate((date(2024, 10, 17), date(2024, 11, 17), date(2024, 12, 17))):
        db.add(QBBankTransaction(qb_txn_id=f"T-{n}", payee="North Star Insurance",
                                 amount=120.0, txn_date=day, txn_type="Cash"))
    db.commit()

    candidates = find_candidates(db)

    (cand,) = candidates
    assert cand.occurrences == 3
    assert cand.first_seen == date(2024, 10, 17)


def _request() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/not-a-route/direct-call",
                   "headers": [], "query_string": b""})
    req.state.tenant = {"id": "tenant-test"}
    return req


def test_end_stream_without_a_date_ends_on_the_shop_day(db, monkeypatch):
    audits: list[dict] = []
    monkeypatch.setattr(forecasting_router, "log_audit_event_sync",
                        lambda *_a, **kw: audits.append(kw))
    stream = RecurringStream(label="Phone.com", payee_pattern="PHONE.COM",
                             amount_min=40, amount_max=50)
    db.add(stream)
    db.commit()

    body = forecasting_router.end_recurring_stream(
        str(stream.id), _request(),
        forecasting_router.StreamEndPayload(reason="cancelled"),
        current_user={"sub": "user-1", "role": "admin", "tenant_id": "tenant-test"},
        db=db,
    )

    db.refresh(stream)
    assert stream.ended_at == SHOP_TODAY
    assert audits[0]["details"]["ended_at"] == SHOP_TODAY.isoformat()
    assert body["status"] == "cancelled"

