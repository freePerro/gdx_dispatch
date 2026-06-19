from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.responses import StreamingResponse

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import AppSettings
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers import pdf as pdf_router


@pytest.fixture()
def pdf_app():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override_db():
        return Session()

    app = FastAPI()
    app.include_router(pdf_router.router)
    app.dependency_overrides[get_db] = _override_db
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is require_module("documents"):
                app.dependency_overrides[dep.call] = lambda: None

    yield app
    app.dependency_overrides.clear()
    engine.dispose()


def _seed_documents_data(app: FastAPI) -> tuple[str, str]:
    dep = app.dependency_overrides[get_db]
    db = dep()
    try:
        settings = AppSettings(
            company_name="GDX Garage Pros",
            address="123 Main St, Chicago, IL",
            logo="https://example.com/logo.png",
            primary_color="#114488",
            secondary_color="#22aa77",
        )
        db.add(settings)
        db.flush()

        estimate = Estimate(
            job_id=None,
            customer_id=None,
            estimate_number="EST-000001",
            label="Garage Door Tune-up",
            notes="Estimate terms",
            total=Decimal("325.00"),
            status="draft",
            public_token=uuid4().hex,
            company_id="tenant-test",  # NOT NULL since Build Rule 5 hardening
        )
        db.add(estimate)
        db.flush()
        db.add(
            EstimateLine(
                estimate_id=estimate.id,
                description="Torsion Spring",
                quantity=1,
                unit_price=Decimal("250.00"),
                line_total=Decimal("250.00"),
                sort_order=1,
                company_id="tenant-test",
            )
        )
        db.commit()
        return str(estimate.id), ""
    finally:
        db.close()


def test_estimate_pdf_generates(monkeypatch):
    from gdx_dispatch.core import pdf_generator

    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            captured["html"] = string
            captured["base_url"] = base_url or ""

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\nestimate"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)

    pdf_bytes = pdf_generator.generate_estimate_pdf(
        estimate_data={
            "estimate_number": "EST-0001",
            "customer": {"name": "Acme"},
            "lines": [{"description": "Spring", "quantity": 1, "unit_price": 100, "line_total": 100}],
            "subtotal": 100,
            "tax": 8,
            "total": 108,
            "terms": "Net 30",
        },
        tenant_branding={"company_name": "GDX"},
    )

    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")
    assert "EST-0001" in captured["html"]


def test_invoice_pdf_generates(monkeypatch):
    from gdx_dispatch.core import pdf_generator

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            self.string = string

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\ninvoice"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)

    pdf_bytes = pdf_generator.generate_invoice_pdf(
        invoice_data={
            "invoice_number": "INV-0001",
            "customer": {"name": "Acme"},
            "lines": [{"description": "Labor", "quantity": 2, "unit_price": 75, "line_total": 150}],
            "subtotal": 150,
            "tax": 12,
            "total": 162,
            "balance_due": 162,
            "status": "sent",
            "due_date": "2030-01-31",
            "terms": "Due on receipt",
        },
        tenant_branding={"company_name": "GDX"},
    )

    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF")


def test_pdf_includes_company_name(monkeypatch):
    from gdx_dispatch.core import pdf_generator

    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\nbranding"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)

    pdf_generator.generate_estimate_pdf(
        estimate_data={
            "estimate_number": "EST-42",
            "customer": {"name": "Acme"},
            "lines": [],
            "subtotal": 0,
            "tax": 0,
            "total": 0,
            "terms": "",
        },
        tenant_branding={"company_name": "Northwind Doors"},
    )

    assert "Northwind Doors" in captured["html"]


def test_pdf_line_items(monkeypatch):
    from gdx_dispatch.core import pdf_generator

    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\nlines"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)

    pdf_generator.generate_invoice_pdf(
        invoice_data={
            "invoice_number": "INV-7",
            "customer": {"name": "Acme"},
            "lines": [{"description": "Cable", "quantity": 2, "unit_price": 40, "line_total": 80}],
            "subtotal": 80,
            "tax": 0,
            "total": 80,
            "balance_due": 80,
            "status": "draft",
            "due_date": None,
            "terms": "",
        },
        tenant_branding={"company_name": "GDX"},
    )

    assert "Cable" in captured["html"]
    assert "2" in captured["html"]


def test_estimate_payload_applies_per_estimate_tax_and_discount(pdf_app: FastAPI):
    """Per-estimate tax_rate + discount must flow into the PDF payload."""
    db = pdf_app.dependency_overrides[get_db]()
    try:
        est = Estimate(
            estimate_number="EST-TAX-1",
            total=Decimal("1000.00"),
            tax_rate=Decimal("0.0825"),
            discount=Decimal("100.00"),
            status="draft",
            public_token=uuid4().hex,
            company_id="tenant-test",
        )
        db.add(est)
        db.commit()
        payload = pdf_router._estimate_payload(est, None, db=db)
    finally:
        db.close()
    assert payload["subtotal"] == 1000.0
    assert payload["discount"] == 100.0
    # (1000 - 100) * 0.0825 = 74.25
    assert payload["tax"] == 74.25
    assert payload["total"] == 974.25
    assert payload["tax_rate_pct"] == 8.25


