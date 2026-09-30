"""Cash calendar: the next N days, dated, against the operating accounts'
synced balance. Real service calls on an ORM-built SQLite schema; the router
arm goes through FastAPI."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Customer, Invoice, Job
from gdx_dispatch.modules.bank_feeds.models import BankFeedAccount
from gdx_dispatch.modules.forecasting import router as forecasting_router
from gdx_dispatch.modules.forecasting.cash_calendar import cash_calendar
from gdx_dispatch.modules.forecasting.models import (
    CADENCE_MONTHLY,
    STREAM_SOURCE_OBSERVED,
    STREAM_STATUS_ACTIVE,
    QBRecurringTransaction,
    RecurringStream,
)
from gdx_dispatch.modules.forecasting.service import get_or_create_settings
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.modules.vendor_invoices.models import VendorBillPayment, VendorInvoice

TODAY = date(2026, 10, 1)


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    return engine


@pytest.fixture()
def db():
    engine = _engine()
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


# ── seed helpers ────────────────────────────────────────────────────────────


def _account(db, name, balance, *, sync=True, inactive=False, as_of=datetime(2026, 10, 1, 5, 0, tzinfo=UTC)):
    a = BankFeedAccount(
        connection_id=uuid4(), provider="simplefin", external_account_id=uuid4().hex,
        name=name, balance=None if balance is None else Decimal(str(balance)),
        available_balance=None if balance is None else Decimal(str(balance)),
        balance_as_of=as_of, sync_enabled=sync, is_inactive=inactive,
    )
    db.add(a)
    db.commit()
    return a


def _customer(db, name="Smith"):
    c = Customer(id=uuid4(), name=name, company_id="t")
    db.add(c)
    db.commit()
    return c


def _invoice(db, amount, due, *, customer=None, job=None, status="sent", number=None):
    inv = Invoice(
        id=uuid4(), customer_id=customer.id if customer else None, job_id=job.id if job else None,
        company_id="t", invoice_number=number or f"INV-{uuid4().hex[:6]}", public_token=uuid4().hex,
        subtotal=amount, total=amount, balance_due=amount, status=status, due_date=due,
    )
    db.add(inv)
    db.commit()
    return inv


def _bill(db, total, due, *, status="open", paid=None, number=None):
    b = VendorInvoice(
        vendor_name_raw="Door supplier", invoice_number=number or f"B-{uuid4().hex[:5]}",
        subtotal=Decimal(str(total)), tax=Decimal("0"), shipping=Decimal("0"), total=Decimal(str(total)),
        status=status, due_date=due, source="manual", extraction_method="manual",
    )
    db.add(b)
    db.commit()
    if paid:
        db.add(VendorBillPayment(vendor_invoice_id=b.id, amount=Decimal(str(paid)), paid_date=TODAY))
        db.commit()
    return b


def _stream(db, label, amount, next_date):
    db.add(RecurringStream(
        label=label, source=STREAM_SOURCE_OBSERVED, status=STREAM_STATUS_ACTIVE,
        payee_pattern=label.upper(), amount_min=amount, amount_max=amount,
        cadence=CADENCE_MONTHLY, next_expected_date=next_date,
    ))
    db.commit()


def _floor(db, value):
    s = get_or_create_settings(db)
    s.cash_floor = None if value is None else Decimal(str(value))
    db.commit()


def _labels(out):
    return [r["label"] for r in out["rows"]]


# ── starting balance ────────────────────────────────────────────────────────


def test_default_accounts_are_synced_active_and_not_negative(db):
    _account(db, "Operating", 1810.02)
    _account(db, "Payroll", 2358.32)
    _account(db, "Old test account", 1000, sync=False)
    _account(db, "Credit line", -20000)
    _account(db, "Never synced", None)
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["accounts"]["chosen"] is False
    assert {a["name"] for a in out["accounts"]["included"]} == {"Operating", "Payroll"}
    reasons = {a["name"]: a["reason"] for a in out["accounts"]["excluded"]}
    assert "Syncing is off" in reasons["Old test account"]
    assert "loan or credit line" in reasons["Credit line"]
    assert "No balance" in reasons["Never synced"]
    assert out["starting_balance"] == pytest.approx(4168.34)


def test_chosen_accounts_replace_the_default(db):
    op = _account(db, "Operating", 1810.02)
    _account(db, "Payroll", 2358.32)
    s = get_or_create_settings(db)
    s.operating_account_ids = [str(op.id)]
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["accounts"]["chosen"] is True
    assert [a["name"] for a in out["accounts"]["included"]] == ["Operating"]
    assert out["starting_balance"] == pytest.approx(1810.02)


def test_balance_as_of_is_the_oldest_included_sync(db):
    _account(db, "A", 10, as_of=datetime(2026, 10, 1, 5, tzinfo=UTC))
    _account(db, "B", 10, as_of=datetime(2026, 9, 28, 5, tzinfo=UTC))
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["balance_as_of"].startswith("2026-09-28")


# ── rows ────────────────────────────────────────────────────────────────────


def test_invoices_due_in_window_are_rows_past_due_are_unscheduled(db):
    _account(db, "Operating", 1000)
    smith = _customer(db, "Smith")
    _invoice(db, 1549, TODAY + timedelta(days=2), customer=smith, number="INV-1")
    _invoice(db, 300, TODAY - timedelta(days=10), customer=smith, number="INV-OLD")
    _invoice(db, 999, TODAY + timedelta(days=40), customer=smith, number="INV-LATER")
    _invoice(db, 50, TODAY + timedelta(days=3), customer=smith, status="paid", number="INV-PAID")
    out = cash_calendar(db, days=14, today=TODAY)
    assert _labels(out) == ["Smith · invoice INV-1"]
    row = out["rows"][0]
    assert (row["date"], row["direction"], row["amount"], row["certainty"]) == ("2026-10-03", "in", 1549.0, "scheduled")
    assert row["link"]["kind"] == "invoice"
    past = out["unscheduled"]["customer_invoices_past_due"]
    assert past["count"] == 1 and past["total"] == pytest.approx(300.0)


def test_vendor_bills_use_open_balance_and_undated_or_late_are_unscheduled(db):
    _account(db, "Operating", 5000)
    _bill(db, 800, TODAY + timedelta(days=5), paid=300, number="B-DUE")
    _bill(db, 400, TODAY - timedelta(days=3), number="B-LATE")
    _bill(db, 250, None, number="B-NODATE")
    _bill(db, 900, TODAY + timedelta(days=5), status="paid", number="B-PAID")
    _bill(db, 700, TODAY + timedelta(days=5), status="void", number="B-VOID")
    out = cash_calendar(db, days=14, today=TODAY)
    assert _labels(out) == ["Door supplier · bill B-DUE"]
    assert out["rows"][0]["amount"] == pytest.approx(500.0)
    assert out["rows"][0]["direction"] == "out"
    un = out["unscheduled"]["vendor_bills_past_due_or_undated"]
    assert un["count"] == 2 and un["total"] == pytest.approx(650.0)


def test_recurring_payments_are_out_and_income_templates_are_in(db):
    _account(db, "Operating", 1000)
    _stream(db, "Loan", 376.56, TODAY)
    db.add(QBRecurringTransaction(qb_id="q-in", txn_type="Invoice", name="Maintenance plan",
                                  amount=200.0, next_date=TODAY + timedelta(days=4), active=True))
    db.add(QBRecurringTransaction(qb_id="q-est", txn_type="Estimate", name="Quote",
                                  amount=999.0, next_date=TODAY + timedelta(days=4), active=True))
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    by_label = {r["label"]: r for r in out["rows"]}
    assert set(by_label) == {"Loan", "Maintenance plan"}
    assert by_label["Loan"]["direction"] == "out"
    assert by_label["Loan"]["link"]["kind"] == "recurring_stream"
    assert by_label["Maintenance plan"]["direction"] == "in"


def test_scheduled_job_with_estimate_is_expected_money_in(db):
    _account(db, "Operating", 1000)
    c = _customer(db)
    job = Job(customer_id=c.id, title="Install", description="t", lifecycle_stage="scheduled",
              dispatch_status="assigned", company_id="t",
              scheduled_at=datetime.combine(TODAY + timedelta(days=6), datetime.min.time()).replace(hour=9))
    bare = Job(customer_id=c.id, title="No quote", description="t", lifecycle_stage="scheduled",
               dispatch_status="assigned", company_id="t",
               scheduled_at=datetime.combine(TODAY + timedelta(days=6), datetime.min.time()).replace(hour=9))
    db.add_all([job, bare])
    db.commit()
    db.add(Estimate(job_id=job.id, customer_id=c.id, estimate_number="E-1", label="e", proposal_mode=False,
                    total=Decimal("2400.00"), status="accepted", public_token=uuid4().hex, company_id="t"))
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    [row] = out["rows"]
    assert row["kind"] == "job" and row["direction"] == "in" and row["certainty"] == "expected"
    assert row["amount"] == pytest.approx(2400.0)
    assert row["date"] == "2026-10-07"
    assert out["notes"]["jobs_without_accepted_estimate"] == 1


def _job(db, c, title, day, stage="scheduled"):
    j = Job(customer_id=c.id, title=title, description="t", lifecycle_stage=stage, dispatch_status="assigned",
            company_id="t", scheduled_at=datetime.combine(TODAY + timedelta(days=day), datetime.min.time()).replace(hour=15))
    db.add(j)
    db.commit()
    return j


def _est(db, job, total, status="accepted", deleted=False):
    db.add(Estimate(job_id=job.id, customer_id=job.customer_id, estimate_number=f"E-{uuid4().hex[:6]}", label="e",
                    proposal_mode=False, total=Decimal(str(total)), status=status, public_token=uuid4().hex,
                    company_id="t", deleted_at=datetime(2026, 9, 1, tzinfo=UTC) if deleted else None))
    db.commit()


def test_only_accepted_live_estimates_count_and_estimate_visits_do_not(db):
    _account(db, "Operating", 1000)
    c = _customer(db)
    job = _job(db, c, "Install", 3)
    _est(db, job, 2000)
    _est(db, job, 2000, status="declined")
    _est(db, job, 2000, status="draft")
    _est(db, job, 2000, deleted=True)
    visit = _job(db, c, "Quote visit", 4, stage="estimate")
    _est(db, visit, 5000)
    out = cash_calendar(db, days=14, today=TODAY)
    assert [(r["label"], r["amount"]) for r in out["rows"]] == [("Smith · Install", 2000.0)]


def test_a_job_counts_only_what_is_not_yet_invoiced(db):
    _account(db, "Operating", 1000)
    c = _customer(db)
    partly = _job(db, c, "Partly", 3)
    _est(db, partly, 2000)
    _invoice(db, 500, TODAY + timedelta(days=2), customer=c, job=partly, number="DEP-1")  # open deposit invoice
    fully = _job(db, c, "Fully", 5)
    _est(db, fully, 800)
    _invoice(db, 800, TODAY - timedelta(days=20), customer=c, job=fully, status="paid", number="PAID-1")
    voided = _job(db, c, "Voided", 6)
    _est(db, voided, 300)
    _invoice(db, 300, TODAY + timedelta(days=6), customer=c, job=voided, status="void", number="VOID-1")
    out = cash_calendar(db, days=14, today=TODAY)
    rows = {r["label"]: r["amount"] for r in out["rows"]}
    # The deposit is its own row; the job carries only the remaining 1500.
    assert rows["Smith · Partly"] == 1500.0
    assert rows[f"{c.name} · invoice DEP-1"] == 500.0
    assert "Smith · Fully" not in rows
    assert rows["Smith · Voided"] == 300.0
    assert out["summary"]["total_in"] == pytest.approx(2300.0)
    assert out["notes"]["jobs_already_invoiced"] == 1


def test_job_label_is_the_title_without_a_number_and_prefixed_with_one(db):
    _account(db, "Operating", 1000)
    c = _customer(db)
    bare = _job(db, c, "Lake cabin", 2)
    _est(db, bare, 100)
    numbered = _job(db, c, "Spring swap", 3)
    numbered.job_number = "J-1042"
    db.commit()
    _est(db, numbered, 100)
    out = cash_calendar(db, days=14, today=TODAY)
    labels = [r["label"] for r in out["rows"]]
    assert labels == ["Smith · Lake cabin", "Smith · Job J-1042 · Spring swap"]
    assert not any("None" in lbl for lbl in labels)


def test_finished_jobs_not_yet_billed_are_listed_beside_the_calendar(db):
    _account(db, "Operating", 1000)
    c = _customer(db)
    done = _job(db, c, "Done, unbilled", -10, stage="completed")
    _est(db, done, 900)
    _invoice(db, 200, TODAY - timedelta(days=9), customer=c, job=done, status="draft", number="DRAFT-2")
    billed = _job(db, c, "Done, billed", -12, stage="completed")
    _est(db, billed, 400)
    _invoice(db, 400, TODAY - timedelta(days=11), customer=c, job=billed, status="paid", number="PAID-2")
    # Invoiced from the closeout for less than the estimate, and paid: done,
    # not owed. The gap is not money anyone is waiting for.
    under = _job(db, c, "Done, billed under estimate", -14, stage="completed")
    _est(db, under, 10940.23)
    _invoice(db, 9253.89, TODAY - timedelta(days=13), customer=c, job=under, status="paid", number="PAID-3")
    # Paid deposit, no final invoice yet: still owes the rest.
    deposit_only = _job(db, c, "Done, deposit only", -16, stage="completed")
    # 00:30 UTC on the 21st is the evening of the 20th in the shop's zone.
    deposit_only.completed_at = datetime(2026, 9, 21, 0, 30, tzinfo=UTC)
    db.commit()
    _est(db, deposit_only, 5000)
    dep = _invoice(db, 1000, TODAY - timedelta(days=30), customer=c, job=deposit_only, status="paid", number="DEP-9")
    dep.billing_type = "deposit"
    db.commit()
    # Marked "not billable" by the office (warranty, goodwill): settled.
    dismissed = _job(db, c, "Warranty, dismissed", -18, stage="completed")
    dismissed.not_billable_at = datetime(2026, 9, 25, tzinfo=UTC)
    db.commit()
    _est(db, dismissed, 700)
    from gdx_dispatch.models.tenant_models import AppSettings

    db.add(AppSettings(timezone="America/Chicago"))
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["rows"] == []
    fin = out["unscheduled"]["finished_jobs_not_billed"]
    items = {i["label"]: i for i in fin["items"]}
    assert set(items) == {"Smith · Done, unbilled", "Smith · Done, deposit only"}
    # Its closeout draft (200) is what will be billed, not the estimate (900).
    assert items["Smith · Done, unbilled"]["amount"] == pytest.approx(200.0)
    assert items["Smith · Done, unbilled"]["link"] == {"kind": "job", "id": str(done.id)}
    assert items["Smith · Done, deposit only"]["amount"] == pytest.approx(4000.0)
    assert items["Smith · Done, deposit only"]["due_date"] == "2026-09-20"
    assert fin["count"] == 2 and fin["total"] == pytest.approx(4200.0)
    assert fin["unpriced"] == 0


def test_finished_repairs_without_an_estimate_are_listed_too(db):
    """Repairs are priced at closeout, not by an estimate. The list is the
    same set Ready for Billing shows: a repair with a priced closeout draft is
    valued at the draft; one with nothing priced is listed with no amount."""
    _account(db, "Operating", 1000)
    c = _customer(db)
    priced = _job(db, c, "Spring repair", -3, stage="completed")
    _invoice(db, 359.06, None, customer=c, job=priced, status="draft", number="CLOSE-1")
    unpriced = _job(db, c, "Opener repair", -4, stage="completed")
    out = cash_calendar(db, days=14, today=TODAY)
    fin = out["unscheduled"]["finished_jobs_not_billed"]
    items = {i["label"]: i["amount"] for i in fin["items"]}
    assert items == {"Smith · Spring repair": 359.06, "Smith · Opener repair": None}
    assert fin["count"] == 2 and fin["unpriced"] == 1
    assert fin["total"] == pytest.approx(359.06)
    assert str(unpriced.id) in {i["link"]["id"] for i in fin["items"]}


def test_a_change_order_draft_outvalues_the_estimate(db):
    """Change-order lines go onto the invoice, never the estimate: the draft
    is what will be billed."""
    _account(db, "Operating", 1000)
    c = _customer(db)
    job = _job(db, c, "Install with CO", -2, stage="completed")
    _est(db, job, 1000)
    _invoice(db, 1400, None, customer=c, job=job, status="draft", number="CO-DRAFT")
    out = cash_calendar(db, days=14, today=TODAY)
    fin = out["unscheduled"]["finished_jobs_not_billed"]
    assert [(i["label"], i["amount"]) for i in fin["items"]] == [("Smith · Install with CO", 1400.0)]


def test_a_finished_job_is_never_dropped_while_its_billing_is_unresolved(db):
    """Deposits covering the estimate do not settle billing (Ready for Billing
    still shows the job): value falls back to the closeout draft, else none.
    Only the latest draft counts."""
    _account(db, "Operating", 1000)
    c = _customer(db)
    extra = _job(db, c, "Covered plus extra work", -3, stage="completed")
    _est(db, extra, 1000)
    dep = _invoice(db, 1000, TODAY - timedelta(days=30), customer=c, job=extra, status="paid", number="DEP-A")
    dep.billing_type = "deposit"
    db.commit()
    old = _invoice(db, 250, None, customer=c, job=extra, status="draft", number="DRAFT-OLD")
    old.created_at = datetime(2026, 9, 1, tzinfo=UTC)
    new = _invoice(db, 300, None, customer=c, job=extra, status="draft", number="DRAFT-NEW")
    new.created_at = datetime(2026, 9, 5, tzinfo=UTC)
    db.commit()
    covered = _job(db, c, "Covered, no draft", -4, stage="completed")
    _est(db, covered, 800)
    dep2 = _invoice(db, 800, TODAY - timedelta(days=30), customer=c, job=covered, status="paid", number="DEP-B")
    dep2.billing_type = "deposit"
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    fin = out["unscheduled"]["finished_jobs_not_billed"]
    items = {i["label"]: i["amount"] for i in fin["items"]}
    assert items == {"Smith · Covered plus extra work": 300.0, "Smith · Covered, no draft": None}
    assert fin["count"] == 2 and fin["unpriced"] == 1


def test_a_draft_invoice_does_not_take_money_off_the_job(db):
    """A draft is not billed: it is not a row of its own and nobody owes it
    yet, so the job keeps the full accepted amount until it is sent."""
    _account(db, "Operating", 1000)
    c = _customer(db)
    job = _job(db, c, "Draft deposit", 3)
    _est(db, job, 2000)
    _invoice(db, 500, TODAY + timedelta(days=2), customer=c, job=job, status="draft", number="DRAFT-1")
    out = cash_calendar(db, days=14, today=TODAY)
    assert [(r["label"], r["amount"]) for r in out["rows"]] == [("Smith · Draft deposit", 2000.0)]
    assert out["notes"]["jobs_already_invoiced"] == 0


def test_an_evening_job_lands_on_the_shops_local_day(db):
    """scheduled_at is stored in UTC. A job at 03:00 UTC on Oct 7 is 10pm on
    Oct 6 in the shop's zone (app_settings.timezone, America/Chicago here)."""
    from gdx_dispatch.models.tenant_models import AppSettings

    db.add(AppSettings(timezone="America/Chicago"))
    _account(db, "Operating", 1000)
    c = _customer(db)
    job = Job(customer_id=c.id, title="Late", description="t", lifecycle_stage="scheduled",
              dispatch_status="assigned", company_id="t", scheduled_at=datetime(2026, 10, 7, 3, 0, tzinfo=UTC))
    db.add(job)
    db.commit()
    db.add(Estimate(job_id=job.id, customer_id=c.id, estimate_number="E-2", label="e", proposal_mode=False,
                    total=Decimal("100.00"), status="accepted", public_token=uuid4().hex, company_id="t"))
    db.commit()
    out = cash_calendar(db, days=14, today=TODAY)
    assert [r["date"] for r in out["rows"]] == ["2026-10-06"]


