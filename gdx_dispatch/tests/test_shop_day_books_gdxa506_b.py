"""GDXA-506 (part b) — the books' business dates are the shop's calendar day.

The clock is frozen at 03:00 UTC on 2026-11-01, which is 22:00 on 2026-10-31
in America/Chicago (``AppSettings.timezone``): past UTC midnight AND past UTC
month end, before shop midnight. On the server's UTC clock it is already
November; in the shop it is still the last evening of October.

Every test here fails on the pre-GDXA-506 code, which took today from the UTC
clock (``date.today()`` / ``datetime.now(UTC).date()``) or took a UTC instant's
UTC date, and so moved the shop's evening onto tomorrow and into next month.
Route functions are called directly: freezegun swaps ``date`` inside router
modules, which FastAPI's parameter analysis does not survive.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import ensure_audit_table
from gdx_dispatch.models.tenant_models import (
    AppSettings,
    Customer,
    Expense,
    Invoice,
    Job,
    JobReceipt,
    OverheadObligation,
)

# Imported before any clock is frozen: building its pydantic models while
# freezegun has swapped ``date`` for FakeDate fails schema generation.
from gdx_dispatch.routers import vendor_invoices as _vendor_invoices_router  # noqa: F401
from gdx_dispatch.tests.conftest import make_fresh_db

FROZEN_UTC = "2026-11-01 03:00:00"  # 2026-10-31 22:00 CDT
SHOP_TODAY = date(2026, 10, 31)
TENANT = "tenant-506b"
ADMIN = {"sub": "office-user", "user_id": "office-user", "role": "admin", "tenant_id": TENANT}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    monkeypatch.delenv("GDX_ENV", raising=False)
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    ensure_audit_table(session)
    session.add(AppSettings(timezone="America/Chicago"))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": TENANT}),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


# --------------------------------------------------------------------------- #
# 1. routers/expenses.py — promote a field receipt
# --------------------------------------------------------------------------- #
def _promote(db, purchased_at):
    from gdx_dispatch.modules.ledger.service import ensure_gl_seed
    from gdx_dispatch.routers.expenses import PromoteReceiptIn, promote_job_receipt

    ensure_gl_seed(db, TENANT)
    db.commit()
    receipt = JobReceipt(
        job_id=uuid4(), vendor="Fuel stop", amount=Decimal("41.00"),
        notes="evening fuel", purchased_at=purchased_at,
    )
    db.add(receipt)
    db.commit()
    with freeze_time(FROZEN_UTC):
        return promote_job_receipt(
            PromoteReceiptIn(job_receipt_id=receipt.id), request=_request(), _=ADMIN, db=db,
        )


def test_promoted_receipt_is_dated_on_the_shop_day_it_was_bought(db):
    """02:00 UTC on Nov 1 is 21:00 on Oct 31 in the shop: an October expense."""
    out = _promote(db, datetime(2026, 11, 1, 2, 0, tzinfo=UTC))
    assert out["date"] == SHOP_TODAY.isoformat()


def test_promoted_receipt_with_no_purchase_time_is_dated_shop_today(db):
    out = _promote(db, None)
    assert out["date"] == SHOP_TODAY.isoformat()


# --------------------------------------------------------------------------- #
# 2-3. routers/overhead.py — the projection anchors on the shop's month
# --------------------------------------------------------------------------- #
def _october_only_obligation(db):
    """A loan whose last payment month is October 2026."""
    db.add(OverheadObligation(
        company_id=TENANT, label="Truck loan", category="other",
        amount=Decimal("500.00"), cadence="monthly",
        start_date=date(2026, 1, 1), end_date=date(2026, 10, 31),
    ))
    db.commit()


def test_overhead_list_monthly_total_is_the_shop_month(db):
    from gdx_dispatch.routers.overhead import list_obligations

    _october_only_obligation(db)
    with freeze_time(FROZEN_UTC):
        out = list_obligations(request=_request(), include_inactive=False, db=db)
    # Still October in the shop: the loan is still owed this month.
    assert out["current_monthly_total"] == "500.00"


def test_overhead_projection_starts_at_the_shop_month(db):
    from gdx_dispatch.routers.overhead import get_projection

    _october_only_obligation(db)
    with freeze_time(FROZEN_UTC):
        out = get_projection(request=_request(), horizon_months=3, db=db)
    assert [m["label"] for m in out["months"]] == ["2026-10", "2026-11", "2026-12"]
    assert out["current_monthly_total"] == "500.00"


# --------------------------------------------------------------------------- #
# 4. routers/reports.py — outstanding aging on the shop's today
# --------------------------------------------------------------------------- #
def test_outstanding_aging_ages_on_the_shop_day(db):
    from gdx_dispatch.routers.reports import outstanding_aging

    customer = Customer(id=uuid4(), name="Aging", email="aging@example.com", company_id=TENANT)
    db.add(customer)
    db.commit()

    def _inv(number, due, balance):
        db.add(Invoice(
            id=uuid4(), customer_id=customer.id, invoice_number=number,
            billing_type="standard", sequence_number=1,
            subtotal=Decimal(balance), tax_amount=Decimal("0"),
            total=Decimal(balance), balance_due=Decimal(balance),
            status="sent", invoice_date=date(2026, 9, 1), due_date=due,
            public_token=uuid4().hex, locked=False, company_id=TENANT,
        ))

    _inv("INV-506B01", date(2026, 11, 1), "7.00")   # due tomorrow in the shop
    _inv("INV-506B02", date(2026, 10, 1), "40.00")  # 30 days in the shop, 31 in UTC
    db.commit()

    with freeze_time(FROZEN_UTC):
        out = outstanding_aging(_={}, db=db)

    assert out["counts"] == {"0_30": 1, "31_60": 0, "61_90": 0, "91_plus": 0}
    assert out["totals"]["0_30"] == pytest.approx(40.0)


# --------------------------------------------------------------------------- #
# 5. routers/vendor_statements.py — age of the oldest open line
# --------------------------------------------------------------------------- #
def test_vendor_account_oldest_open_days_count_to_the_shop_day(db):
    from gdx_dispatch.routers.vendor_statements import list_vendor_accounts
    from gdx_dispatch.tests.test_vendor_account_view import _statement

    _statement(db, date(2026, 10, 15), [("INV9", 250, 250, "0-29", date(2026, 10, 1))])
    db.commit()

    with freeze_time(FROZEN_UTC):
        [account] = asyncio.run(list_vendor_accounts(_=ADMIN, db=db))

    assert account.oldest_line_date == date(2026, 10, 1)
    assert account.days_oldest_open == 30


# --------------------------------------------------------------------------- #
# 6. modules/bank_feeds/simplefin_router.py — the balance-history window
# --------------------------------------------------------------------------- #
def test_balance_history_window_counts_back_from_the_shop_day(db):
    from gdx_dispatch.modules.bank_feeds import simplefin_router as sr
    from gdx_dispatch.modules.bank_feeds.models import (
        AUTH_HEALTHY,
        PROVIDER_SIMPLEFIN,
        BankFeedAccount,
        BankFeedBalanceSnapshot,
        BannoConnection,
        BannoInstitution,
    )

    inst = BannoInstitution(provider=PROVIDER_SIMPLEFIN, fi_host="bridge.example",
                            display_label="SimpleFIN Bridge")
    db.add(inst)
    db.commit()
    conn = BannoConnection(institution_id=inst.id, fi_host="bridge.example",
                           banno_user_id=sr.SIMPLEFIN_USER_ID,
                           provider_base_url="https://bridge.example/simplefin",
                           auth_state=AUTH_HEALTHY)
    db.add(conn)
    db.commit()
    acct = BankFeedAccount(connection_id=conn.id, external_account_id="A1",
                           provider=PROVIDER_SIMPLEFIN, name="Checking")
    db.add(acct)
    db.commit()
    # Exactly 30 days before the shop's today: the window's first day.
    db.add(BankFeedBalanceSnapshot(account_id=acct.id, snapshot_date=date(2026, 10, 1),
                                   balance_cents=1234))
    db.commit()

    with freeze_time(FROZEN_UTC):
        out = sr.simplefin_balance_history(str(acct.id), days=30, _perm=None, db=db)

    assert [s["date"] for s in out["snapshots"]] == ["2026-10-01"]


# --------------------------------------------------------------------------- #
# 7-8. modules/vendor_invoices — placeholder expense date and the lock fallback
# --------------------------------------------------------------------------- #
def test_undated_unsettled_vendor_bill_expense_date_is_shop_today(db):
    from gdx_dispatch.modules.vendor_invoices.models import VendorInvoice
    from gdx_dispatch.modules.vendor_invoices.payments import effective_expense_date

    inv = VendorInvoice(vendor_name_raw="Example Door Supply", invoice_number="VB-506",
                        total=Decimal("100.00"), invoice_date=None)
    db.add(inv)
    db.commit()

    with freeze_time(FROZEN_UTC):
        assert effective_expense_date(db, inv) == SHOP_TODAY


def _posted_bill_in_a_locked_month(db):
    """A confirmed, GL-posted bill whose expense entry sits in a closed month
    (locked through 2026-10-30), so a reversal must fall back to today."""
    from gdx_dispatch.modules.ledger.models import GlPeriodLock
    from gdx_dispatch.tests.test_m29_vendor_void_reversal import (
        TENANT as M29_TENANT,
    )
    from gdx_dispatch.tests.test_m29_vendor_void_reversal import (
        _bill_with_confirmed_lines,
        _enable_posting,
        _expense_entries,
    )

    _enable_posting(db)
    with freeze_time(FROZEN_UTC):
        inv, _, _, (l1, _) = _bill_with_confirmed_lines(db)
    exp = db.get(Expense, l1.expense_id)
    [entry] = _expense_entries(db, exp.id)
    exp.date = date(2026, 10, 10)
    entry.effective_at = date(2026, 10, 10)
    db.add(GlPeriodLock(lock_date=date(2026, 10, 30), company_id=M29_TENANT))
    db.commit()
    return inv, exp


def test_void_in_a_locked_month_reverses_on_the_shop_day(db):
    from gdx_dispatch.tests.test_m29_vendor_void_reversal import _expense_entries, _void

    inv, exp = _posted_bill_in_a_locked_month(db)
    with freeze_time(FROZEN_UTC):
        _void(db, inv)

    reversal = next(e for e in _expense_entries(db, exp.id) if e.reverses_entry_id is not None)
    assert reversal.effective_at == SHOP_TODAY


def test_redate_reversal_in_a_locked_month_posts_on_the_shop_day(db):
    """sync_expense_dates' era branch: the bill's target date falls before the
    cutover, so the prior post-cutover entry unwinds; its month is locked, so
    the reversal posts at today — the shop's today."""
    from gdx_dispatch.modules.ledger import service as ledger_service
    from gdx_dispatch.modules.vendor_invoices.payments import sync_expense_dates
    from gdx_dispatch.tests.test_m29_vendor_void_reversal import TENANT as M29_TENANT
    from gdx_dispatch.tests.test_m29_vendor_void_reversal import _expense_entries

    inv, exp = _posted_bill_in_a_locked_month(db)
    inv.invoice_date = date(2026, 1, 15)
    ledger_service.get_gl_settings(db, M29_TENANT).cutover_month = date(2026, 2, 1)
    db.commit()

    with freeze_time(FROZEN_UTC):
        assert sync_expense_dates(db, inv) == 1
    db.commit()

    reversal = next(e for e in _expense_entries(db, exp.id) if e.reverses_entry_id is not None)
    assert reversal.effective_at == SHOP_TODAY


