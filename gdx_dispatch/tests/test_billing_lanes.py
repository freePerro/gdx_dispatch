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