def test_estimate_payload_falls_back_to_tax_config(pdf_app: FastAPI):
    """Estimate.tax_rate is None → resolve via TaxConfig.default_rate."""
    from gdx_dispatch.modules.tax.models import TaxConfig

    db = pdf_app.dependency_overrides[get_db]()
    try:
        db.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
        est = Estimate(
            estimate_number="EST-TAX-2",
            total=Decimal("200.00"),
            tax_rate=None,
            status="draft",
            public_token=uuid4().hex,
            company_id="tenant-test",
        )
        db.add(est)
        db.commit()
        payload = pdf_router._estimate_payload(est, None, db=db)
    finally:
        db.close()
    assert payload["tax"] == 14.76
    assert payload["total"] == 214.76


def test_invoice_payload_tax_rate_pct_uses_persisted_rate(pdf_app: FastAPI):
    """Regression: PDF "Tax (X%)" label must equal the configured rate, not
    the back-derived tax_amount/subtotal (which drifts on small subtotals
    because tax_amount is rounded to cents). Fire-dept invoice symptom:
    rate=7.38% rendered as 7.41% before the fix.
    """
    from gdx_dispatch.models.tenant_models import Invoice

    invoice = Invoice(
        invoice_number="INV-TAX-1",
        subtotal=Decimal("6.75"),
        tax_rate=Decimal("0.0738"),
        tax_amount=Decimal("0.50"),  # round(6.75 * 0.0738, 2)
        total=Decimal("7.25"),
        balance_due=Decimal("7.25"),
        status="sent",
        public_token=uuid4().hex,
        customer_id=uuid4(),
        company_id="tenant-test",
    )
    payload = pdf_router._invoice_payload(invoice, None)
    assert payload["tax_rate_pct"] == 7.38


def test_invoice_payload_rate_is_source_of_truth_when_lines_partially_taxable(pdf_app: FastAPI):
    """Intentional asymmetry: tax_rate is the customer-facing rate, tax_amount
    is what was actually charged. They legitimately diverge when some lines
    are non-taxable (e.g. labor in most US states) — subtotal=$200, only
    $100 is taxable at 8.25% → tax=$8.25, rate label is still 8.25%, NOT
    $8.25/$200=4.125%. Pinning this so a future refactor doesn't "fix" the
    label back to the misleading derivation.
    """
    from gdx_dispatch.models.tenant_models import Invoice

    invoice = Invoice(
        invoice_number="INV-TAX-MIX",
        subtotal=Decimal("200.00"),
        tax_rate=Decimal("0.0825"),
        tax_amount=Decimal("8.25"),  # only $100 of the $200 subtotal was taxable
        total=Decimal("208.25"),
        balance_due=Decimal("208.25"),
        status="sent",
        public_token=uuid4().hex,
        customer_id=uuid4(),
        company_id="tenant-test",
    )
    payload = pdf_router._invoice_payload(invoice, None)
    assert payload["tax_rate_pct"] == 8.25
    assert payload["tax"] == 8.25
    assert payload["subtotal"] == 200.0


def test_invoice_payload_tax_rate_pct_falls_back_when_rate_null(pdf_app: FastAPI):
    """Legacy QB-imported invoices have tax_rate=NULL — keep the
    back-derivation so the label still appears (matches pre-fix behavior).
    """
    from gdx_dispatch.models.tenant_models import Invoice

    invoice = Invoice(
        invoice_number="INV-TAX-2",
        subtotal=Decimal("100.00"),
        tax_rate=None,
        tax_amount=Decimal("8.25"),
        total=Decimal("108.25"),
        balance_due=Decimal("108.25"),
        status="sent",
        public_token=uuid4().hex,
        customer_id=uuid4(),
        company_id="tenant-test",
    )
    payload = pdf_router._invoice_payload(invoice, None)
    assert payload["tax_rate_pct"] == 8.25


@pytest.mark.anyio
async def test_endpoint_requires_auth(pdf_app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=pdf_app), base_url="http://test") as client:
        resp = await client.get(f"/api/estimates/{uuid4()}/pdf")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_endpoint_returns_pdf(pdf_app: FastAPI, monkeypatch):
    estimate_id, _ = _seed_documents_data(pdf_app)

    monkeypatch.setattr(pdf_router, "generate_estimate_pdf", lambda estimate_data, tenant_branding: b"%PDF-1.7\ntest")

    db = pdf_app.dependency_overrides[get_db]()
    try:
        resp = pdf_router.estimate_pdf(UUID(estimate_id), db=db)
    finally:
        db.close()

    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "application/pdf"
    assert "attachment;" in resp.headers.get("content-disposition", "")
