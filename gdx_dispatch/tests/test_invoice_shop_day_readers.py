"""The invoice "is it overdue / how old is it" checks #444 moved read the SHOP's today.

#444 moved invoice and due dates onto the shop's calendar (``AppSettings
.timezone``). A reader left on the UTC day then disagrees with those dates for
the last five or six hours of every Minnesota evening: a deposit due on
receipt reads overdue the moment it exists, and two aging reports that used to
agree split apart (/audit, 2026-09-13).

One frozen evening, three invoices, each reader #444 moved (not every date
reader in the app — the rest are counted on the maintainer's ledger):

    04:30 UTC on 14 Sep = 23:30 on the 13th in America/Chicago.

    TODAY  due 13 Sep  $100   shop: due today, not overdue   UTC: 1 day overdue
    NEXT   due 14 Sep  $7     shop: not yet due              UTC: due today
    LATE   due 14 Aug  $40    shop: 30 days (0-30 bucket)    UTC: 31 days (31-60)

Each assertion below holds on the shop day and fails on the UTC day.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.models.tenant_models import AppSettings, Customer, Invoice, ReminderSettings
from gdx_dispatch.tests.conftest import make_fresh_db

EVENING_UTC = "2026-09-14 04:30:00"
SHOP_DAY = date(2026, 9, 13)
TENANT = "tenant-444"


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    session.add(AppSettings(timezone="America/Chicago"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _invoice(db, customer, *, number: str, due: date, balance: str) -> Invoice:
    inv = Invoice(
        id=uuid4(),
        customer_id=customer.id,
        invoice_number=number,
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal(balance),
        tax_amount=Decimal("0"),
        total=Decimal(balance),
        balance_due=Decimal(balance),
        status="sent",
        invoice_date=due - timedelta(days=30),
        due_date=due,
        public_token=uuid4().hex,
        locked=False,
        company_id=TENANT,
    )
    db.add(inv)
    db.commit()
    return inv


@pytest.fixture
def invoices(db):
    customer = Customer(id=uuid4(), name="Shop Day", email="shopday@example.com", company_id=TENANT)
    db.add(customer)
    db.commit()
    return {
        "today": _invoice(db, customer, number="INV-444001", due=SHOP_DAY, balance="100.00"),
        "next": _invoice(db, customer, number="INV-444002", due=SHOP_DAY + timedelta(days=1), balance="7.00"),
        "late": _invoice(db, customer, number="INV-444003", due=SHOP_DAY - timedelta(days=30), balance="40.00"),
    }


def test_collections_aging_ages_on_the_shop_day(db, invoices):
    from gdx_dispatch.routers.collections import aging_report

    with freeze_time(EVENING_UTC):
        out = aging_report(_={}, db=db)

    assert out["as_of"] == SHOP_DAY.isoformat()
    assert out["total_outstanding"] == pytest.approx(140.0)  # NEXT is not yet due
    by_label = {b["label"]: b for b in out["buckets"]}
    assert by_label["Current (0-30)"]["count"] == 2
    assert by_label["31-60 Days"]["count"] == 0


def test_collections_list_counts_only_really_overdue_invoices(db, invoices):
    from gdx_dispatch.routers.collections import list_collections

    with freeze_time(EVENING_UTC):
        out = list_collections(_={}, db=db)

    rows = {r["invoice_number"]: r for r in out["items"]}
    assert set(rows) == {"INV-444003"}
    assert rows["INV-444003"]["days_overdue"] == 30


def test_cash_risk_aging_agrees_with_collections_aging(db, invoices):
    from gdx_dispatch.routers.reports import cash_risk_kpis

    with freeze_time(EVENING_UTC):
        out = cash_risk_kpis(_={}, db=db)

    aging = out["ar_aging"]
    assert aging["total_outstanding"] == pytest.approx(140.0)
    assert aging["buckets"]["current"]["count"] == 2
    assert aging["buckets"]["d31_60"]["count"] == 0


def test_report_summary_overdue_count_uses_the_shop_day(db, invoices):
    from gdx_dispatch.routers.reports import _summary_window

    with freeze_time(EVENING_UTC):
        out = _summary_window(db, "2026-08-01T00:00:00+00:00", "2026-09-14T23:59:59+00:00")

    assert out["overdue_invoices"] == 1  # LATE only


def test_billing_summary_overdue_total_uses_the_shop_day(db, invoices):
    from gdx_dispatch.routers.invoices import billing_summary

    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    with freeze_time(EVENING_UTC):
        out = billing_summary(request=req, _={}, db=db)

    assert out["overdue"] == pytest.approx(40.0)


def test_dunning_selection_and_reminder_age_use_the_shop_day(db, invoices):
    from gdx_dispatch.routers.invoice_reminders import _reminder_context, compute_due_sends

    settings = ReminderSettings(company_id=TENANT, schedule_days="[1,30]")
    db.add(settings)
    db.commit()
    with freeze_time(EVENING_UTC):
        due = compute_due_sends(db, settings)
        ctx = _reminder_context(db, invoices["late"])

    # TODAY is not a day overdue yet, so the 1-day reminder must not fire.
    assert {str(d["invoice"].id) for d in due} == {str(invoices["late"].id)}
    assert ctx["ctx"]["days_overdue"] == 30


def test_payment_plan_installment_due_today_is_pending(db, invoices):
    from gdx_dispatch.routers.invoices import _plan_out

    plan = SimpleNamespace(
        id=uuid4(), invoice_id=invoices["today"].id, status="active",
        num_installments=2, total_amount=Decimal("200"), start_date=SHOP_DAY - timedelta(days=30),
    )
    installments = [
        SimpleNamespace(id=uuid4(), seq=1, due_date=SHOP_DAY - timedelta(days=30), amount=Decimal("100")),
        SimpleNamespace(id=uuid4(), seq=2, due_date=SHOP_DAY, amount=Decimal("100")),
    ]
    with freeze_time(EVENING_UTC):
        out = _plan_out(plan, installments, invoice=None, db=db)

    assert [i["status"] for i in out["installments"]] == ["overdue", "pending"]


def test_dashboard_snapshot_today_is_the_shop_day(db, invoices):
    """The no-params dashboard window is the shop's date, and so is its overdue
    cutoff. (Its job-timestamp counts use that date's UTC-midnight bounds — a
    documented approximation, not asserted here.)"""
    from gdx_dispatch.routers.reports import daily_snapshot

    with freeze_time(EVENING_UTC):
        out = daily_snapshot(start_date=None, end_date=None, _={}, db=db)

    assert out["snapshot_date"] == SHOP_DAY.isoformat()
    assert out["overdue_invoices_count"] == 1  # LATE only
    assert out["overdue_invoices_total"] == pytest.approx(40.0)