def test_window_is_exactly_n_days(db):
    _account(db, "Operating", 1000)
    _bill(db, 10, TODAY + timedelta(days=13), number="LAST-DAY")
    _bill(db, 20, TODAY + timedelta(days=14), number="DAY-AFTER")
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["last_day"] == "2026-10-14"
    assert _labels(out) == ["Door supplier · bill LAST-DAY"]


# ── running balance, low point, floor ───────────────────────────────────────


def test_running_balance_money_out_first_and_the_floor(db):
    _account(db, "Operating", 1000)
    smith = _customer(db, "Smith")
    # Same day: a 900 bill and a 1549 invoice. The low point of that day is
    # after the bill, before the customer's money.
    _bill(db, 900, TODAY + timedelta(days=2), number="B1")
    _invoice(db, 1549, TODAY + timedelta(days=2), customer=smith, number="INV-1")
    _stream(db, "Payroll", 2900, TODAY + timedelta(days=8))
    _floor(db, 1000)
    out = cash_calendar(db, days=14, today=TODAY)
    assert [(r["label"], r["balance_after"], r["balance_if_nothing_comes_in"]) for r in out["rows"]] == [
        ("Door supplier · bill B1", 100.0, 100.0),
        ("Smith · invoice INV-1", 1649.0, 100.0),
        ("Payroll", -1251.0, -2800.0),
    ]
    s = out["summary"]
    assert s["lowest_balance"] == pytest.approx(-1251.0) and s["lowest_date"] == "2026-10-09"
    assert s["first_below_floor"] == "2026-10-03"
    assert s["first_below_floor_if_nothing_comes_in"] == "2026-10-03"
    assert s["lowest_if_nothing_comes_in"] == pytest.approx(-2800.0)
    assert (s["total_in"], s["total_out"], s["ending_balance"]) == (1549.0, 3800.0, -1251.0)


