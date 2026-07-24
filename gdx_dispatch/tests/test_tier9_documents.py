"""Tier 9 — customer-facing document correctness
(docs/design/backend-vue-contract-gaps-2026-07-24.md).

These are the highest-stakes fixes: numbers on the PDFs and emails a
customer actually receives. Each asserts the document now FOOTS.
"""
from __future__ import annotations

from uuid import uuid4

from gdx_dispatch.core.email_sender import _money, build_invoice_email_html

# ── 9.10 sign placement ─────────────────────────────────────────────────────


def test_money_sign_outside_dollar():
    assert _money(-500) == "-$500.00"
    assert _money(500) == "$500.00"
    assert _money(0) == "$0.00"
    assert _money(-1234.5) == "-$1,234.50"
    assert _money(None) == "$0.00"


# ── 9.4 email body foots (paid-to-date + credits) ───────────────────────────


def test_invoice_email_body_shows_settlement_lines():
    html = build_invoice_email_html(
        company_name="Acme",
        invoice_number="INV-1",
        customer_name="Cust",
        line_items=[{"description": "Netting credit", "quantity": 1, "unit_price": -500, "line_total": -500}],
        subtotal=1000.0,
        tax_amount=0.0,
        total=1000.0,
        balance_due=350.0,
        paid_to_date=150.0,
        credits_applied=500.0,
    )
    # Settlement lines present so Total → Balance Due reconciles
    assert "Paid to Date" in html
    assert "-$150.00" in html
    assert "Credits Applied" in html
    assert "-$500.00" in html
    # Netting line renders -$500.00, not $-500.00 (9.10)
    assert "$-500" not in html


def test_invoice_email_body_omits_settlement_when_zero():
    html = build_invoice_email_html(
        company_name="Acme", invoice_number="INV-2", customer_name="C",
        line_items=[], subtotal=100.0, tax_amount=0.0, total=100.0, balance_due=100.0,
    )
    assert "Paid to Date" not in html
    assert "Credits Applied" not in html


# ── 9.2 invoice PDF reconciles: Total − Paid − Credits == Balance Due ───────


def test_invoice_pdf_payload_includes_credits_and_reconciles():

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401
    from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceAdjustment, InvoiceLine
    from gdx_dispatch.routers.pdf import _invoice_payload

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine)()
    try:
        cust = Customer(id=uuid4(), company_id="t", name="Credit Cust")
        db.add(cust)
        inv = Invoice(
            id=uuid4(), company_id="t", customer_id=cust.id, invoice_number="INV-CR",
            public_token=uuid4().hex, subtotal=500, tax_amount=0, total=500, balance_due=350, status="sent",
        )
        db.add(inv)
        db.add(InvoiceLine(id=uuid4(), invoice_id=inv.id, company_id="t", description="Work",
                           quantity=1, unit_price=500, line_total=500, sort_order=1))
        db.add(InvoiceAdjustment(invoice_id=inv.id, kind="credit_memo", amount=150,
                                 reason="goodwill", created_by="u", company_id="t"))
        db.commit()
        db.refresh(inv)

        p = _invoice_payload(inv, cust, db)
        assert p["credits_applied"] == 150.0
        assert p["paid_to_date"] == 0.0
        assert abs(p["total"] - p["paid_to_date"] - p["credits_applied"] - p["balance_due"]) < 0.01

        # And it renders without a template error, with a Credits Applied line
        from gdx_dispatch.core.pdf_generator import generate_invoice_pdf
        pdf = generate_invoice_pdf(
            invoice_data=p,
            tenant_branding={"company_name": "Co", "logo": "", "primary_color": "#000",
                             "secondary_color": "#222", "address": ""},
        )
        assert pdf[:5] == b"%PDF-"
    finally:
        db.close()
        engine.dispose()


def test_invoice_settlement_shows_true_overpayment():
    """An overpayment is a real fact: paid $130 (80 cash + 50 credit) on a
    $100 invoice must print the TRUE amounts, not capped ones — capping would
    lie about money received. Balance clamps to $0; the customer sees they're
    paid up."""
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.models import tenant_models  # noqa: F401
    from gdx_dispatch.models.tenant_models import Customer, Invoice, InvoiceAdjustment, Payment
    from gdx_dispatch.routers.pdf import _invoice_settlement

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TenantBase.metadata.create_all(engine, checkfirst=True)
    from datetime import UTC, datetime
    db = sessionmaker(bind=engine)()
    try:
        cust = Customer(id=uuid4(), company_id="t", name="Overpaid")
        db.add(cust)
        inv = Invoice(id=uuid4(), company_id="t", customer_id=cust.id, invoice_number="INV-OP",
                      public_token=uuid4().hex, subtotal=100, tax_amount=0, total=100,
                      balance_due=0, status="paid")
        db.add(inv)
        # $80 paid + $50 credit = $130 against a $100 invoice → clamps to $0
        db.add(Payment(id=uuid4(), invoice_id=inv.id, company_id="t", amount=80, method="cash",
                       payment_date=datetime.now(UTC)))
        db.add(InvoiceAdjustment(invoice_id=inv.id, kind="credit_memo", amount=50,
                                 reason="x", created_by="u", company_id="t"))
        db.commit()
        db.refresh(inv)

        paid, credits = _invoice_settlement(inv, db)
        # True amounts, not capped: $80 paid + $50 credit both shown in full.
        assert paid == 80.0
        assert credits == 50.0
        # The stored balance is clamped to $0 (max(100 - 130, 0)).
        assert float(inv.balance_due) == 0.0
    finally:
        db.close()
        engine.dispose()