# --------------------------------------------------------------------------- #
# 9. routers/exports.py — date-range filters and the filename
# --------------------------------------------------------------------------- #
EVENING = datetime(2026, 11, 1, 2, 0)    # 21:00 Oct 31 in the shop (naive = UTC)
PREVIOUS = datetime(2026, 10, 31, 4, 0)  # 23:00 Oct 30 in the shop


def _two_jobs(db):
    customer = Customer(id=uuid4(), name="Export", email="export@example.com", company_id=TENANT)
    db.add(customer)
    db.commit()
    evening = Job(id=uuid4(), title="Evening job", customer_id=customer.id,
                  company_id=TENANT, created_at=EVENING)
    previous = Job(id=uuid4(), title="Previous-day job", customer_id=customer.id,
                   company_id=TENANT, created_at=PREVIOUS)
    db.add_all([evening, previous])
    db.commit()
    return customer, evening, previous


def _csv_ids(response):
    lines = response.body.decode().strip().splitlines()[1:]
    return {ln.split(",")[0] for ln in lines}


def test_jobs_export_filters_on_the_shop_day_and_names_the_file_for_it(db):
    from gdx_dispatch.routers.exports import export_jobs

    _, evening, previous = _two_jobs(db)
    with freeze_time(FROZEN_UTC):
        resp = export_jobs(request=_request(), user=ADMIN, db=db, status=None,
                           start="2026-10-31", end="2026-10-31")

    ids = {i.replace("-", "") for i in _csv_ids(resp)}
    assert ids == {evening.id.hex}, "the shop's Oct 31 holds the evening job only"
    assert previous.id.hex not in ids
    assert "jobs_tenant-506b_2026-10-31.csv" in resp.headers["content-disposition"]