def test_no_floor_means_no_floor_warning(db):
    _account(db, "Operating", 100)
    _bill(db, 900, TODAY + timedelta(days=2))
    out = cash_calendar(db, days=14, today=TODAY)
    assert out["floor"] is None
    assert out["summary"]["first_below_floor"] is None
    assert out["summary"]["lowest_balance"] == pytest.approx(-800.0)


def test_starting_below_the_floor_is_flagged_today(db):
    _account(db, "Operating", 400)
    _floor(db, 500)
    out = cash_calendar(db, days=7, today=TODAY)
    assert out["summary"]["first_below_floor"] == TODAY.isoformat()


def test_days_outside_range_raise(db):
    with pytest.raises(ValueError):
        cash_calendar(db, days=0, today=TODAY)
    with pytest.raises(ValueError):
        cash_calendar(db, days=91, today=TODAY)


# ── router ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def client():
    engine = _engine()
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        d = SessionLocal()
        try:
            yield d
        finally:
            d.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": "tenant-test"}
        return await call_next(request)

    app.include_router(forecasting_router.router)
    app.dependency_overrides[forecasting_router.get_db] = _override_db
    app.dependency_overrides[forecasting_router.get_current_user] = lambda: {
        "sub": "test-user", "role": "admin", "tenant_id": "tenant-test",
    }
    yield TestClient(app, raise_server_exceptions=True), SessionLocal
    app.dependency_overrides.clear()
    engine.dispose()


