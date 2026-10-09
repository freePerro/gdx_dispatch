"""Resale-quote markup math (modules/reseller/service.build_snapshot).

Pure: the input is the payload routers/pdf._estimate_payload builds, the output
is what the branded PDF prints and the two numbers the reseller tracks.
"""
from __future__ import annotations

from decimal import Decimal

from gdx_dispatch.modules.reseller.service import build_snapshot


def _payload(**over):
    p = {
        "lines": [
            {"description": "16x7 insulated door", "category": "Doors", "quantity": 1,
             "unit_price": 1234.57, "line_total": 1234.57},
            {"description": "Struts", "category": "", "quantity": 3, "unit_price": 33.33, "line_total": 99.99},
        ],
        "subtotal": 1334.56,
        "discount": 0.0,
        "tax": 98.12,
        "tax_rate_pct": 7.375,
        "total": 1432.68,
        "deposit_pct": 50,
        "deposit_amount": 716.34,
        "terms": "OUR TERMS",
        "notes": "OUR NOTES",
        "hide_line_prices": False,
        "accepted_tier": None,
        "tier_options": [],
    }
    p.update(over)
    return p


def test_zero_markup_reproduces_our_numbers_exactly():
    out = build_snapshot(_payload(), Decimal("0"))
    snap = out["snapshot"]
    assert [r["line_total"] for r in snap["lines"]] == ["1234.57", "99.99"]
    assert [r["unit_price"] for r in snap["lines"]] == ["1234.57", "33.33"]
    assert out["base_subtotal"] == out["resale_subtotal"] == Decimal("1334.56")
    assert snap["total"] == "1334.56"


def test_marked_lines_sum_to_the_resale_subtotal_with_per_line_rounding():
    out = build_snapshot(_payload(), Decimal("25"))
    snap = out["snapshot"]
    # 1234.57 x 1.25 = 1543.2125 -> 1543.21 ; struts 33.33 x 1.25 = 41.6625 -> 41.66, x 3 = 124.98
    assert [r["line_total"] for r in snap["lines"]] == ["1543.21", "124.98"]
    assert sum(Decimal(r["line_total"]) for r in snap["lines"]) == out["resale_subtotal"] == Decimal("1668.19")
    assert out["base_subtotal"] == Decimal("1334.56")


def test_a_discount_marks_up_our_discounted_total():
    out = build_snapshot(_payload(discount=100), Decimal("20"))
    snap = out["snapshot"]
    # Their total is ours after discount x 1.2 (1234.56 -> 1481.47); the
    # discount is the gap to the lines rounded per unit (1601.48), a cent off 120.
    assert snap["discount"] == "120.01"
    assert out["resale_subtotal"] == Decimal("1481.47")
    assert out["base_subtotal"] == Decimal("1234.56")
    assert out["resale_subtotal"] == Decimal(snap["subtotal"]) - Decimal(snap["discount"])
    assert snap["total"] == str(out["resale_subtotal"])


def test_no_tax_deposit_terms_or_notes_reach_the_snapshot():
    snap = build_snapshot(_payload(), Decimal("10"))["snapshot"]
    flat = repr(snap)
    for key in ("tax", "tax_rate_pct", "deposit_pct", "deposit_amount", "terms", "notes"):
        assert key not in snap
    assert "OUR TERMS" not in flat and "OUR NOTES" not in flat
    # Categories are ours (the catalog's); only description and quantity travel.
    assert all(set(r) == {"description", "quantity", "unit_price", "line_total"} for r in snap["lines"])


def test_hidden_line_prices_yield_a_totals_only_snapshot():
    out = build_snapshot(_payload(hide_line_prices=True), Decimal("25"))
    snap = out["snapshot"]
    assert snap["lines_priced"] is False
    assert all("unit_price" not in r and "line_total" not in r for r in snap["lines"])
    assert out["hide_line_prices"] is True
    # Totals still exist: our subtotal x 1.25.
    assert out["resale_subtotal"] == Decimal("1668.20")


def test_lines_that_do_not_add_up_to_our_subtotal_print_without_prices():
    """A hand-set total: a marked table that cannot sum to its own Total is
    dropped, and the resale subtotal comes from our subtotal instead."""
    out = build_snapshot(_payload(subtotal=1500), Decimal("10"))
    assert out["snapshot"]["lines_priced"] is False
    assert out["resale_subtotal"] == Decimal("1650.00")


def test_an_accepted_tier_carries_its_name_as_the_heading():
    snap = build_snapshot(_payload(accepted_tier={"name": "Better", "description": "x"}), Decimal("0"))["snapshot"]
    assert snap["heading"] == "Better"


