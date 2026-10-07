"""Service-lane pricing math — plan §8/§11, Doug's worked examples verbatim.

The rules, decided 2026-07-29 and order-load-bearing:
    round hours UP to the next half FIRST, × techs, THEN the 1-hour floor;
    amount = first_hour_price + hourly_rate × (man_hours − 1).
Billed ≠ attested, permanently: these functions produce the CUSTOMER
quantity; hours_worked keeps the exact attested figure for payroll.

Doug's approved examples (plan §8):
    0.25 → 0.50 → floor 1.00 → $100
    2.10 → 2.50 → $250
    3.00 → 3.00 → $300      ← the job this whole effort started on
    3.60 → 4.00 → $400
Crew (plan §11): 3.0 h × 2 techs = 6.0 man-hours → $600.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.billing_lanes import (
    FIRST_HOUR_DESCRIPTION,
    first_hour_charged_elsewhere,
    job_labor_lines,
    roundup_to_half,
    service_rates,
)
from gdx_dispatch.models.pricing_engine import PricingSettings
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job, TimeEntry

SERVICE = "Service Call"


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    PricingSettings.__table__.create(bind=engine)
    for tbl in (Job, Invoice, InvoiceLine, TimeEntry):
        tbl.__table__.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _job(job_type: str = SERVICE):
    return SimpleNamespace(id=uuid4(), job_type=job_type)


def _closeout(hours: float, techs: int = 1):
    return SimpleNamespace(hours_worked=hours, techs_on_site=techs, closed_at=None)


def _total(lines) -> Decimal:
    return sum((ln["line_total"] for ln in lines), Decimal("0"))


def _service_lines(db, hours: float, techs: int = 1, **kw):
    return job_labor_lines(db, _job(), _closeout(hours, techs), day_rows=[], **kw)


@pytest.mark.parametrize(
    ("raw", "rounded"),
    [(0.25, 0.5), (2.10, 2.5), (3.00, 3.0), (3.60, 4.0), (0.0, 0.0), (1.01, 1.5)],
)
def test_roundup_to_half(raw: float, rounded: float) -> None:
    assert roundup_to_half(raw) == rounded


@pytest.mark.parametrize(
    ("hours", "techs", "man"),
    [
        (0.25, 1, "1.00"),   # round 0.5, floor 1.0
        (2.10, 1, "2.50"),
        (3.00, 1, "3.00"),
        (3.60, 1, "4.00"),
        (3.00, 2, "6.00"),   # Doug's crew example
        (0.25, 2, "1.00"),   # 0.5 × 2 = 1.0 — floor is a no-op, NOT 2.0:
                             # round-then-multiply-then-floor, per the plan
        (0.10, 3, "1.50"),   # 0.5 × 3
    ],
)
def test_billed_man_hours_order_of_operations(db, hours: float, techs: int, man: str) -> None:
    lines = _service_lines(db, hours, techs)
    assert len(lines) == 1, "equal rates bill one line"
    assert lines[0]["quantity"] == Decimal(man)


def test_dougs_worked_examples_price_exactly(db) -> None:
    for hours, expected in [(0.25, "100.00"), (2.10, "250.00"), (3.00, "300.00"), (3.60, "400.00")]:
        lines = _service_lines(db, hours)
        assert _total(lines) == Decimal(expected), (hours, lines)
    crew = _service_lines(db, 3.0, 2)
    assert _total(crew) == Decimal("600.00")
    assert crew[0]["quantity"] == Decimal("6.00")
    assert crew[0]["man_hours"] == 6.0
    assert crew[0]["estimated_man_hours"] == Decimal("6.00"), "billed must never overwrite attested"


def test_zero_attested_hours_bill_no_line(db) -> None:
    """Rule 2: nothing invents hours — no line, not a floored hour."""
    assert _service_lines(db, 0.0) == []
    assert job_labor_lines(db, _job(), None, day_rows=[]) == []


def test_install_and_office_lanes_get_no_service_labor(db) -> None:
    assert job_labor_lines(db, _job("Installation"), _closeout(3.0), day_rows=[]) == []
    assert job_labor_lines(db, _job("Office"), _closeout(3.0), day_rows=[]) == []


def test_rates_come_from_settings_and_can_diverge(db) -> None:
    db.add(
        PricingSettings(
            service_call_first_hour_price=Decimal("125"),
            service_call_hourly_rate=Decimal("100"),
        )
    )
    db.commit()
    first, hourly = service_rates(db)
    assert (first, hourly) == (Decimal("125.00"), Decimal("100.00"))
    # 2.5 man-hours: 125 + 100×1.5 = 275 — the two-part structure is real,
    # not a collapsed multiply, and it is billed as a pair of lines.
    lines = _service_lines(db, 2.1)
    assert [(ln["description"], ln["quantity"], ln["unit_price"]) for ln in lines][0] == (
        FIRST_HOUR_DESCRIPTION, Decimal("1.00"), Decimal("125.00"),
    )
    assert lines[1]["quantity"] == Decimal("1.50")
    assert lines[1]["unit_price"] == Decimal("100.00")
    assert _total(lines) == Decimal("275.00")
    # The day-row ids and the attested figure ride the hourly line only.
    assert "time_entry_ids" not in lines[0]
    assert lines[1]["time_entry_ids"] == []
    # Floored to one hour at different rates: the first-hour line alone,
    # carrying the ids itself, and no zero-quantity hourly line.
    short = _service_lines(db, 0.25)
    assert len(short) == 1
    assert short[0]["description"] == FIRST_HOUR_DESCRIPTION
    assert short[0]["time_entry_ids"] == []
    assert _total(short) == Decimal("125.00")


def test_defaults_are_one_hundred_when_unconfigured(db) -> None:
    assert service_rates(db) == (Decimal("100.00"), Decimal("100.00"))


def test_install_labor_line_flat_prices_from_matrix(db) -> None:
    """Plan §8 install lane: flat price from the picked matrix row, read live.
    A gone/inactive/$0 row → None (caller falls to office-priced)."""
    import datetime as _dt
    from uuid import uuid4

    from gdx_dispatch.core.billing_lanes import install_labor_line
    from gdx_dispatch.models.labor_pricing import LaborPriceItem

    LaborPriceItem.__table__.create(bind=db.get_bind(), checkfirst=True)
    item = LaborPriceItem(
        id=uuid4(), description="16x7 Sectional Install", service_type="install",
        flat_price=Decimal("650"), assumed_man_hours=Decimal("6.5"),
        default_crew_size=1, min_wall_clock_minutes=15, active=True,
        effective_from=_dt.date(2026, 1, 1), sort_order=1,
    )
    db.add(item)
    db.commit()

    line = install_labor_line(db, str(item.id))
    assert line is not None
    assert line.line_total == Decimal("650.00")
    assert "16x7 Sectional Install" in line.description

    item.active = False
    db.commit()
    assert install_labor_line(db, str(item.id)) is None
    assert install_labor_line(db, str(uuid4())) is None
    assert install_labor_line(db, "not-a-uuid") is None


# ---------------------------------------------------------------------------
# Editable description template (Doug 2026-08-07, migration 060): "the labor
# description is editable there but what it automatically fills in is not."
# ---------------------------------------------------------------------------


def test_custom_description_template_is_used(db) -> None:
    db.add(PricingSettings(
        service_labor_description_template=(
            "Labor: {hours:.1f} hrs on site ({techs} tech) — ${hourly_rate}/hr after the first"
        ),
    ))
    db.commit()
    (line,) = _service_lines(db, 3.0)
    assert line["description"] == "Labor: 3.0 hrs on site (1 tech) — $100.00/hr after the first"
    assert float(line["line_total"]) == 300.0, "template changes TEXT, never the math"


def test_blank_template_means_builtin_default(db) -> None:
    db.add(PricingSettings(service_labor_description_template="   "))
    db.commit()
    (line,) = _service_lines(db, 3.0)
    assert line["description"].startswith("Service labor — 3.00 man-hours")


def test_broken_template_falls_back_never_raises(db) -> None:
    # {nope} is not a placeholder; a settings typo must never 500 a closeout,
    # an autodraft, or an invoice.
    db.add(PricingSettings(service_labor_description_template="Labor {nope} {hours"))
    db.commit()
    (line,) = _service_lines(db, 2.0)
    assert line["description"].startswith("Service labor — 2.00 man-hours")
    assert float(line["line_total"]) == 200.0


def test_the_template_is_used_only_where_it_is_true(db) -> None:
    """The tenant template speaks of one day's crew and both rates on one
    line. A job with day rows gets a text stating its own quantity, rate and
    day count instead, so the line never reads hours it does not bill (D16)."""
    db.add(PricingSettings(service_labor_description_template="Labor {man_hours:.2f} h, {hours} on site"))
    db.commit()
    day1 = _dt.datetime(2026, 10, 1, 15, 0, tzinfo=_dt.timezone.utc)
    day2 = _dt.datetime(2026, 10, 2, 15, 0, tzinfo=_dt.timezone.utc)
    rows = [(uuid4(), day1, 120), (uuid4(), day2, 90)]
    lines = job_labor_lines(db, _job(), _closeout(0.0), day_rows=rows)
    (line,) = lines
    # 210 minutes → 3.5 h, rounded up once; the final day attests 0 h.
    assert line["quantity"] == Decimal("3.50")
    assert line["description"] == "Service labor — 3.50 man-hours at $100.00/hr over 2 days"
    assert line["time_entry_ids"] == [str(rows[0][0]), str(rows[1][0])]
    assert line["estimated_man_hours"] == Decimal("3.50")
    single = job_labor_lines(db, _job(), _closeout(1.0), day_rows=[])
    assert single[0]["description"] == "Labor 1.00 h, 1.0 on site"


def test_day_row_minutes_round_once_not_per_row(db) -> None:
    """Three 10-minute day rows are 30 minutes → 0.5 h, not 3 × 0.5 h; and
    the closeout's own hours round on their own before the rows are added."""
    t = _dt.datetime(2026, 10, 1, 15, 0, tzinfo=_dt.timezone.utc)
    rows = [(uuid4(), t, 10), (uuid4(), t, 10), (uuid4(), t, 10)]
    (line,) = job_labor_lines(db, _job(), _closeout(2.1), day_rows=rows)
    assert line["quantity"] == Decimal("3.00")  # 2.5 + 0.5
    assert line["estimated_man_hours"] == Decimal("2.60")  # 2.1 + 0.5 raw