def test_endpoint_validates_days_and_returns_the_calendar(client):
    tc, SessionLocal = client
    assert tc.get("/api/forecast/cash-calendar?days=0").status_code == 400
    assert tc.get("/api/forecast/cash-calendar?days=91").status_code == 400
    d = SessionLocal()
    _account(d, "Operating", 1234.5)
    d.close()
    r = tc.get("/api/forecast/cash-calendar?days=14")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days"] == 14
    assert body["starting_balance"] == pytest.approx(1234.5)
    assert set(body["summary"]) >= {"lowest_balance", "first_below_floor", "first_below_floor_if_nothing_comes_in"}


def test_settings_save_floor_and_accounts_with_an_audit_row(client):
    from gdx_dispatch.core.audit import AuditLog

    tc, SessionLocal = client
    d = SessionLocal()
    op = _account(d, "Operating", 1000)
    op_id = str(op.id)
    d.close()
    r = tc.put("/api/forecast/settings", json={"cash_floor": 1500, "operating_account_ids": [op_id]})
    assert r.status_code == 200, r.text
    assert r.json()["cash_floor"] == 1500.0
    assert r.json()["operating_account_ids"] == [op_id]
    cal = tc.get("/api/forecast/cash-calendar?days=7").json()
    assert cal["floor"] == 1500.0 and cal["accounts"]["chosen"] is True
    assert cal["summary"]["first_below_floor"] == cal["as_of"]
    d = SessionLocal()
    rows = d.execute(select(AuditLog).where(AuditLog.action == "forecast_settings.update")).scalars().all()
    assert rows and rows[-1].details.get("cash_floor") == 1500
    d.close()
    # Clearing: the floor explicitly, the accounts back to the default.
    r = tc.put("/api/forecast/settings", json={"clear_cash_floor": True, "operating_account_ids": []})
    assert r.json()["cash_floor"] is None
    assert r.json()["operating_account_ids"] == []


def test_settings_refuse_an_unknown_account_id(client):
    tc, _ = client
    r = tc.put("/api/forecast/settings", json={"operating_account_ids": [str(uuid4())]})
    assert r.status_code == 400
    assert "unknown bank account" in r.json()["detail"]
    r = tc.put("/api/forecast/settings", json={"operating_account_ids": ["not-a-uuid"]})
    assert r.status_code == 400
