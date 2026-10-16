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
    from gdx_dispatch.tests.conftest import iter_app_routes
    for _, route in iter_app_routes(app):
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
    """Estimate.tax_rate is None on a service call → resolve via TaxConfig.default_rate."""
    from gdx_dispatch.models.tenant_models import Job
    from gdx_dispatch.modules.tax.models import TaxConfig

    db = pdf_app.dependency_overrides[get_db]()
    try:
        db.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
        job = Job(title="Repair", job_type="Service Call", company_id="tenant-test")
        db.add(job)
        db.flush()
        est = Estimate(
            job_id=job.id,
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


def test_estimate_payload_null_rate_on_no_job_prints_no_tax(pdf_app: FastAPI):
    """GDXA-930: a NULL-rate estimate on no job is an installation's quote.
    It printed "Tax (7.38%)" while the invoice made from it billed 0."""
    from gdx_dispatch.modules.tax.models import TaxConfig

    db = pdf_app.dependency_overrides[get_db]()
    try:
        db.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
        est = Estimate(
            estimate_number="EST-TAX-930",
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
    assert (payload["tax"], payload["tax_rate_pct"], payload["total"]) == (0.0, 0.0, 200.0)


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

    monkeypatch.setattr(
        pdf_router,
        "generate_estimate_pdf",
        lambda estimate_data, tenant_branding, template_config=None: b"%PDF-1.7\ntest",
    )

    db = pdf_app.dependency_overrides[get_db]()
    try:
        resp = pdf_router.estimate_pdf(UUID(estimate_id), db=db)
    finally:
        db.close()

    assert isinstance(resp, StreamingResponse)
    assert resp.media_type == "application/pdf"
    assert "attachment;" in resp.headers.get("content-disposition", "")


# ── Job name under the customer block (2026-10-15) ───────────────────────────


def _render_html(monkeypatch, kind: str, data: dict) -> str:
    """Render the REAL template; only the WeasyPrint byte step is stubbed."""
    from gdx_dispatch.core import pdf_generator

    captured: dict[str, str] = {}

    class FakeHTML:
        def __init__(self, string: str, base_url: str | None = None):
            captured["html"] = string

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7\n"

    monkeypatch.setattr(pdf_generator, "HTML", FakeHTML)
    gen = pdf_generator.generate_estimate_pdf if kind == "estimate" else pdf_generator.generate_invoice_pdf
    gen({"customer": {}, **data}, {"company_name": "GDX"})
    return captured["html"]


def _seed_job(db, title: str):
    from gdx_dispatch.models.tenant_models import Customer, Job

    cust = Customer(name="Pat Example", address="9 Elm St", company_id="tenant-test")
    db.add(cust)
    db.flush()
    job = Job(customer_id=cust.id, title=title, status="scheduled", company_id="tenant-test")
    db.add(job)
    db.flush()
    return cust, job


def test_estimate_job_name_prints_below_customer(pdf_app: FastAPI, monkeypatch):
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, _job = _seed_job(db, "unused")
        est = Estimate(estimate_number="EST-JN-1", label="Smith 2-car carriage door",
                       customer_id=cust.id, total=Decimal("0"), status="draft",
                       public_token=uuid4().hex, company_id="tenant-test")
        db.add(est)
        db.flush()
        payload = pdf_router._estimate_payload(est, cust, db=db)
    finally:
        db.close()
    assert payload["job_name"] == "Smith 2-car carriage door"
    html = _render_html(monkeypatch, "estimate", payload)
    assert html.index("Pat Example") < html.index("Job Name") < html.index("Smith 2-car carriage door")


def test_estimate_job_name_prefers_job_title_like_the_email(pdf_app: FastAPI):
    """Same order as the estimate email subject (job title, then label), so
    the email and its attached PDF never name the job differently."""
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Warehouse dock doors")
        est = Estimate(estimate_number="EST-JN-2", label="Dock door quote", job_id=job.id,
                       customer_id=cust.id, total=Decimal("0"), status="draft",
                       public_token=uuid4().hex, company_id="tenant-test")
        db.add(est)
        db.flush()
        assert pdf_router._estimate_payload(est, cust, db=db)["job_name"] == "Warehouse dock doors"
    finally:
        db.close()


def test_invoice_job_name_from_linked_job(pdf_app: FastAPI, monkeypatch):
    from gdx_dispatch.models.tenant_models import Invoice

    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Spring replacement - garage 2")
        inv = Invoice(invoice_number="INV-JN-1", job_id=job.id, customer_id=cust.id,
                      subtotal=Decimal("0"), tax_amount=Decimal("0"), total=Decimal("0"),
                      balance_due=Decimal("0"), status="sent", public_token=uuid4().hex,
                      company_id="tenant-test")
        db.add(inv)
        db.flush()
        payload = pdf_router._invoice_payload(inv, cust, db)
    finally:
        db.close()
    assert payload["job_name"] == "Spring replacement - garage 2"
    html = _render_html(monkeypatch, "invoice", payload)
    assert html.index("Pat Example") < html.index("Job Name") < html.index("Spring replacement - garage 2")


def test_no_job_name_prints_no_heading(pdf_app: FastAPI, monkeypatch):
    """A QB-imported invoice has no job and no estimate: no empty heading."""
    from gdx_dispatch.models.tenant_models import Invoice

    inv = Invoice(invoice_number="INV-JN-2", subtotal=Decimal("0"), tax_amount=Decimal("0"),
                  total=Decimal("0"), balance_due=Decimal("0"), status="sent",
                  public_token=uuid4().hex, customer_id=uuid4(), company_id="tenant-test")
    db = pdf_app.dependency_overrides[get_db]()
    try:
        payload = pdf_router._invoice_payload(inv, None, db)
    finally:
        db.close()
    assert payload["job_name"] == ""
    assert "Job Name" not in _render_html(monkeypatch, "invoice", payload)


# ── Jobsite, status label, pay link, money format (2026-10-15) ───────────────


def _invoice_for(db, job=None, cust=None, **kw):
    from gdx_dispatch.models.tenant_models import Invoice

    fields = dict(invoice_number=f"INV-{uuid4().hex[:6]}", subtotal=Decimal("1800"),
                  tax_amount=Decimal("0"), total=Decimal("1800"), balance_due=Decimal("1800"),
                  status="sent", public_token=uuid4().hex, company_id="tenant-test",
                  job_id=job.id if job is not None else None,
                  customer_id=cust.id if cust is not None else uuid4())
    fields.update(kw)
    inv = Invoice(**fields)
    db.add(inv)
    db.flush()
    return inv


def test_invoice_prints_jobsite_when_it_differs_from_billing(pdf_app: FastAPI, monkeypatch):
    from gdx_dispatch.models.tenant_models import CustomerLocation

    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Cabin door")
        loc = CustomerLocation(customer_id=str(cust.id), address="77 Lake Rd", city="Ely",
                               state="MN", zip="55731", company_id="tenant-test")
        db.add(loc)
        db.flush()
        job.location_id = loc.id
        db.flush()
        payload = pdf_router._invoice_payload(_invoice_for(db, job, cust), cust, db)
    finally:
        db.close()
    assert payload["jobsite_address"] == "77 Lake Rd, Ely, MN 55731"
    html = _render_html(monkeypatch, "invoice", payload)
    assert html.index("Cabin door") < html.index("Jobsite") < html.index("77 Lake Rd, Ely, MN 55731")


def test_invoice_omits_jobsite_equal_to_billing(pdf_app: FastAPI):
    """No location rows → the resolver falls back to customer.address, which is
    already printed: no duplicate Jobsite block."""
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Spring")
        payload = pdf_router._invoice_payload(_invoice_for(db, job, cust), cust, db)
    finally:
        db.close()
    assert payload["jobsite_address"] == ""


def _jobsite_with_billing(pdf_app: FastAPI, monkeypatch, billing: str, address: str = "123 Main St",
                          city: str | None = "Anytown") -> tuple[str, str]:
    """Seed a job at a location row (`address`, `city`, MN 55555 — or the whole
    address in `address` with no city columns, as the estimate-conversion bind
    writes it) for a customer billed at `billing`; return (payload jobsite,
    rendered invoice HTML)."""
    from gdx_dispatch.models.tenant_models import CustomerLocation

    split = city is not None
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Partial billing")
        cust.address = billing
        loc = CustomerLocation(customer_id=str(cust.id), address=address, city=city,
                               state="MN" if split else None, zip="55555" if split else None,
                               company_id="tenant-test")
        db.add(loc)
        db.flush()
        job.location_id = loc.id
        db.flush()
        payload = pdf_router._invoice_payload(_invoice_for(db, job, cust), cust, db)
    finally:
        db.close()
    return payload["jobsite_address"], _render_html(monkeypatch, "invoice", payload)


@pytest.mark.parametrize("billing", ["123 Main St, Anytown", "123 Main St Anytown MN"])
@pytest.mark.parametrize("row", ["split", "whole"])
def test_invoice_omits_jobsite_when_billing_is_a_shorter_form_of_it(pdf_app: FastAPI, monkeypatch, billing, row):
    """GDXA-1281: a billing address missing the state/zip is still the job site;
    the Jobsite block must not repeat it — for a row with city columns and for
    one holding the whole address in `address`."""
    kw = {} if row == "split" else {"address": "123 Main St, Anytown, MN 55555", "city": None}
    jobsite, html = _jobsite_with_billing(pdf_app, monkeypatch, billing, **kw)
    assert jobsite == ""
    assert "Jobsite" not in html


@pytest.mark.parametrize("billing", ["123 Main St, Anytown", "123 Main St, Other"])
def test_invoice_prints_jobsite_in_another_city_with_the_same_street(pdf_app: FastAPI, monkeypatch, billing):
    """The control: same street, billed in another town (or only part of the
    site's town name) — the site is NOT the billing address and must print."""
    jobsite, html = _jobsite_with_billing(pdf_app, monkeypatch, billing, city="Other City")
    assert jobsite == "123 Main St, Other City, MN 55555"
    assert html.index("Jobsite") < html.index("123 Main St, Other City, MN 55555")


@pytest.mark.parametrize(
    ("billing", "street", "full", "covered"),
    [
        ("123 main st anytown mn 55555", "123 main st", "123 Main St, Anytown, MN 55555", True),  # = full
        ("123 main st", "123 main st", "123 Main St, Anytown, MN 55555", True),  # = street
        ("123 main st anytown", "123 main st", "123 Main St, Anytown, MN 55555", True),  # street + town
        ("123 main st anytown mn", "123 main st", "123 Main St, Anytown, MN 55555", True),
        # the whole address in the street field (estimate-conversion rows)
        ("123 main st anytown", "123 main st anytown mn 55555", "123 Main St, Anytown, MN 55555", True),
        ("123 main st anytown mn 5555", "123 main st", "123 Main St, Anytown, MN 55555", False),  # part word
        ("123 main", "123 main st", "123 Main St, Anytown, MN 55555", False),  # short of the street
        ("123 main st othertown", "123 main st", "123 Main St, Anytown, MN 55555", False),  # another town
        ("123 main st mn 55555", "123 main st", "123 Main St, Anytown, MN 55555", False),  # skips the town
        # a street named like the town is not the town
        ("100 anoka falls rd anoka", "100 anoka falls rd", "100 Anoka Falls Rd, Anoka Falls, MN 55303", False),
        ("123 main st anytown", "123 main st", "123 Main St", False),  # site has no town to match
        ("", "123 main st", "123 Main St, Anytown, MN 55555", False),
    ],
)
def test_billing_covers_site(billing, street, full, covered):
    assert pdf_router._billing_covers_site(billing, street, full) is covered


@pytest.mark.parametrize(
    ("status", "balance", "settled", "label"),
    [
        ("sent", 100, 0, "Unpaid"),
        ("sent", 100, 50, "Partially paid"),
        ("open", 100, 0, "Unpaid"),
        ("paid", 0, 100, "Paid"),
        ("void", 0, 0, "Void"),
        ("draft", 100, 0, "Draft"),
        ("sent", 0, 0, "No balance due"),
    ],
)
def test_invoice_status_label(status, balance, settled, label):
    assert pdf_router._invoice_status_label(status, balance, settled) == label


def test_invoice_pdf_prints_customer_status_not_raw(monkeypatch):
    html = _render_html(monkeypatch, "invoice", {"invoice_number": "I", "status": "sent",
                                                 "status_label": "Unpaid", "balance_due": 5})
    assert "Payment Status: Unpaid" in html
    assert "Payment Status: sent" not in html


def test_invoice_pay_link_gates(pdf_app: FastAPI, monkeypatch):
    """The link prints only where the email would offer it AND the pay page
    would accept it: balance owed, Stripe + base URL configured, not draft/void."""
    from gdx_dispatch.core import payments

    monkeypatch.setenv("GDX_PUBLIC_BASE_URL", "https://pay.example.test")
    monkeypatch.setattr(payments, "stripe_configured", lambda: True)
    monkeypatch.setattr(payments, "card_surcharge_notice", lambda db, tid: "Cards carry a 3% fee.")
    db = pdf_app.dependency_overrides[get_db]()
    try:
        owed = _invoice_for(db)
        p = pdf_router._invoice_payload(owed, None, db)
        assert p["pay_url"] == f"https://pay.example.test/pay/{owed.public_token}"
        assert p["card_notice"] == "Cards carry a 3% fee."
        html = _render_html(monkeypatch, "invoice", p)
        assert f'href="https://pay.example.test/pay/{owed.public_token}"' in html
        assert "Cards carry a 3% fee." in html
        for kw in ({"status": "draft"}, {"status": "void"}, {"balance_due": Decimal("0"), "status": "paid"}):
            assert pdf_router._invoice_payload(_invoice_for(db, **kw), None, db)["pay_url"] == ""
        monkeypatch.setattr(payments, "stripe_configured", lambda: False)
        assert pdf_router._invoice_payload(_invoice_for(db), None, db)["pay_url"] == ""
    finally:
        db.close()


def test_totals_use_thousands_separator(monkeypatch):
    inv = _render_html(monkeypatch, "invoice", {
        "invoice_number": "I", "subtotal": 1800, "tax": 0, "total": 1800,
        "paid_to_date": 1200, "credits_applied": 0, "balance_due": 600, "status": "sent"})
    assert "$1,800.00" in inv and "-$1,200.00" in inv and "$1800.00" not in inv
    est = _render_html(monkeypatch, "estimate", {
        "estimate_number": "E", "subtotal": 2500, "discount": 100, "tax": 0, "total": 2400})
    assert "$2,500.00" in est and "-$100.00" in est and "$2,400.00" in est and "$2400.00" not in est


def test_past_due_invoice_prints_past_due(pdf_app: FastAPI):
    """"overdue" is never stored — it is derived from the due date when read
    (routers/invoices._effective_status). The PDF must say what the app says."""
    from datetime import date, timedelta

    db = pdf_app.dependency_overrides[get_db]()
    try:
        late = _invoice_for(db, due_date=date.today() - timedelta(days=60))
        assert pdf_router._invoice_payload(late, None, db)["status_label"] == "Past due"
        not_yet = _invoice_for(db, due_date=date.today() + timedelta(days=30))
        assert pdf_router._invoice_payload(not_yet, None, db)["status_label"] == "Unpaid"
    finally:
        db.close()


# ── Job #, description of work, payment terms (GDXA-1291) ────────────────────


def test_invoice_prints_job_number_description_and_terms(pdf_app: FastAPI, monkeypatch):
    from datetime import date

    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Spring replacement")
        job.job_number = "JOB-2026-042"
        job.description = "gate code 4411, dog in yard"  # office intake text
        est = Estimate(estimate_number="EST-DW-1", description="Replace both torsion springs",
                       customer_id=cust.id, job_id=job.id, total=Decimal("0"), status="accepted",
                       public_token=uuid4().hex, company_id="tenant-test")
        db.add(est)
        db.flush()
        inv = _invoice_for(db, job, cust, estimate_id=est.id,
                           invoice_date=date(2026, 10, 1), due_date=date(2026, 10, 31))
        payload = pdf_router._invoice_payload(inv, cust, db)
    finally:
        db.close()
    assert payload["job_number"] == "JOB-2026-042"
    assert payload["description"] == "Replace both torsion springs"
    assert payload["terms"] == "Net 30, due 2026-10-31"
    html = _render_html(monkeypatch, "invoice", payload)
    assert "Job #: JOB-2026-042" in html
    assert html.index("Description of Work") < html.index("Replace both torsion springs")
    assert "gate code" not in html
    assert html.index("<strong>Terms</strong>") < html.index("Net 30, due 2026-10-31")


def test_invoice_blank_when_job_number_description_terms_absent(pdf_app: FastAPI, monkeypatch):
    """Legacy job with no number, no source estimate, no stored invoice_date:
    no empty "Job #", "Description of Work" or "Terms" heading."""
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Spring")
        payload = pdf_router._invoice_payload(_invoice_for(db, job, cust), cust, db)
    finally:
        db.close()
    assert (payload["job_number"], payload["description"], payload["terms"]) == ("", "", "")
    html = _render_html(monkeypatch, "invoice", payload)
    for heading in ("Job #", "Description of Work", "<strong>Terms</strong>"):
        assert heading not in html


def test_invoice_job_description_never_printed(pdf_app: FastAPI):
    """job.description is office intake text, not customer-facing."""
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Spring")
        job.description = "gate code 4411, dog in yard"
        payload = pdf_router._invoice_payload(_invoice_for(db, job, cust), cust, db)
    finally:
        db.close()
    assert "gate code" not in repr(payload)


@pytest.mark.parametrize(
    ("days", "words"),
    [(0, "Due on receipt"), (15, "Net 15, due 2026-10-16"), (-3, "")],
)
def test_invoice_payment_terms_words(days, words):
    from datetime import date, timedelta
    from types import SimpleNamespace

    inv = SimpleNamespace(invoice_date=date(2026, 10, 1), due_date=date(2026, 10, 1) + timedelta(days=days))
    assert pdf_router._invoice_payment_terms(inv) == words
    assert pdf_router._invoice_payment_terms(SimpleNamespace(invoice_date=None, due_date=date(2026, 10, 1))) == ""


def test_estimate_prints_linked_job_number(pdf_app: FastAPI, monkeypatch):
    db = pdf_app.dependency_overrides[get_db]()
    try:
        cust, job = _seed_job(db, "Dock doors")
        job.job_number = "JOB-2026-077"
        est = Estimate(estimate_number="EST-JNUM-1", job_id=job.id, customer_id=cust.id,
                       total=Decimal("0"), status="draft", public_token=uuid4().hex,
                       company_id="tenant-test")
        db.add(est)
        db.flush()
        payload = pdf_router._estimate_payload(est, cust, db=db)
        unlinked = Estimate(estimate_number="EST-JNUM-2", customer_id=cust.id, total=Decimal("0"),
                            status="draft", public_token=uuid4().hex, company_id="tenant-test")
        db.add(unlinked)
        db.flush()
        bare = pdf_router._estimate_payload(unlinked, cust, db=db)
    finally:
        db.close()
    assert payload["job_number"] == "JOB-2026-077"
    assert "Job #: JOB-2026-077" in _render_html(monkeypatch, "estimate", payload)
    assert bare["job_number"] == ""
    assert "Job #" not in _render_html(monkeypatch, "estimate", bare)
