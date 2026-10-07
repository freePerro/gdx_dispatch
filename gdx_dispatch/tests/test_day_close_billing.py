"""Multi-day jobs PR 3, billing (plan §5.4a "Billing", Doug's R-P1).

The days a crew closed with "No" are billed once, at the end, on ONE line:
"Labor — earlier visits". Its man-hours are every day row summed across days
and people, rounded UP to the half hour ONCE, at the plain hourly rate — no
first-hour price and no 1 h floor (those belong to the final day's line only).

Every test here runs the real builder against a real ORM-built SQLite database
and reads back the invoice lines it wrote.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core import closeout_billing as cb
from gdx_dispatch.core.closeout_billing import (
    AUTODRAFT_LINE_SOURCE,
    autodraft_invoice_for_closeout,
    build_closeout_lines,
    earlier_visits_line,
    release_untouched_autodraft,
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
    # Distinct prices so a first-hour charge on the earlier-visits line shows.
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


def _earlier(lines) -> list[InvoiceLine]:
    return [ln for ln in lines if ln.description.startswith("Labor — earlier visits")]


# ── the line ─────────────────────────────────────────────────────────


def test_day_rows_of_3h_and_4_2h_give_one_7_5_man_hour_line_at_the_hourly_rate(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)       # day 1, the tech
    _row(db, job, at=DAY2, minutes=252)       # day 2, a helper (4.2 h)
    co = _closeout(db, job, hours=2)
    inv = _invoice(db, job)

    added, total, _taxable = _build(db, job, co, inv)
    lines = _lines(db, inv)
    [earlier] = _earlier(lines)

    # 7.2 h rounded up ONCE to 7.5, at $100/hr: $750. Not $800 (a first-hour
    # price of $150 + 6.5 × $100), and no per-day rounding (3.0 + 4.5 = 7.5
    # happens to agree here; see the next test for where it would not).
    assert Decimal(str(earlier.line_total)) == Decimal("750.00")
    assert Decimal(str(earlier.unit_price)) == Decimal("750.00")
    assert earlier.quantity == 1
    assert earlier.description.startswith("Labor — earlier visits — 7.50 man-hours at $100.00/hr")
    assert "first hour" not in earlier.description
    # The raw attested man-hours are the record; the rounding is billing's.
    assert Decimal(str(earlier.estimated_man_hours)) == Decimal("7.20")
    assert earlier.labor_source == "attested"
    assert earlier.source == AUTODRAFT_LINE_SOURCE
    assert earlier.category == "Labor"
    # The final-day line is there too, priced as it always was.
    assert added == 2
    assert total == Decimal("750.00") + Decimal("250.00")


def test_rounding_is_once_over_the_sum_not_per_day(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=70)    # 1.17 h — per-day rounding would bill 1.5
    _row(db, job, at=DAY2, minutes=70)    # 1.17 h — and again 1.5, = 3.0
    line = earlier_visits_line(db, job)
    # 140 min = 2.33 h → 2.5 once.
    assert line["line_total"] == 250.0
    assert line["man_hours"] == 2.33


def test_a_single_short_day_has_no_one_hour_floor(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=20)
    line = earlier_visits_line(db, job)
    assert line["line_total"] == 50.0  # 0.5 h × $100, not a 1 h minimum


def test_only_day_rows_count(db):
    """A consumed timer (0 min, marker set), an ordinary job timer with no
    marker, a soft-deleted row and an unclosed row add nothing."""
    job = _job(db)
    _row(db, job, at=DAY1, minutes=60)
    _row(db, job, at=DAY1, minutes=0)                      # consumed at 0
    _row(db, job, at=DAY1, minutes=600, day_closed=False)  # a plain timer
    _row(db, job, at=DAY1, minutes=600, deleted=True)
    _row(db, job, at=DAY1, minutes=600, clock_out=False)
    other = _job(db)
    _row(db, other, at=DAY1, minutes=600)                  # another job's day
    assert earlier_visits_line(db, job)["line_total"] == 100.0


def test_no_day_rows_is_none(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=0)
    assert earlier_visits_line(db, job) is None
    assert earlier_visits_line(db, job.id, job_type=SERVICE_CALL) is None


def test_the_line_has_the_suggestion_labor_line_shape(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)
    line = earlier_visits_line(db, job.id, job_type=SERVICE_CALL)
    assert set(line) == {
        "description", "quantity", "unit_price", "line_total", "source",
        "labor_price_item_id", "man_hours",
    }
    # `source` is labor provenance there: the picker only accepts "attested".
    assert line["source"] == "attested"
    assert line["labor_price_item_id"] is None
    assert line["man_hours"] == 3.0
    assert line["unit_price"] == line["line_total"] == 300.0


def test_install_lane_gets_no_earlier_visits_line(db):
    job = _job(db, job_type=INSTALLATION)
    _row(db, job, at=DAY1, minutes=480)
    assert earlier_visits_line(db, job) is None
    co = _closeout(db, job, hours=4)
    inv = _invoice(db, job)
    _build(db, job, co, inv)
    assert _earlier(_lines(db, inv)) == []


# ── build_closeout_lines ─────────────────────────────────────────────


def test_a_final_day_of_zero_hours_still_bills_the_earlier_days(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=360)
    co = _closeout(db, job, hours=0)
    inv = _invoice(db, job)
    added, total, _t = _build(db, job, co, inv)
    lines = _lines(db, inv)
    assert added == 1
    assert [ln.description.split(" — ")[1] for ln in lines] == ["earlier visits"]
    assert total == Decimal("600.00")


def test_the_final_day_line_is_unchanged_by_day_rows(db):
    plain = _job(db)
    co_plain = _closeout(db, plain, hours=2.25, techs=2)
    inv_plain = _invoice(db, plain)
    _build(db, plain, co_plain, inv_plain)

    multi = _job(db)
    _row(db, multi, at=DAY1, minutes=480)
    co_multi = _closeout(db, multi, hours=2.25, techs=2)
    inv_multi = _invoice(db, multi)
    _build(db, multi, co_multi, inv_multi)

    final_plain = _snapshot(_lines(db, inv_plain))
    final_multi = _snapshot([ln for ln in _lines(db, inv_multi) if ln not in _earlier(_lines(db, inv_multi))])
    assert final_multi == final_plain
    # And it still carries the first-hour price: 2.5 h × 2 = 5 man-hours,
    # $150 + 4 × $100.
    assert final_plain[0][3] == Decimal("550.00")


def test_a_job_with_no_day_rows_is_byte_for_byte_unchanged(db, monkeypatch):
    """Same job, same closeout, built once by this PR's builder and once with
    the earlier-visits step removed (the builder as it was): identical
    return values and identical rows, field for field."""
    job = _job(db)
    _row(db, job, at=DAY1, minutes=480, day_closed=False)  # a plain closed timer
    _row(db, job, at=DAY1, minutes=0)                      # a consumed timer
    co = _closeout(db, job, hours=1.75, techs=1)

    inv_now = _invoice(db, job)
    now = _build(db, job, co, inv_now)
    snap_now = _snapshot(_lines(db, inv_now))

    monkeypatch.setattr(cb, "earlier_visits_line", lambda *a, **k: None)
    inv_before = _invoice(db, job)
    before = _build(db, job, co, inv_before)
    snap_before = _snapshot(_lines(db, inv_before))

    assert now == before
    assert snap_now == snap_before
    assert len(snap_now) == 1 and snap_now[0][0].startswith("Service labor")


def test_the_line_follows_the_labor_taxable_flag(db, monkeypatch):
    import gdx_dispatch.modules.proposals.totals as totals

    job = _job(db)
    _row(db, job, at=DAY1, minutes=60)
    co = _closeout(db, job, hours=1)

    inv = _invoice(db, job)
    _added, _total, taxable = _build(db, job, co, inv)
    [earlier] = _earlier(_lines(db, inv))
    assert earlier.taxable is False
    assert taxable == Decimal("0")

    monkeypatch.setattr(totals, "_load_tax_labor_flag", lambda _db: True)
    inv2 = _invoice(db, job)
    _added, total2, taxable2 = _build(db, job, co, inv2)
    [earlier2] = _earlier(_lines(db, inv2))
    assert earlier2.taxable is True
    # Same flag as the final-day line: both labor lines taxed, nothing else.
    assert taxable2 == total2 == Decimal("100.00") + Decimal("150.00")


# ── the autodraft ────────────────────────────────────────────────────


def test_the_autodraft_carries_the_line_and_a_re_closeout_rebuilds_it(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=180)
    co = _closeout(db, job, hours=1)
    inv = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co)
    assert inv is not None
    [first] = _earlier(_lines(db, inv))
    assert Decimal(str(first.line_total)) == Decimal("300.00")
    assert Decimal(str(inv.total)) == Decimal("450.00")  # 300 + the $150 first hour

    # A day row lands late (another "No" restated), then the job is re-closed.
    _row(db, job, at=DAY2, minutes=60)
    emptied = release_untouched_autodraft(db, job=job)
    assert emptied is inv
    assert _lines(db, inv) == []   # the machine owned the line, so it released it
    co.superseded_at = DAY3
    co2 = _closeout(db, job, hours=1)
    rebuilt = autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co2, reuse_invoice=emptied)
    assert rebuilt is inv
    [second] = _earlier(_lines(db, inv))
    assert Decimal(str(second.line_total)) == Decimal("400.00")
    assert second.id != first.id


def test_an_accepted_estimate_job_is_unchanged(db):
    job = _job(db)
    _row(db, job, at=DAY1, minutes=480)
    db.add(Estimate(id=uuid4(), job_id=job.id, customer_id=job.customer_id, status="accepted",
                    estimate_number=f"EST-{uuid4().hex[:6]}", public_token=uuid4().hex,
                    company_id=TENANT))
    db.flush()
    co = _closeout(db, job, hours=2)
    assert autodraft_invoice_for_closeout(db, tenant_id=TENANT, job=job, closeout=co) is None
    assert db.execute(select(Invoice).where(Invoice.job_id == job.id)).first() is None
