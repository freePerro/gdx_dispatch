"""PDF Template Editor config → rendered PDF wiring.

The editor (Settings → PDF Templates) stores a per-tenant config row that
pdf_generator now consumes. These tests pin the two contracts that matter:

1. template_config=None reproduces the legacy layout exactly — most tenants
   never saved a template and their PDFs must not change.
2. A saved config actually drives the output: brand color, font, header/
   footer, block visibility + order, and the new line-item options
   (category column/grouped, non-taxable marker).

Rendering is asserted on the HTML WeasyPrint receives (FakeHTML capture),
same approach as test_pdf.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from gdx_dispatch.core import pdf_generator
from gdx_dispatch.routers import pdf as pdf_router

# Reuse the SQLite app fixture (creates pdf_templates via shared metadata).
from gdx_dispatch.tests.test_pdf import pdf_app  # noqa: F401  (pytest fixture)


@pytest.fixture()
def captured_html(monkeypatch):
    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\nfake"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)
    return captured


def _estimate_data(**overrides):
    data = {
        "estimate_number": "EST-1",
        "customer": {"name": "Acme", "address": "1 Way"},
        "jobsite_address": "",
        "description": "",
        "lines": [
            {"description": "CHI 2283 16x7", "category": "Door", "quantity": 1,
             "unit_price": 1200.0, "line_total": 1200.0},
            {"description": "Torsion springs", "category": "Parts", "quantity": 1,
             "unit_price": 180.0, "line_total": 180.0},
            {"description": "Install labor", "category": "Labor", "quantity": 1,
             "unit_price": 450.0, "line_total": 450.0},
        ],
        "subtotal": 1830.0, "discount": 0.0, "tax": 128.10, "tax_rate_pct": 7.0,
        "total": 1958.10, "hide_line_prices": False, "deposit_pct": 0,
        "deposit_amount": 0.0, "terms": "Net 30", "notes": "note here",
        "attachment_images": [], "attachment_files": [],
    }
    data.update(overrides)
    return data


def _invoice_data(**overrides):
    data = {
        "invoice_number": "INV-1",
        "customer": {"name": "Acme", "address": "1 Way"},
        "lines": [
            {"description": "Door section", "category": "Door", "taxable": True,
             "quantity": 1, "unit_price": 1200.0, "line_total": 1200.0},
            {"description": "Install labor", "category": "Labor", "taxable": False,
             "quantity": 1, "unit_price": 450.0, "line_total": 450.0},
        ],
        "subtotal": 1650.0, "tax": 88.56, "tax_rate_pct": 7.38, "total": 1738.56,
        "balance_due": 1738.56, "status": "sent", "due_date": "2026-08-01",
        "terms": "Due on receipt", "hide_line_prices": False,
    }
    data.update(overrides)
    return data


_BRANDING = {"company_name": "GDX", "address": "HQ", "logo": "",
             "primary_color": "#114488", "secondary_color": "#22aa77"}


def _blocks(**tweaks):
    """Default 8 blocks; tweaks = {block_type: {field: value}}."""
    blocks = pdf_generator.default_blocks("estimate")
    for block in blocks:
        for field, value in (tweaks.get(block["type"]) or {}).items():
            block[field] = value
    return blocks


# ---------------------------------------------------------------------------
# 1. Legacy parity — no saved template
# ---------------------------------------------------------------------------

def test_no_config_estimate_keeps_signature_and_flat_columns(captured_html):
    pdf_generator.generate_estimate_pdf(_estimate_data(), _BRANDING)
    html = captured_html["html"]
    assert "Customer Signature" in html
    assert "<th>Category</th>" not in html
    assert "non-taxable" not in html
    assert "#114488" in html  # branding primary still the accent
    assert "Arial, sans-serif" in html  # legacy font stack
    # Legacy section ORDER, not just presence (audit catch): the old template
    # printed Terms before Notes, and both after the totals.
    assert html.index("Net 30") < html.index("note here")


def test_no_config_estimate_signature_prints_after_attachments(captured_html):
    """Legacy layout pinned by order: the signature line is dead last, below
    the attachment sections — an estimate with photos must not move the
    signature above the photo grid (audit catch)."""
    data = _estimate_data(
        attachment_images=[{"src": "file:///tmp/pic.png", "name": "pic.png"}],
        attachment_files=[{"name": "spec-sheet.pdf"}],
    )
    pdf_generator.generate_estimate_pdf(data, _BRANDING)
    html = captured_html["html"]
    assert html.index("Customer Signature") > html.index("Attached Photos")
    assert html.index("Customer Signature") > html.index("spec-sheet.pdf")


def test_no_config_invoice_has_no_signature_or_marker(captured_html):
    pdf_generator.generate_invoice_pdf(_invoice_data(), _BRANDING)
    html = captured_html["html"]
    assert "Customer Signature" not in html
    assert "non-taxable" not in html
    assert "<th>Category</th>" not in html


# ---------------------------------------------------------------------------
# 2. Config drives the output
# ---------------------------------------------------------------------------

def test_brand_color_font_header_footer(captured_html):
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING,
        template_config={
            # ColorPicker saves without '#' — must still be honored
            "brand_color": "0057a8",
            "font_family": "Georgia",
            "header_content": "Quality doors since 1999",
            "footer_content": "Thank you for your business!",
            "blocks": _blocks(),
        },
    )
    html = captured_html["html"]
    assert "#0057a8" in html and "#114488" not in html
    assert "Georgia" in html
    assert "Quality doors since 1999" in html
    assert "Thank you for your business!" in html


def test_hidden_blocks_disappear(captured_html):
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING,
        template_config={"blocks": _blocks(
            terms={"visible": False},
            signature={"visible": False},
            logo={"visible": False},
        )},
    )
    html = captured_html["html"]
    assert "Net 30" not in html
    assert "Customer Signature" not in html
    assert "note here" in html  # notes block untouched


def test_block_reorder_is_respected(captured_html):
    blocks = _blocks()
    for block in blocks:
        if block["type"] == "notes":
            block["order"] = 3.5  # move notes above line_items
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING, template_config={"blocks": blocks},
    )
    html = captured_html["html"]
    assert html.index("note here") < html.index("Torsion springs")


def test_invalid_color_and_font_fall_back_safely(captured_html):
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING,
        template_config={
            "brand_color": "red; } body { display:none }",
            "font_family": "Wingdings",
            "blocks": [],
        },
    )
    html = captured_html["html"]
    assert "display:none" not in html
    assert "#114488" in html  # fell back to branding primary
    assert "Arial, sans-serif" in html  # fell back to legacy stack


# ---------------------------------------------------------------------------
# 3. Line-item options (Phase 2)
# ---------------------------------------------------------------------------

def _li_config(**settings):
    return {"blocks": _blocks(line_items={"settings": settings})}


def test_category_column(captured_html):
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING,
        template_config=_li_config(show_category=True, category_display="column"),
    )
    html = captured_html["html"]
    assert "<th>Category</th>" in html
    assert ">Door<" in html and ">Parts<" in html


def test_category_grouped_keeps_first_appearance_order(captured_html):
    data = _estimate_data(lines=[
        {"description": "Spring", "category": "Parts", "quantity": 1, "unit_price": 1, "line_total": 1},
        {"description": "Door panel", "category": "Door", "quantity": 1, "unit_price": 1, "line_total": 1},
        {"description": "Cable", "category": "Parts", "quantity": 1, "unit_price": 1, "line_total": 1},
    ])
    pdf_generator.generate_estimate_pdf(
        data, _BRANDING,
        template_config=_li_config(show_category=True, category_display="grouped"),
    )
    html = captured_html["html"]
    assert "<th>Category</th>" not in html
    assert html.count('class="cat-row"') == 2  # Parts + Door, not one per line
    # first-appearance order (Parts first), both Parts lines under one heading
    assert html.index(">Parts<") < html.index(">Door<")
    assert html.index("Spring") < html.index("Cable") < html.index("Door panel")


def test_uncategorized_lines_get_no_group_heading(captured_html):
    data = _estimate_data(lines=[
        {"description": "Misc item", "category": "", "quantity": 1, "unit_price": 1, "line_total": 1},
    ])
    pdf_generator.generate_estimate_pdf(
        data, _BRANDING,
        template_config=_li_config(show_category=True, category_display="grouped"),
    )
    assert 'class="cat-row"' not in captured_html["html"]


def test_taxable_marker_only_on_nontaxable_invoice_lines(captured_html):
    pdf_generator.generate_invoice_pdf(
        _invoice_data(), _BRANDING,
        template_config=_li_config(show_taxable_marker=True),
    )
    html = captured_html["html"]
    # exactly one marker, and it sits on the labor line (the last line),
    # not on the taxable door line
    assert html.count("non-taxable") == 1
    assert html.index("non-taxable") > html.index("Install labor") > html.index("Door section")


def test_taxable_marker_never_renders_on_estimates(captured_html):
    # Estimate lines carry no taxable key — the marker must not appear even
    # if the setting is stored on the estimate template.
    pdf_generator.generate_estimate_pdf(
        _estimate_data(), _BRANDING,
        template_config=_li_config(show_taxable_marker=True),
    )
    assert "non-taxable" not in captured_html["html"]


def test_hide_line_prices_still_gates_price_columns(captured_html):
    pdf_generator.generate_estimate_pdf(
        _estimate_data(hide_line_prices=True), _BRANDING,
        template_config=_li_config(show_category=True, category_display="column"),
    )
    html = captured_html["html"]
    assert "Unit Price" not in html and "Line Total" not in html
    assert "<th>Category</th>" in html


# ---------------------------------------------------------------------------
# 4. Config loading + payload fields (router layer)
# ---------------------------------------------------------------------------

def _seed_template_row(db, template_type="estimate", **overrides):
    from gdx_dispatch.models.tenant_models import PdfTemplate

    row = PdfTemplate(
        id=str(uuid4()),
        company_id="tenant-test",
        template_type=template_type,
        brand_color=overrides.get("brand_color", "#ff6600"),
        font_family=overrides.get("font_family", "Georgia"),
        header_content=overrides.get("header_content", "HDR"),
        footer_content=overrides.get("footer_content", "FTR"),
        blocks=overrides.get("blocks", json.dumps(pdf_generator.default_blocks(template_type))),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.commit()
    return row


def test_template_config_loader_roundtrip(pdf_app):
    from gdx_dispatch.core.database import get_db

    db = pdf_app.dependency_overrides[get_db]()
    try:
        assert pdf_router._template_config(db, "estimate") is None  # nothing saved
        _seed_template_row(db)
        cfg = pdf_router._template_config(db, "estimate")
    finally:
        db.close()
    assert cfg["brand_color"] == "#ff6600"
    assert cfg["font_family"] == "Georgia"
    assert cfg["header_content"] == "HDR"
    assert isinstance(cfg["blocks"], list) and len(cfg["blocks"]) == 8


def test_template_config_loader_tolerates_bad_blocks_json(pdf_app):
    from gdx_dispatch.core.database import get_db

    db = pdf_app.dependency_overrides[get_db]()
    try:
        _seed_template_row(db, blocks="{not json")
        cfg = pdf_router._template_config(db, "estimate")
    finally:
        db.close()
    assert cfg["blocks"] is None  # renderer falls back to defaults


def test_estimate_endpoint_passes_saved_config(pdf_app, monkeypatch):
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.tests.test_pdf import _seed_documents_data

    estimate_id, _ = _seed_documents_data(pdf_app)
    captured: dict[str, object] = {}

    def fake_generate(estimate_data, tenant_branding, template_config=None):
        captured["config"] = template_config
        return b"%PDF-1.7\nx"

    monkeypatch.setattr(pdf_router, "generate_estimate_pdf", fake_generate)
    db = pdf_app.dependency_overrides[get_db]()
    try:
        _seed_template_row(db)
        pdf_router.estimate_pdf(UUID(estimate_id), db=db)
    finally:
        db.close()
    assert captured["config"]["brand_color"] == "#ff6600"


def test_estimate_payload_lines_include_category(pdf_app):
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine

    db = pdf_app.dependency_overrides[get_db]()
    try:
        est = Estimate(
            estimate_number="EST-CAT-1", total=Decimal("100.00"), status="draft",
            public_token=uuid4().hex, company_id="tenant-test",
        )
        db.add(est)
        db.flush()
        db.add(EstimateLine(
            estimate_id=est.id, description="Spring", category="Parts",
            quantity=1, unit_price=Decimal("100.00"), line_total=Decimal("100.00"),
            sort_order=1, company_id="tenant-test",
        ))
        db.commit()
        payload = pdf_router._estimate_payload(est, None, db=db)
    finally:
        db.close()
    assert payload["lines"][0]["category"] == "Parts"


def test_invoice_payload_lines_include_category_and_taxable():
    from datetime import datetime, timezone

    from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine

    now = datetime.now(timezone.utc)
    invoice = Invoice(
        invoice_number="INV-CAT-1", subtotal=Decimal("100.00"),
        tax_amount=Decimal("0.00"), total=Decimal("100.00"),
        balance_due=Decimal("100.00"), status="draft",
        public_token=uuid4().hex, customer_id=uuid4(), company_id="tenant-test",
    )
    invoice.lines = [
        InvoiceLine(
            id=uuid4(), description="Labor", category="Labor", taxable=False,
            quantity=1, unit_price=Decimal("50.00"), line_total=Decimal("50.00"),
            sort_order=1, created_at=now, company_id="tenant-test",
        ),
        InvoiceLine(
            # taxable=None (legacy row predating the column) must read as True
            id=uuid4(), description="Part", category=None, taxable=None,
            quantity=1, unit_price=Decimal("50.00"), line_total=Decimal("50.00"),
            sort_order=2, created_at=now, company_id="tenant-test",
        ),
    ]
    payload = pdf_router._invoice_payload(invoice, None)
    assert payload["lines"][0] == {
        "description": "Labor", "category": "Labor", "taxable": False,
        "quantity": 1, "unit_price": 50.0, "line_total": 50.0,
    }
    assert payload["lines"][1]["category"] == ""
    assert payload["lines"][1]["taxable"] is True