def _labor_invoice(db, job_id, *, status="draft", line_deleted=False, invoice_deleted=False,
                   pricing_source="labor_attested"):
    inv = Invoice(job_id=job_id, customer_id=uuid4(), status=status, company_id="tenant-1",
                  invoice_number=f"INV-{uuid4().hex[:6]}", public_token=uuid4().hex,
                  deleted_at=_dt.datetime.now(_dt.timezone.utc) if invoice_deleted else None)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, description="Labor", quantity=Decimal("2"),
                       unit_price=Decimal("100"), line_total=Decimal("200"),
                       pricing_source=pricing_source, company_id="tenant-1",
                       deleted_at=_dt.datetime.now(_dt.timezone.utc) if line_deleted else None))
    db.commit()
    return inv


def test_first_hour_and_floor_only_once_per_job(db) -> None:
    """Rule 3: another live, non-void invoice on the job with a live attested
    labor line has charged the first hour; this one bills hourly, no floor."""
    db.add(PricingSettings(service_call_first_hour_price=Decimal("125"),
                           service_call_hourly_rate=Decimal("100")))
    db.commit()
    job = _job()
    assert not first_hour_charged_elsewhere(db, job.id)
    other = _labor_invoice(db, job.id)
    assert first_hour_charged_elsewhere(db, job.id)
    # The asking invoice is excluded from its own test.
    assert not first_hour_charged_elsewhere(db, job.id, invoice_id=other.id)

    lines = job_labor_lines(db, job, _closeout(0.25), day_rows=[])
    (line,) = lines
    assert line["quantity"] == Decimal("0.50"), "no floor once the first hour is billed"
    assert line["unit_price"] == Decimal("100.00")
    assert _total(lines) == Decimal("50.00")


@pytest.mark.parametrize("kwargs", [
    {"status": "void"},
    {"invoice_deleted": True},
    {"line_deleted": True},
    {"pricing_source": "manual"},
])
def test_first_hour_test_ignores_void_deleted_and_unattested(db, kwargs) -> None:
    job = _job()
    _labor_invoice(db, job.id, **kwargs)
    assert not first_hour_charged_elsewhere(db, job.id), kwargs


def test_quantity_times_price_rounds_half_up(db) -> None:
    db.add(PricingSettings(service_call_first_hour_price=Decimal("33.33"),
                           service_call_hourly_rate=Decimal("33.33")))
    db.commit()
    (line,) = _service_lines(db, 2.5)
    # 2.5 × 33.33 = 83.325 → 83.33 (a float product rounds the wrong way).
    assert line["line_total"] == Decimal("83.33")
