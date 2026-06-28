"""Unit tests for the pure overhead projection engine (ADR-016).

No DB — the engine is duck-typed over simple obligation stand-ins.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from gdx_dispatch.services.overhead_projection import (
    add_months,
    amount_for_month,
    effective_end_date,
    is_active_in_month,
    monthly_equivalent,
    project,
)


def _ob(**kw):
    base = dict(
        id=kw.get("label", "x"),
        label="x",
        category="other",
        amount=Decimal("100"),
        cadence="monthly",
        start_date=date(2025, 1, 1),
        end_date=None,
        term_total_occurrences=None,
        scheduled_changes=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── monthly-equivalent normalization ─────────────────────────────────


@pytest.mark.parametrize(
    "amount,cadence,expected",
    [
        ("100", "monthly", "100"),
        ("1200", "annual", "100"),
        ("300", "quarterly", "100"),
        ("600", "semiannual", "100"),
        ("50", "biweekly", "108.33333333333333333333333333"),  # 50*26/12
    ],
)
def test_monthly_equivalent(amount, cadence, expected):
    got = monthly_equivalent(Decimal(amount), cadence)
    assert float(got) == pytest.approx(float(Decimal(expected)))


def test_monthly_equivalent_weekly():
    # 100 * 52 / 12 = 433.33…
    assert float(monthly_equivalent(Decimal("100"), "weekly")) == pytest.approx(433.3333, abs=1e-3)


def test_monthly_equivalent_unknown_cadence():
    with pytest.raises(ValueError):
        monthly_equivalent(Decimal("1"), "fortnightly")


# ── add_months helper ────────────────────────────────────────────────


def test_add_months_wraps_year():
    assert add_months(2026, 11, 3) == (2027, 2)
    assert add_months(2026, 1, 0) == (2026, 1)
    assert add_months(2026, 12, 1) == (2027, 1)


# ── effective end date (end_date wins; term derives it) ───────────────


def test_effective_end_date_explicit_wins():
    ob = _ob(end_date=date(2027, 5, 1), term_total_occurrences=99)
    assert effective_end_date(ob) == date(2027, 5, 1)


def test_effective_end_date_from_monthly_term():
    # 12 monthly payments from Jan 2026 → last payment Dec 2026
    ob = _ob(start_date=date(2026, 1, 1), term_total_occurrences=12, cadence="monthly")
    assert effective_end_date(ob) == date(2026, 12, 1)


def test_effective_end_date_open_ended():
    assert effective_end_date(_ob()) is None


# ── active-in-month boundaries ────────────────────────────────────────


def test_not_active_before_start():
    ob = _ob(start_date=date(2026, 6, 1))
    assert not is_active_in_month(ob, 2026, 5)
    assert is_active_in_month(ob, 2026, 6)


def test_not_active_after_end():
    ob = _ob(start_date=date(2025, 1, 1), end_date=date(2026, 8, 1))
    assert is_active_in_month(ob, 2026, 8)
    assert not is_active_in_month(ob, 2026, 9)


# ── scheduled step-changes ────────────────────────────────────────────


def test_scheduled_change_applies_from_effective_month():
    ob = _ob(
        amount=Decimal("100"),
        cadence="monthly",
        scheduled_changes=[{"effective_date": "2026-07-01", "amount": "150"}],
    )
    assert amount_for_month(ob, 2026, 6) == Decimal("100")
    assert amount_for_month(ob, 2026, 7) == Decimal("150")


def test_latest_change_wins():
    ob = _ob(
        amount=Decimal("100"),
        cadence="monthly",
        scheduled_changes=[
            {"effective_date": "2026-03-01", "amount": "120"},
            {"effective_date": "2026-07-01", "amount": "150"},
        ],
    )
    assert amount_for_month(ob, 2026, 2) == Decimal("100")
    assert amount_for_month(ob, 2026, 5) == Decimal("120")
    assert amount_for_month(ob, 2026, 8) == Decimal("150")


# ── the headline: a loan paying off makes the projection step down ────


def test_projection_steps_down_when_loan_pays_off():
    rent = _ob(id="rent", label="Rent", category="rent",
               amount=Decimal("1000"), cadence="monthly", start_date=date(2025, 1, 1))
    loan = _ob(id="loan", label="Truck loan", category="loan",
               amount=Decimal("500"), cadence="monthly",
               start_date=date(2025, 1, 1), end_date=date(2026, 8, 1))

    result = project([rent, loan], anchor_year=2026, anchor_month=7, horizon_months=4)

    totals = {m["label"]: m["total"] for m in result["months"]}
    assert totals["2026-07"] == Decimal("1500.00")
    assert totals["2026-08"] == Decimal("1500.00")
    assert totals["2026-09"] == Decimal("1000.00")  # loan gone → step down
    assert totals["2026-10"] == Decimal("1000.00")

    assert result["current_monthly_total"] == Decimal("1500.00")
    assert result["horizon_total"] == Decimal("1000.00")

    assert len(result["step_downs"]) == 1
    sd = result["step_downs"][0]
    assert sd["label"] == "2026-09"
    assert sd["drop"] == Decimal("500.00")
    assert sd["ended"] == ["Truck loan"]


def test_projection_by_category_and_categories_list():
    rent = _ob(id="r", label="Rent", category="rent", amount=Decimal("1000"))
    ins = _ob(id="i", label="GL policy", category="insurance",
              amount=Decimal("1200"), cadence="annual")  # → 100/mo
    result = project([rent, ins], anchor_year=2026, anchor_month=1, horizon_months=1)
    m0 = result["months"][0]
    assert m0["total"] == Decimal("1100.00")
    assert m0["by_category"] == {"insurance": Decimal("100.00"), "rent": Decimal("1000.00")}
    assert result["categories"] == ["insurance", "rent"]


def test_empty_projection_is_zero():
    result = project([], anchor_year=2026, anchor_month=1, horizon_months=6)
    assert result["current_monthly_total"] == Decimal("0.00")
    assert len(result["months"]) == 6
    assert all(m["total"] == Decimal("0.00") for m in result["months"])
    assert result["step_downs"] == []
