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

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.billing_lanes import (
    billed_man_hours,
    roundup_to_half,
    service_labor_line,
    service_rates,
)
from gdx_dispatch.models.pricing_engine import PricingSettings


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    PricingSettings.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("raw", "rounded"),
    [(0.25, 0.5), (2.10, 2.5), (3.00, 3.0), (3.60, 4.0), (0.0, 0.0), (1.01, 1.5)],
)
def test_roundup_to_half(raw: float, rounded: float) -> None:
    assert roundup_to_half(raw) == rounded


@pytest.mark.parametrize(
    ("hours", "techs", "man"),
    [
        (0.25, 1, 1.0),   # round 0.5, floor 1.0
        (2.10, 1, 2.5),
        (3.00, 1, 3.0),
        (3.60, 1, 4.0),
        (3.00, 2, 6.0),   # Doug's crew example
        (0.25, 2, 1.0),   # 0.5 × 2 = 1.0 — floor is a no-op, NOT 2.0:
                          # round-then-multiply-then-floor, per the plan
        (0.10, 3, 1.5),   # 0.5 × 3
    ],
)
def test_billed_man_hours_order_of_operations(hours: float, techs: int, man: float) -> None:
    assert billed_man_hours(hours, techs) == man


def test_dougs_worked_examples_price_exactly(db) -> None:
    for hours, expected in [(0.25, "100.00"), (2.10, "250.00"), (3.00, "300.00"), (3.60, "400.00")]:
        line = service_labor_line(db, hours_worked=hours, techs_on_site=1)
        assert line.line_total == Decimal(expected), (hours, line.line_total)
    crew = service_labor_line(db, hours_worked=3.0, techs_on_site=2)
    assert crew.line_total == Decimal("600.00")
    assert crew.billed_man_hours == 6.0
    assert crew.attested_hours == 3.0, "billed must never overwrite attested"


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
    # not a collapsed multiply.
    line = service_labor_line(db, hours_worked=2.1, techs_on_site=1)
    assert line.line_total == Decimal("275.00")


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
    line = service_labor_line(db, hours_worked=3.0, techs_on_site=1)
    assert line.description == "Labor: 3.0 hrs on site (1 tech) — $100.00/hr after the first"
    assert float(line.line_total) == 300.0, "template changes TEXT, never the math"


def test_blank_template_means_builtin_default(db) -> None:
    db.add(PricingSettings(service_labor_description_template="   "))
    db.commit()
    line = service_labor_line(db, hours_worked=3.0, techs_on_site=1)
    assert line.description.startswith("Service labor — 3.00 man-hours")


def test_broken_template_falls_back_never_raises(db) -> None:
    # {nope} is not a placeholder; a settings typo must never 500 a closeout,
    # an autodraft, or an invoice.
    db.add(PricingSettings(service_labor_description_template="Labor {nope} {hours"))
    db.commit()
    line = service_labor_line(db, hours_worked=2.0, techs_on_site=1)
    assert line.description.startswith("Service labor — 2.00 man-hours")
    assert float(line.line_total) == 200.0
