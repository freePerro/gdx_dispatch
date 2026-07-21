"""Hide per-line prices on customer documents ("hide line-item prices").

Doug 2026-07-12. Covers the tri-state resolution helper, the payload builders
that carry the effective flag, and the three customer-facing templates (estimate
PDF, invoice PDF, install sheet) rendered in both show/hide modes. Only the
per-line Unit Price / Line Total columns are hidden — the Subtotal/Tax/Total
breakdown stays (invoice tax must be separately stated; no mystery Total).
Hermetic: templates render as HTML strings via their Jinja envs — no WeasyPrint,
no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

from gdx_dispatch.core.pdf_generator import _render_template as _render_pdf_html
from gdx_dispatch.modules.estimates_features import effective_hide_line_prices
from gdx_dispatch.modules.estimates_features.router import FeaturesPayload, _COLS
from gdx_dispatch.routers.install_sheet import _load_template
from gdx_dispatch.routers.pdf import _estimate_payload, _invoice_payload


# ── Tri-state resolution helper ─────────────────────────────────────────────
def test_effective_hide_line_prices_tri_state():
    # Explicit override always wins over the tenant default.
    assert effective_hide_line_prices(True, False) is True
    assert effective_hide_line_prices(False, True) is False
    assert effective_hide_line_prices(True, True) is True
    assert effective_hide_line_prices(False, False) is False
    # NULL override inherits the tenant default.
    assert effective_hide_line_prices(None, True) is True
    assert effective_hide_line_prices(None, False) is False


def _fake_estimate(hide, *, total="100.00"):
    return SimpleNamespace(
        estimate_number="EST-000001",
        jobsite_address="",
        description="",
        notes="",
        lines=[
            SimpleNamespace(
                description="Double door", quantity=1, unit_price=100.0,
                line_total=100.0, sort_order=1, created_at=None, id=1, category=None,
            )
        ],
        total=total,
        discount=None,
        tax_rate=None,
        customer_id=None,
        hide_line_prices=hide,
    )


# ── Estimate payload resolution ─────────────────────────────────────────────
def test_estimate_payload_override_wins_over_default():
    # Per-estimate False beats a tenant default of True (and vice-versa).
    assert _estimate_payload(_fake_estimate(False), None, hide_line_prices_default=True)["hide_line_prices"] is False
    assert _estimate_payload(_fake_estimate(True), None, hide_line_prices_default=False)["hide_line_prices"] is True


def test_estimate_payload_null_inherits_tenant_default():
    assert _estimate_payload(_fake_estimate(None), None, hide_line_prices_default=True)["hide_line_prices"] is True
    assert _estimate_payload(_fake_estimate(None), None, hide_line_prices_default=False)["hide_line_prices"] is False


def test_estimate_payload_default_arg_is_show():
    # Omitting the default (test-only direct calls / no tenant) shows prices.
    assert _estimate_payload(_fake_estimate(None), None)["hide_line_prices"] is False


def test_estimate_total_unchanged_when_hidden():
    # Hiding is purely presentational — the computed total must not move.
    shown = _estimate_payload(_fake_estimate(False), None)
    hidden = _estimate_payload(_fake_estimate(True), None)
    assert shown["total"] == hidden["total"]


# ── Estimate PDF template ───────────────────────────────────────────────────
def _render_estimate_pdf(hide):
    payload = _estimate_payload(_fake_estimate(hide), None, hide_line_prices_default=False)
    # Through the real render path (still hermetic — returns the HTML string,
    # never touches WeasyPrint) so the template gets its `tpl` context.
    return _render_pdf_html(
        "estimate_pdf.html",
        payload,
        {"company_name": "GDX", "primary_color": "#000", "secondary_color": "#111", "address": "", "logo": ""},
        None,
        "estimate",
    )


def test_estimate_pdf_shows_prices_by_default():
    html = _render_estimate_pdf(False)
    assert "Unit Price" in html
    assert "Line Total" in html
    assert "Subtotal" in html
    assert "$100.00" in html
    assert "Total" in html  # grand total always present


def test_estimate_pdf_hides_prices_when_flagged():
    html = _render_estimate_pdf(True)
    # Only the per-line price COLUMNS are gone...
    assert "Unit Price" not in html
    assert "Line Total" not in html
    # ...the description, qty, the tax breakdown and the Total all remain
    # (Subtotal/Tax stay so the Total is never a mystery number).
    assert "Double door" in html
    assert "Subtotal" in html
    assert "Tax" in html
    assert "Total" in html
    assert "$100.00" in html  # subtotal/total values still render


# ── Invoice payload + template ──────────────────────────────────────────────
def _fake_invoice(hide):
    return SimpleNamespace(
        invoice_number="INV-000001",
        lines=[SimpleNamespace(description="Double door", quantity=1, unit_price=100.0, line_total=100.0, sort_order=1, created_at=None, id=1, category=None, taxable=True)],
        subtotal=100.0, tax_amount=0.0, tax_rate=None, total=100.0, balance_due=100.0,
        status="draft", due_date=None, notes="", hide_line_prices=hide,
    )


def test_invoice_payload_carries_flag():
    assert _invoice_payload(_fake_invoice(True), None)["hide_line_prices"] is True
    assert _invoice_payload(_fake_invoice(False), None)["hide_line_prices"] is False


def _render_invoice_pdf(hide):
    payload = _invoice_payload(_fake_invoice(hide), None)
    return _render_pdf_html(
        "invoice_pdf.html",
        payload,
        {"company_name": "GDX", "primary_color": "#000", "secondary_color": "#111", "address": "", "logo": ""},
        None,
        "invoice",
    )


def test_invoice_pdf_hides_prices_when_flagged():
    shown = _render_invoice_pdf(False)
    assert "Unit Price" in shown and "$100.00" in shown

    hidden = _render_invoice_pdf(True)
    assert "Unit Price" not in hidden
    assert "Line Total" not in hidden
    # Tax stays separately stated (invoice compliance); Total + Balance Due stay.
    assert "Subtotal" in hidden
    assert "Tax" in hidden
    assert "Total" in hidden
    assert "Balance Due" in hidden


# ── Install sheet template ──────────────────────────────────────────────────
def _render_install_sheet(hide):
    return _load_template().render(
        company_name="GDX", job_number="JOB-0001", customer_name="Jane",
        address="", phone="", scheduled_at="", technician="Bob",
        job_type="Install", priority="Normal", door_specs=None,
        parts=[{"description": "Double door", "category": "Doors", "quantity": 1, "unit_price": 100.0, "total": 100.0}],
        estimate_total=100.0, hide_line_prices=hide, notes="", auto_print=False,
    )


def test_install_sheet_shows_prices_by_default():
    html = _render_install_sheet(False)
    assert "Unit Price" in html
    assert "Estimate Total" in html
    assert "$100.00" in html


def test_install_sheet_hides_prices_when_flagged():
    html = _render_install_sheet(True)
    # Only the per-line price columns are hidden...
    assert "Unit Price" not in html
    # ...the estimate total row and the part itself still show.
    assert "Estimate Total" in html
    assert "Double door" in html


# ── Tenant-default plumbing ─────────────────────────────────────────────────
def test_features_expose_hide_line_prices_column():
    # The Settings save writes every _COLS field; the toggle must be included
    # or saving another setting would silently reset it.
    assert "estimates_hide_line_prices" in _COLS
    assert "estimates_hide_line_prices" in FeaturesPayload.model_fields


# ── DB-backed round-trips (reuse the estimates router test app) ─────────────
from fastapi.testclient import TestClient  # noqa: E402

from gdx_dispatch.tests.test_estimates import (  # noqa: E402,F401
    client,
    _create_customer,
    _create_estimate,
)


def test_create_and_patch_round_trip_tri_state(client: TestClient):
    # Created with no flag → NULL (inherit).
    est = _create_estimate(client, label="No override")
    assert est["hide_line_prices"] is None

    # Created with an explicit True override.
    est2 = _create_estimate(client, label="Hidden", hide_line_prices=True)
    assert est2["hide_line_prices"] is True

    # PATCH true → false → null (revert to inherit) all persist as sent.
    for value in (True, False, None):
        r = client.patch(f"/api/estimates/{est['id']}", json={"hide_line_prices": value})
        assert r.status_code == 200, r.text
        assert r.json()["hide_line_prices"] is value


def test_duplicate_clones_hide_line_prices(client: TestClient):
    src = _create_estimate(client, label="Hidden src", hide_line_prices=True)
    r = client.post(f"/api/estimates/{src['id']}/duplicate")
    assert r.status_code == 201, r.text
    assert r.json()["hide_line_prices"] is True