def test_invoices_export_filters_on_the_shop_day(db):
    from gdx_dispatch.routers.exports import export_invoices

    customer, _, _ = _two_jobs(db)

    def _inv(number, created):
        inv = Invoice(
            id=uuid4(), customer_id=customer.id, invoice_number=number,
            billing_type="standard", sequence_number=1,
            subtotal=Decimal("10"), tax_amount=Decimal("0"), total=Decimal("10"),
            balance_due=Decimal("10"), status="sent", invoice_date=date(2026, 10, 30),
            due_date=date(2026, 11, 30), public_token=uuid4().hex, locked=False,
            company_id=TENANT, created_at=created,
        )
        db.add(inv)
        return inv

    _inv("INV-506B10", EVENING)
    _inv("INV-506B11", PREVIOUS)
    db.commit()

    with freeze_time(FROZEN_UTC):
        resp = export_invoices(request=_request(), user=ADMIN, db=db, status=None,
                               start="2026-10-31", end="2026-10-31")

    numbers = {ln.split(",")[1] for ln in resp.body.decode().strip().splitlines()[1:]}
    assert numbers == {"INV-506B10"}
    assert "invoices_tenant-506b_2026-10-31.csv" in resp.headers["content-disposition"]


def test_expense_rows_read_back_unchanged_after_promote(db):
    """Guard on the promote fixture itself: the stored Expense carries the
    shop day too, not only the response dict."""
    out = _promote(db, datetime(2026, 11, 1, 2, 0, tzinfo=UTC))
    stored = db.scalars(select(Expense).where(Expense.vendor == "Fuel stop")).one()
    assert str(stored.id) == out["id"]
    assert stored.date == SHOP_TODAY
