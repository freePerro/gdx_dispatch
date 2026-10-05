"""GDXA-233: the estimate PDF prints Good/Better/Best tiers.

Before this, `_estimate_payload` read `estimate.lines` only and the template
had no tier block, so an open tiered estimate printed an empty (or wrong)
table over "Total $0.00", and an accepted one printed the tier's total under
rows that were not the tier's. The same payload feeds the office /pdf, the
emailed attachment and the public /proposals/{token}/pdf.

Payload tests run on a real SQLite session; template tests assert on the HTML
WeasyPrint receives, as test_pdf_template_render.py does.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from gdx_dispatch.core import pdf_generator
from gdx_dispatch.routers import pdf as pdf_router
from gdx_dispatch.tests.test_pdf import pdf_app  # noqa: F401
from gdx_dispatch.tests.test_pdf_template_render import _BRANDING, _li_config, captured_html  # noqa: F401


def _seed_tiered(db, *, proposal_mode=True):
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine, ProposalTier, ProposalTierLine

    est = Estimate(
        estimate_number="EST-TIER-1", total=Decimal("2200.00"), status="sent",
        public_token=uuid4().hex, company_id="tenant-test", proposal_mode=proposal_mode,
        tax_rate=Decimal("0"),
    )
    db.add(est)
    db.flush()
    # The mobile builder's shape: estimate.lines carries every tier's items,
    # untagged. None of it is the customer's content on a tiered estimate.
    db.add(EstimateLine(
        estimate_id=est.id, description="UNTAGGED base line", quantity=1,
        unit_price=Decimal("999"), line_total=Decimal("999"), sort_order=1, company_id="tenant-test",
    ))
    good = ProposalTier(estimate_id=est.id, tier_name="good", description="Spring swap",
                        total_price=Decimal("450.00"), warranty_months=12, display_order=0)
    better = ProposalTier(estimate_id=est.id, tier_name="better", description="New door",
                          total_price=Decimal("2200.00"), warranty_months=60, display_order=1)
    db.add_all([good, better])
    db.flush()
    db.add_all([
        ProposalTierLine(tier_id=better.id, description="CHI 2283 16x7", category="Door", quantity=1,
                         unit_price=Decimal("1800"), line_total=Decimal("1800"), sort_order=1,
                         company_id="tenant-test"),
        ProposalTierLine(tier_id=better.id, description="Install labor", category="Labor", quantity=1,
                         unit_price=Decimal("400"), line_total=Decimal("400"), sort_order=2,
                         company_id="tenant-test"),
    ])
    db.commit()
    return est, good, better


@pytest.fixture()
def db(pdf_app):  # noqa: F811
    from gdx_dispatch.core.database import get_db

    session = pdf_app.dependency_overrides[get_db]()
    try:
        yield session
    finally:
        session.close()


def test_open_tiered_estimate_offers_every_option_at_its_own_price(db):
    est, _, _ = _seed_tiered(db)
    payload = pdf_router._estimate_payload(est, None, deposit_pct=30, db=db)
    assert payload["lines"] == []
    assert payload["accepted_tier"] is None
    assert payload["deposit_amount"] == 0.0  # no single total to take 30% of
    names = [o["name"] for o in payload["tier_options"]]
    assert names == ["Good", "Better"]
    good, better = payload["tier_options"]
    assert good["price"] == 450.0 and better["price"] == 2200.0
    # A flat tier still prints a row, never an empty table.
    assert [r["line_total"] for r in good["lines"]] == [450.0]
    assert [r["description"] for r in better["lines"]] == ["CHI 2283 16x7", "Install labor"]
    assert "UNTAGGED" not in repr(payload)


def test_accepted_line_built_tier_prints_its_lines_and_its_total(db):
    est, _, better = _seed_tiered(db)
    est.accepted_tier_id = better.id
    est.status = "accepted"
    db.commit()
    payload = pdf_router._estimate_payload(est, None, db=db)
    assert payload["tier_options"] == []
    assert payload["accepted_tier"] == {"name": "Better", "description": "New door"}
    assert [r["description"] for r in payload["lines"]] == ["CHI 2283 16x7", "Install labor"]
    assert sum(r["line_total"] for r in payload["lines"]) == payload["subtotal"] == 2200.0


def test_accepted_flat_tier_prints_one_row_at_its_price(db):
    est, good, _ = _seed_tiered(db)
    est.accepted_tier_id = good.id
    est.total = Decimal("450.00")
    db.commit()
    payload = pdf_router._estimate_payload(est, None, db=db)
    assert payload["accepted_tier"]["name"] == "Good"
    assert [(r["description"], r["line_total"]) for r in payload["lines"]] == [("Good option", 450.0)]
    assert payload["total"] == 450.0


def test_non_proposal_estimate_still_prints_its_own_lines(db):
    est, _, _ = _seed_tiered(db, proposal_mode=False)
    payload = pdf_router._estimate_payload(est, None, db=db)
    assert payload["tier_options"] == [] and payload["accepted_tier"] is None
    assert [r["description"] for r in payload["lines"]] == ["UNTAGGED base line"]


def _options_data(**overrides):
    data = {
        "estimate_number": "EST-TIER-1",
        "customer": {"name": "Acme", "address": "1 Way"},
        "lines": [], "accepted_tier": None,
        "tier_options": [
            {"name": "Good", "description": "Spring swap", "warranty_months": 12, "price": 450.0,
             "hide_line_prices": False,
             "lines": [{"description": "Good option", "category": "", "quantity": 1,
                        "unit_price": 450.0, "line_total": 450.0}]},
            {"name": "Better", "description": "New door", "warranty_months": 0, "price": 2200.0,
             "hide_line_prices": False,
             "lines": [{"description": "CHI 2283 16x7", "category": "Door", "quantity": 1,
                        "unit_price": 1800.0, "line_total": 1800.0},
                       {"description": "Install labor", "category": "Labor", "quantity": 1,
                        "unit_price": 400.0, "line_total": 400.0}]},
        ],
        "subtotal": 0.0, "discount": 0.0, "tax": 0.0, "tax_rate_pct": 0.0, "total": 0.0,
        "hide_line_prices": False, "deposit_pct": 0, "deposit_amount": 0.0,
        "terms": "", "notes": "", "attachment_images": [], "attachment_files": [],
    }
    data.update(overrides)
    return data


def test_open_options_render_without_a_zero_total(captured_html):  # noqa: F811
    pdf_generator.generate_estimate_pdf(_options_data(), _BRANDING)
    html = captured_html["html"]
    assert "Option 1: Good" in html and "Option 2: Better" in html
    assert "$2,200.00" in html and "Install labor" in html
    assert "Warranty: 12 months" in html
    assert "Choose one option" in html
    assert 'class="totals"' not in html  # the $0.00 Total block is gone


def test_open_options_honor_hidden_prices_and_grouping(captured_html):  # noqa: F811
    data = _options_data()
    for option in data["tier_options"]:
        option["hide_line_prices"] = True
    pdf_generator.generate_estimate_pdf(
        data, _BRANDING,
        template_config=_li_config(show_category=True, category_display="grouped"),
    )
    html = captured_html["html"]
    assert "Unit Price" not in html and "$1,800.00" not in html
    assert "$2,200.00" in html  # the option's own price stays, as on the public page
    assert html.count('class="cat-row"') == 2  # Door + Labor in the Better option


def test_accepted_tier_heading_over_its_lines(captured_html):  # noqa: F811
    data = _options_data(
        tier_options=[], accepted_tier={"name": "Better", "description": "New door"},
        lines=_options_data()["tier_options"][1]["lines"], subtotal=2200.0, total=2200.0,
    )
    pdf_generator.generate_estimate_pdf(data, _BRANDING)
    html = captured_html["html"]
    assert "Accepted option: Better" in html
    assert html.index("Accepted option: Better") < html.index("CHI 2283 16x7")
    assert 'class="totals"' in html and "2200.00" in html
