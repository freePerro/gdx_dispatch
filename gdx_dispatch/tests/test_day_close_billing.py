"""Multi-day jobs, billing: one labor line, hours as its quantity, and the
day rows billed through a point (``time_entries.billed_invoice_id``).

The days a crew closed with "No" join the final day's closeout hours on the
SAME labor: the closeout's hours round up to the half hour × techs, the day
rows' minutes add on and round up to the half hour ONCE, and the first-hour
price and the 1 h floor apply once, to that sum — and only when no other live
invoice on the job already bills attested labor. The rows a line bills are
stamped with its invoice, so a second invoice cannot bill them again; a void,
a delete, or an autodraft rebuild gives them back, with an audit row.

Every test here runs the real builder against a real ORM-built SQLite database
and reads back the invoice lines and time entries it wrote.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.billing_lanes import FIRST_HOUR_DESCRIPTION, job_labor_lines
from gdx_dispatch.core.closeout_billing import (
    AUTODRAFT_LINE_SOURCE,
    autodraft_invoice_for_closeout,
    build_closeout_lines,
    release_untouched_autodraft,
    unbilled_day_rows,
    void_untouched_autodraft,
)
from gdx_dispatch.core.job_taxonomy import INSTALLATION, SERVICE_CALL
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobCloseout,
    TimeEntry,
)
from gdx_dispatch.modules.proposals.models import Estimate
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-day-close-billing"
DAY1 = _dt.datetime(2026, 11, 2, 15, 0, tzinfo=_dt.UTC)
DAY2 = DAY1 + _dt.timedelta(days=1)
DAY3 = DAY1 + _dt.timedelta(days=2)


@pytest.fixture
def db():
    engine = make_fresh_db()
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    # Distinct prices, so where the first hour lands is visible.
    session.add(PricingSettings(
        service_call_first_hour_price=Decimal("150.00"),
        service_call_hourly_rate=Decimal("100.00"),
    ))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(db, *, job_type=SERVICE_CALL) -> Job:
    cust = Customer(id=uuid4(), name="Acme", phone="555", email="a@x.com",
                    address="11 Main", company_id=TENANT)
    db.add(cust)
    db.flush()
    job = Job(
        id=uuid4(), company_id=TENANT, customer_id=cust.id, title="Spring",
        description="", scheduled_at=DAY1, status="Completed", priority="Normal",
        job_type=job_type, lifecycle_stage="completed", dispatch_status="assigned",
        billing_status="unbilled", is_demo=False, is_return_visit=False,
    )
    db.add(job)
    db.flush()
    return job


def _row(db, job, *, at, minutes, day_closed=True, clock_out=True, deleted=False,
         user_id=None) -> TimeEntry:
    te = TimeEntry(
        id=uuid4(), job_id=job.id, tech_id=str(uuid4()), company_id=TENANT,
        entry_type="job", user_id=user_id, clock_in=at,
        clock_out=(at + _dt.timedelta(minutes=minutes)) if clock_out else None,
        duration_minutes=minutes,
        day_closed_at=(at + _dt.timedelta(hours=9)) if day_closed else None,
        deleted_at=DAY3 if deleted else None,
    )
    db.add(te)
    db.flush()
    return te


def _closeout(db, job, *, hours, techs=1, matrix=None) -> JobCloseout:
    co = JobCloseout(
        id=uuid4(), job_id=job.id, hours_worked=hours, techs_on_site=techs,
        labor_matrix_item_id=matrix, closed_by_user_id=str(uuid4()), closed_at=DAY3,
    )
    db.add(co)
    db.flush()
    return co


def _invoice(db, job) -> Invoice:
    inv = Invoice(
        id=uuid4(), job_id=job.id, customer_id=job.customer_id,
        invoice_number=f"INV-{uuid4().hex[:6]}", public_token=uuid4().hex,
        status="draft", company_id=TENANT,
    )
    db.add(inv)
    db.flush()
    return inv


def _lines(db, invoice) -> list[InvoiceLine]:
    db.flush()
    return list(db.execute(
        select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id, InvoiceLine.deleted_at.is_(None))
        .order_by(InvoiceLine.sort_order)
    ).scalars())


def _snapshot(lines) -> list[tuple]:
    return [
        (ln.description, ln.quantity, Decimal(str(ln.unit_price)), Decimal(str(ln.line_total)),
         ln.taxable, ln.category,
         None if ln.estimated_man_hours is None else Decimal(str(ln.estimated_man_hours)),
         ln.labor_source, ln.source, ln.sort_order)
        for ln in lines
    ]


def _build(db, job, co, inv):
    return build_closeout_lines(
        db, tenant_id=TENANT, invoice=inv, closeout=co, job_type=job.job_type, job_id=str(job.id),
    )


def _labor(lines) -> list[InvoiceLine]:
    return [ln for ln in lines if ln.category == "Labor"]


def _priced(lines) -> list[tuple]:
    return [(ln.description.split(" (")[0], Decimal(str(ln.quantity)), Decimal(str(ln.unit_price)),
             Decimal(str(ln.line_total))) for ln in lines]


def _holder(db, row) -> object:
    db.expire_all()
    return db.get(TimeEntry, row.id).billed_invoice_id


def _equal_rates(db, rate="100.00"):
    ps = db.execute(select(PricingSettings)).scalar_one()
    ps.service_call_first_hour_price = Decimal(rate)
    ps.service_call_hourly_rate = Decimal(rate)
    db.flush()


ACTOR = "00000000-0000-0000-0000-0000000000a1"


def _released_audits(db, inv) -> list[AuditLog]:
    db.flush()
    return list(db.execute(
        select(AuditLog).where(AuditLog.action == "labor_day_rows_released",
                               AuditLog.entity_id == str(inv.id))
    ).scalars())


# ── the line ─────────────────────────────────────────────────────────


def test_day_rows_join_the_final_day_on_one_labor_with_one_first_hour(db):
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=180)       # day 1, the tech
    r2 = _row(db, job, at=DAY2, minutes=252)       # day 2, a helper (4.2 h)
    co = _closeout(db, job, hours=2)
    inv = _invoice(db, job)

    added, total, _taxable = _build(db, job, co, inv)
    labor = _labor(_lines(db, inv))

    # 2.0 h final day + 7.2 h of day rows rounded up ONCE to 7.5 = 9.5
    # man-hours. The rates differ, so a split pair: the first hour at $150,
    # then 8.5 h at $100. Not a second first hour, and not $150 + $750 + $250.
    assert _priced(labor) == [
        (FIRST_HOUR_DESCRIPTION, Decimal("1"), Decimal("150"), Decimal("150")),
        ("Service labor — 8.50 h after the first hour at $100.00/hr", Decimal("8.5"), Decimal("100"),
         Decimal("850")),
    ]
    # The text states the line's own quantity, never the job's total as if
    # it were (D16), and does not offer the first hour again.
    assert labor[1].description == (
        "Service labor — 8.50 h after the first hour at $100.00/hr (9.50 man-hours over 3 days)"
    )
    assert added == 2
    assert total == Decimal("1000.00")
    for ln in labor:
        assert ln.labor_source == "attested"
        assert ln.pricing_source == "labor_attested"
        assert ln.source == AUTODRAFT_LINE_SOURCE
        assert ln.taxable is False
    # The raw attested man-hours are the record, on the hourly line only.
    assert labor[0].estimated_man_hours is None
    assert Decimal(str(labor[1].estimated_man_hours)) == Decimal("9.20")
    # Both rows are now billed by this invoice.
    assert _holder(db, r1) == inv.id and _holder(db, r2) == inv.id


def test_equal_rates_bill_one_line_with_hours_as_quantity(db):
    _equal_rates(db)
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=2.1)
    inv = _invoice(db, job)
    _build(db, job, co, inv)
    # 2.5 + 3.0 = 5.5 h × $100: the split collapses when the rates agree.
    assert _priced(_labor(_lines(db, inv))) == [
        ("Service labor — 5.50 man-hours at $100.00/hr over 2 days", Decimal("5.5"), Decimal("100"),
         Decimal("550")),
    ]


def test_rounding_is_once_over_the_day_rows_not_per_day(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=70)    # 1.17 h — per-day rounding would bill 1.5
    _row(db, job, at=DAY2, minutes=70)    # 1.17 h — and again 1.5, = 3.0
    [first, rest] = job_labor_lines(db, job, None)
    # 140 min = 2.33 h → 2.5 once: the first hour, then 1.5 h.
    assert (first["quantity"], rest["quantity"]) == (Decimal("1.00"), Decimal("1.50"))
    assert rest["man_hours"] == 2.33


def test_the_floor_applies_once_to_the_sum(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=20)
    # 0.5 h alone floors to the first hour, billed alone.
    [line] = job_labor_lines(db, job, None)
    assert (line["description"], line["quantity"], line["line_total"]) == (
        FIRST_HOUR_DESCRIPTION, Decimal("1.00"), Decimal("150.00"))
    # With a 2 h final day the sum is 2.5 h, and nothing is floored twice.
    co = _closeout(db, job, hours=2)
    lines = job_labor_lines(db, job, co)
    assert sum(ln["line_total"] for ln in lines) == Decimal("300.00")


def test_only_day_rows_count(db):
    """A consumed timer (0 min, marker set), an ordinary job timer with no
    marker, a soft-deleted row and an unclosed row add nothing."""
    job = _job(db)
    _row(db, job, at=DAY1, minutes=120)
    _row(db, job, at=DAY1, minutes=0)                      # consumed at 0
    _row(db, job, at=DAY1, minutes=600, day_closed=False)  # a plain timer
    _row(db, job, at=DAY1, minutes=600, deleted=True)
    _row(db, job, at=DAY1, minutes=600, clock_out=False)
    other = _job(db)
    _row(db, other, at=DAY1, minutes=600)                  # another job's day
    lines = job_labor_lines(db, job, None)
    assert sum(ln["line_total"] for ln in lines) == Decimal("250.00")  # 150 + 1 × 100


def test_no_day_rows_and_no_hours_is_no_line(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=0)
    assert job_labor_lines(db, job, None) == []
    co = _closeout(db, job, hours=0)
    assert job_labor_lines(db, job, co) == []
    inv = _invoice(db, job)
    assert _build(db, job, co, inv)[0] == 0


def test_the_carrier_line_has_the_suggestion_shape(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    [_first, line] = job_labor_lines(db, job, None)
    assert set(line) == {
        "description", "quantity", "unit_price", "line_total", "source",
        "labor_price_item_id", "man_hours", "time_entry_ids", "estimated_man_hours",
    }
    # `source` is labor provenance there: the picker only accepts "attested".
    assert line["source"] == "attested"
    assert line["labor_price_item_id"] is None
    assert line["man_hours"] == 3.0
    assert line["time_entry_ids"] == [str(row.id)]


def test_install_lane_bills_no_day_rows(db):
    job = _job(db, job_type=INSTALLATION)
    row = _row(db, job, at=DAY1, minutes=480)
    assert job_labor_lines(db, job, None) == []
    co = _closeout(db, job, hours=4)
    inv = _invoice(db, job)
    _build(db, job, co, inv)
    assert _holder(db, row) is None, "an install bills from the matrix, never the day rows"


# ── build_closeout_lines ─────────────────────────────────────────────


def test_a_final_day_of_zero_hours_still_bills_the_earlier_days(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=360)
    co = _closeout(db, job, hours=0)
    inv = _invoice(db, job)
    added, total, _t = _build(db, job, co, inv)
    assert added == 2
    # 6 h: the first hour at $150, then 5 h at $100.
    assert total == Decimal("650.00")
    assert _holder(db, row) == inv.id


def test_a_job_with_no_day_rows_bills_hours_as_the_quantity(db):
    _equal_rates(db)
    job = _job(db)
    _row(db, job, at=DAY1, minutes=480, day_closed=False)  # a plain closed timer
    _row(db, job, at=DAY1, minutes=0)                      # a consumed timer
    co = _closeout(db, job, hours=1.75, techs=2)
    inv = _invoice(db, job)
    added, total, _t = _build(db, job, co, inv)
    [line] = _lines(db, inv)
    # 2.0 h × 2 techs = 4 man-hours, billed as 4 × $100 — the same $400 the
    # old one-line `1 × $400` billed, in the new shape.
    assert (added, total) == (1, Decimal("400.00"))
    assert (Decimal(str(line.quantity)), Decimal(str(line.unit_price))) == (Decimal("4"), Decimal("100"))
    assert line.description.startswith("Service labor — 4.00 man-hours")
    assert "over" not in line.description


def test_the_lines_follow_the_labor_taxable_flag(db, monkeypatch):
    import gdx_dispatch.modules.proposals.totals as totals

    job = _job(db)
    _row(db, job, at=DAY1, minutes=60)
    co = _closeout(db, job, hours=1)

    inv = _invoice(db, job)
    _added, _total, taxable = _build(db, job, co, inv)
    assert all(ln.taxable is False for ln in _labor(_lines(db, inv)))
    assert taxable == Decimal("0")

    monkeypatch.setattr(totals, "_load_tax_labor_flag", lambda _db: True)
    # A second invoice finds the rows already billed: only the closeout's
    # hour, at the hourly rate, and every labor line follows the flag.
    inv2 = _invoice(db, job)
    _added, total2, taxable2 = _build(db, job, co, inv2)
    assert all(ln.taxable is True for ln in _labor(_lines(db, inv2)))
    assert taxable2 == total2 == Decimal("100.00")


# ── a second invoice, a void, a rebuild ──────────────────────────────


def test_a_second_invoice_bills_only_what_is_left_and_no_second_first_hour(db):
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=1)
    inv1 = _invoice(db, job)
    _build(db, job, co, inv1)
    assert _holder(db, r1) == inv1.id

    # Reopened: another day row, a new closeout, a second invoice.
    r2 = _row(db, job, at=DAY2, minutes=60)
    co.superseded_at = DAY3
    co2 = _closeout(db, job, hours=0.25)
    inv2 = _invoice(db, job)
    _added, total, _t = _build(db, job, co2, inv2)
    # 0.5 h + 1.0 h = 1.5 h, at the hourly rate only, no floor.
    assert _priced(_labor(_lines(db, inv2))) == [
        ("Service labor — 1.50 man-hours at $100.00/hr over 2 days", Decimal("1.5"), Decimal("100"),
         Decimal("150")),
    ]
    assert _labor(_lines(db, inv2))[0].description == (
        "Service labor — 1.50 man-hours at $100.00/hr over 2 days"
    )
    assert total == Decimal("150.00")
    assert _holder(db, r1) == inv1.id, "the first invoice keeps its rows"
    assert _holder(db, r2) == inv2.id


def test_void_then_re_invoice_bills_the_rows_again_with_an_audit_row(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=1)
    inv = _invoice(db, job)
    _added, total, _t = _build(db, job, co, inv)
    inv.subtotal = inv.total = inv.balance_due = total
    db.flush()  # _holder expires the session; unflushed totals would be lost
    assert _holder(db, row) == inv.id

    void_untouched_autodraft(db, inv, actor=ACTOR)
    db.commit()  # the not-billable route commits the void
    assert _holder(db, row) is None
    [audit] = _released_audits(db, inv)
    assert audit.user_id == ACTOR
    assert audit.details["released_time_entries"] == 1
    assert audit.details["why"] == "invoice_voided"

    # The void no longer counts toward "first hour charged", so the re-invoice
    # bills it again: 4 h = $150 + 3 × $100.
    inv2 = _invoice(db, job)
    _added, total, _t = _build(db, job, co, inv2)
    assert total == Decimal("450.00")
    assert _holder(db, row) == inv2.id


def test_the_autodraft_claims_and_a_re_closeout_releases_and_rebuilds(db):
    job = _job(db)
    r1 = _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=1)
    inv = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co)
    assert inv is not None
    assert Decimal(str(inv.total)) == Decimal("450.00")  # 4 h: $150 + 3 × $100
    assert _holder(db, r1) == inv.id

    # A day row lands late (another "No" restated), then the job is re-closed.
    r2 = _row(db, job, at=DAY2, minutes=60)
    emptied = release_untouched_autodraft(db, job=job, actor=ACTOR)
    assert emptied is inv
    assert _lines(db, inv) == []   # the machine owned the lines, so it released them
    assert _holder(db, r1) is None, "the rebuild must be free to re-claim the row"
    [audit] = _released_audits(db, inv)
    assert audit.details["why"] == "autodraft_rebuilt"
    assert audit.user_id == ACTOR

    co.superseded_at = DAY3
    co2 = _closeout(db, job, hours=1)
    rebuilt = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co2, reuse_invoice=emptied)
    assert rebuilt is inv
    assert Decimal(str(inv.total)) == Decimal("550.00")  # 5 h: $150 + 4 × $100
    assert _holder(db, r1) == inv.id and _holder(db, r2) == inv.id
    assert unbilled_day_rows(db, job.id) == []


def _claimed_audits(db, inv) -> list[AuditLog]:
    db.flush()
    return list(db.execute(
        select(AuditLog).where(AuditLog.action == "labor_day_rows_claimed",
                               AuditLog.entity_id == str(inv.id))
    ).scalars())


def test_the_builders_claim_writes_one_audit_row_by_the_closer(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)
    _row(db, job, at=DAY2, minutes=60)
    co = _closeout(db, job, hours=1)
    inv = _invoice(db, job)
    _build(db, job, co, inv)

    [audit] = _claimed_audits(db, inv)
    assert audit.user_id == co.closed_by_user_id
    assert audit.details["claimed_time_entries"] == 2
    assert audit.details["job_id"] == str(job.id)

    # Nothing left to claim: a second build stages no second row.
    inv2 = _invoice(db, job)
    _build(db, job, co, inv2)
    assert _claimed_audits(db, inv2) == []


def test_the_mobile_invoice_route_claims_the_day_rows_as_the_tech(db):
    """``POST /api/mobile/jobs/{id}/invoice`` builds from the closeout too, so
    it claims the day rows, and the claim is the tech's."""
    import json as _json  # noqa: PLC0415

    from starlette.requests import Request  # noqa: PLC0415

    from gdx_dispatch.models.tenant_models import Technician  # noqa: PLC0415
    from gdx_dispatch.routers.mobile_invoicing import (  # noqa: PLC0415
        CreateInvoiceIn,
        mobile_create_invoice,
    )

    user = "tech-user-day-close-billing"
    job = _job(db)
    tech = Technician(id=uuid4().hex, name="Tech", user_id=user, company_id=TENANT)
    db.add(tech)
    job.assigned_to = tech.id
    row = _row(db, job, at=DAY1, minutes=180)
    _closeout(db, job, hours=1)
    db.commit()

    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": TENANT}
    req.state.tenant_id = TENANT
    resp = mobile_create_invoice(
        job_id=job.id.hex, payload=CreateInvoiceIn(send_email=False), request=req,
        current_user={"user_id": user, "sub": user}, db=db,
    )
    assert resp.status_code in (200, 201), resp.body
    inv = db.get(Invoice, _holder(db, row))
    assert inv is not None and str(inv.id) == _json.loads(resp.body)["id"]
    # 1 h + 3 h = 4 h: $150 + 3 × $100.
    assert Decimal(str(inv.total)) == Decimal("450.00")
    [audit] = _claimed_audits(db, inv)
    assert audit.user_id == user
    assert audit.details["claimed_time_entries"] == 1


def test_an_accepted_estimate_job_is_unchanged(db):
    job = _job(db)
    row = _row(db, job, at=DAY1, minutes=480)
    db.add(Estimate(id=uuid4(), job_id=job.id, customer_id=job.customer_id, status="accepted",
                    estimate_number=f"EST-{uuid4().hex[:6]}", public_token=uuid4().hex,
                    company_id=TENANT))
    db.flush()
    co = _closeout(db, job, hours=2)
    assert autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co) is None
    assert db.execute(select(Invoice).where(Invoice.job_id == job.id)).first() is None
    assert _holder(db, row) is None