def test_open_options_are_marked_up_per_option():
    options = [
        {"name": "Good", "description": "Basic", "warranty_months": 12, "price": 1000.0,
         "lines": [{"description": "Door", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}]},
        {"name": "Best", "description": "", "warranty_months": 60, "price": 2000.01,
         "lines": [
             {"description": "Door", "quantity": 1, "unit_price": 1900.0, "line_total": 1900.0},
             {"description": "Opener", "quantity": 1, "unit_price": 100.01, "line_total": 100.01},
         ]},
    ]
    out = build_snapshot(_payload(lines=[], tier_options=options), Decimal("15"))
    snap = out["snapshot"]
    assert snap["kind"] == "options"
    assert out["base_subtotal"] is None and out["resale_subtotal"] is None
    good, best = snap["options"]
    assert (good["base_price"], good["price"]) == ("1000.00", "1150.00")
    # 1900 x 1.15 = 2185.00 ; 100.01 x 1.15 = 115.0115 -> 115.01
    assert [r["line_total"] for r in best["lines"]] == ["2185.00", "115.01"]
    assert best["price"] == "2300.01"
    assert best["warranty_months"] == 60


def test_options_with_hidden_prices_keep_only_the_option_price():
    options = [{"name": "Good", "description": "", "warranty_months": 0, "price": 1000.0,
                "lines": [{"description": "Door", "quantity": 1, "unit_price": 1000.0, "line_total": 1000.0}]}]
    snap = build_snapshot(_payload(lines=[], tier_options=options, hide_line_prices=True), Decimal("10"))["snapshot"]
    opt = snap["options"][0]
    assert opt["lines_priced"] is False and "line_total" not in opt["lines"][0]
    assert opt["price"] == "1100.00"


def test_a_discount_bigger_than_the_subtotal_floors_at_zero_like_ours():
    # 20% and 25% round the struts differently, so the marked lines and the
    # marked discount differ by cents both ways: neither may leave a balance.
    for pct in ("20", "25"):
        out = build_snapshot(_payload(discount=5000), Decimal(pct))
        assert out["resale_subtotal"] == Decimal("0.00"), pct
    out = build_snapshot(_payload(discount=5000), Decimal("20"))
    assert out["base_subtotal"] == Decimal("0.00")
    assert out["resale_subtotal"] == Decimal("0.00")
    assert out["snapshot"]["total"] == "0.00"


def test_every_priced_row_multiplies_out_after_markup():
    """6 x 1.03 at 50%: rounding unit and line apart gives 1.55 and 9.27, and
    6 x 1.55 is 9.30. The printed row has to multiply out."""
    lines = [
        {"description": "Rollers", "quantity": 6, "unit_price": 1.03, "line_total": 6.18},
        {"description": "Struts", "quantity": 3, "unit_price": 33.33, "line_total": 99.99},
    ]
    out = build_snapshot(_payload(lines=lines, subtotal=106.17), Decimal("50"))
    rows = out["snapshot"]["lines"]
    assert out["snapshot"]["lines_priced"] is True
    for r in rows:
        assert Decimal(r["unit_price"]) * Decimal(str(r["quantity"])) == Decimal(r["line_total"])
    assert rows[0]["line_total"] == "9.30"
    assert sum(Decimal(r["line_total"]) for r in rows) == out["resale_subtotal"]


def test_a_row_that_does_not_multiply_out_on_ours_is_scaled_as_a_line():
    """A hand-set line total (qty 2 at 10.00, line 15.00) is ours to print as
    is; theirs scales the line rather than inventing 2 x unit."""
    lines = [{"description": "Bundle", "quantity": 2, "unit_price": 10.0, "line_total": 15.0}]
    out = build_snapshot(_payload(lines=lines, subtotal=15.0), Decimal("10"))
    assert out["snapshot"]["lines"][0]["line_total"] == "16.50"


def test_a_discount_never_prices_their_customer_below_what_they_pay_us():
    """Scaling the discount apart from lines rounded per unit: 100 x 1.00 less
    50 at 0.4% came to 49.80, and 10 x 9.94 less 99 at 10% earned nothing."""
    cases = [
        ([{"description": "Clips", "quantity": 100, "unit_price": 1.00, "line_total": 100.00}], 100.00, 50, "0.4"),
        ([{"description": "Bolts", "quantity": 10, "unit_price": 9.94, "line_total": 99.40}], 99.40, 99, "10"),
    ]
    for lines, subtotal, discount, pct in cases:
        out = build_snapshot(_payload(lines=lines, subtotal=subtotal, discount=discount), Decimal(pct))
        base = out["base_subtotal"]
        assert out["resale_subtotal"] >= base, pct
        assert out["resale_subtotal"] == (base * (1 + Decimal(pct) / 100)).quantize(Decimal("0.01")), pct
        assert Decimal(out["snapshot"]["discount"]) >= 0, pct


def test_no_discount_prints_no_discount():
    snap = build_snapshot(_payload(), Decimal("25"))["snapshot"]
    assert snap["discount"] == "0.00"


def test_lines_rounding_below_the_target_print_no_discount_and_stay_above_cost():
    """100 x 1.00 less 0.10 at 0.4%: the units round back to 1.00, so the
    lines (100.00) are under 99.90 x 1.004 (100.30). No discount prints."""
    lines = [{"description": "Clips", "quantity": 100, "unit_price": 1.00, "line_total": 100.00}]
    out = build_snapshot(_payload(lines=lines, subtotal=100.00, discount=0.10), Decimal("0.4"))
    assert out["snapshot"]["discount"] == "0.00"
    assert out["resale_subtotal"] == Decimal("100.00") >= out["base_subtotal"]
