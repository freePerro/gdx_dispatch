"""Invoice tax-rate mode + line edit/delete.

Doug 2026-05-06 / S110: invoices stored a flat tax_amount with no rate,
so editing a line never recomputed tax. Plus there was no way to edit or
remove a line once added. This file exercises:

- tax_rate-mode: changing a line's qty/price recomputes tax.
- non-taxable lines: labor flagged taxable=False is excluded from the
  taxable subtotal.
- line PATCH and DELETE endpoints exist and recompute totals.
- legacy flat-tax_amount mode (tax_rate=NULL) still honors the stored
  tax_amount unchanged.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.models.tenant_models  # noqa: F401
from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.pay_periods import shop_today_from_settings
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Job
from gdx_dispatch.routers.invoices import (
    InvoiceCreateIn,
    InvoiceLineCreateIn,
    InvoiceLinePatchIn,
    InvoicePatchIn,
    create_invoice,
    delete_invoice_line,
    patch_invoice,
    patch_invoice_line,
)


def _user():
    return {"sub": "u-1", "user_id": "u-1", "role": "admin", "tenant_id": "tenant-test"}


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


def _seed_job(db, *, job_type: str | None = None, customer_id: UUID | None = None) -> Job:
    job = Job(
        customer_id=customer_id or uuid4(),
        title="Install",
        description="",
        job_type=job_type,
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create_with_lines(db, *, tax_rate, lines):
    job = _seed_job(db)
    payload = InvoiceCreateIn(
        job_id=job.id,
        customer_id=job.customer_id,
        tax_rate=tax_rate,
        line_items=[InvoiceLineCreateIn(**ln) for ln in lines],
        invoice_date=shop_today_from_settings(db),
    )
    return create_invoice(payload=payload, _=_user(), db=db)


def test_rate_mode_taxes_only_taxable_lines(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door 10x8", "quantity": 1, "unit_price": 1000.0, "taxable": True},
            {"description": "Labor — install", "quantity": 4, "unit_price": 100.0, "taxable": False},
        ],
    )
    # Subtotal = 1400, taxable subtotal = 1000, tax = 1000 * 0.0738 = 73.80
    assert inv["subtotal"] == 1400.00
    assert inv["taxable_subtotal"] == 1000.00
    assert round(inv["tax_amount"], 2) == 73.80
    assert round(inv["total"], 2) == 1473.80
    assert inv["tax_rate"] == 0.0738


def test_rate_mode_recomputes_after_line_edit(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door 10x8", "quantity": 1, "unit_price": 1000.0, "taxable": True},
        ],
    )
    line_id = UUID([
        ln for ln in (
            patch_invoice_line.__wrapped__.__name__,  # smoke; real id below
        )
    ][0]) if False else None  # placeholder; replaced below
    # Pull the line id from DB.
    line_row = db.execute(
        InvoiceLine.__table__.select().where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).mappings().first()
    line_id = line_row["id"]

    updated = patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_id,
        payload=InvoiceLinePatchIn(quantity=2),
        user=_user(),
        db=db,
    )
    assert updated["quantity"] == 2
    assert round(updated["line_total"], 2) == 2000.00

    # Re-read invoice via the GET path equivalent (read from DB).
    db.expire_all()
    inv_row = db.get(Invoice, UUID(inv["id"]))
    assert round(float(inv_row.subtotal), 2) == 2000.00
    # Tax should follow: 2000 * 0.0738 = 147.60
    assert round(float(inv_row.tax_amount), 2) == 147.60
    assert round(float(inv_row.total), 2) == 2147.60


def test_line_delete_removes_from_subtotal_and_tax(db):
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door", "quantity": 1, "unit_price": 1000.0, "taxable": True},
            {"description": "Spring", "quantity": 1, "unit_price": 200.0, "taxable": True},
        ],
    )
    rows = db.execute(
        InvoiceLine.__table__.select().where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).mappings().all()
    spring = next(r for r in rows if r["description"] == "Spring")

    delete_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=spring["id"],
        user=_user(),
        db=db,
    )
    db.expire_all()
    inv_row = db.get(Invoice, UUID(inv["id"]))
    assert round(float(inv_row.subtotal), 2) == 1000.00
    assert round(float(inv_row.tax_amount), 2) == 73.80


def test_legacy_flat_tax_amount_path_unchanged(db):
    job = _seed_job(db)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            tax_amount=42.50,
            invoice_date=shop_today_from_settings(db),
            line_items=[InvoiceLineCreateIn(description="X", quantity=1, unit_price=500.0)],
        ),
        _=_user(), db=db,
    )
    # tax_rate stays None → flat tax_amount honored as-is.
    assert inv["tax_rate"] is None
    assert round(inv["tax_amount"], 2) == 42.50
    assert round(inv["total"], 2) == 542.50

    # Patch tax_amount → still flat-mode → no rate applied.
    updated = patch_invoice(
        invoice_id=UUID(inv["id"]),
        payload=InvoicePatchIn(tax_amount=99.99),
        current_user=_user(), db=db,
    )
    assert updated["tax_rate"] is None
    assert round(updated["tax_amount"], 2) == 99.99

def test_explicit_zero_rate_beats_configured_default(db):
    # Doug 2026-07-20: the create form used to translate an entered 0% into
    # tax_rate=null, which made the server re-resolve the tenant default and
    # put the tax back. An EXPLICIT 0 must win over a configured default.
    from gdx_dispatch.modules.tax.models import TaxConfig

    db.add(TaxConfig(name="Default", default_rate=Decimal("0.0738")))
    db.commit()

    inv = _create_with_lines(
        db,
        tax_rate=0.0,
        lines=[
            {"description": "Door 10x8", "quantity": 1, "unit_price": 1000.0, "taxable": True},
        ],
    )
    assert inv["tax_rate"] == 0.0
    assert round(inv["tax_amount"], 2) == 0.00
    assert round(inv["total"], 2) == 1000.00

    # And it must STAY 0 across a line edit (rate-mode recalc with rate=0).
    line_row = db.execute(
        InvoiceLine.__table__.select().where(InvoiceLine.invoice_id == UUID(inv["id"]))
    ).mappings().first()
    patch_invoice_line(
        invoice_id=UUID(inv["id"]),
        line_id=line_row["id"],
        payload=InvoiceLinePatchIn(quantity=3),
        user=_user(),
        db=db,
    )
    db.expire_all()
    inv_row = db.get(Invoice, UUID(inv["id"]))
    assert round(float(inv_row.subtotal), 2) == 3000.00
    assert round(float(inv_row.tax_amount), 2) == 0.00
    assert round(float(inv_row.total), 2) == 3000.00


@pytest.mark.parametrize(
    ("job_type", "expected_rate", "expected_tax"),
    [
        # Repair / service call: the tenant default taxes the part (control).
        ("Service Call", 0.07375, 7.38),
        # GDXA-793: an installation is never taxed — rate 0, not the default.
        ("Installation", 0.0, 0.0),
        # GDXA-889: New Construction is an installation for tax.
        ("New Construction", 0.0, 0.0),
        # Not an installation: taxed at the default, never guessed from text.
        ("Other", 0.07375, 7.38),
        ("Garage Door Install", 0.07375, 7.38),
    ],
)
def test_create_without_rate_follows_the_job_lane(db, job_type, expected_rate, expected_tax):
    from gdx_dispatch.modules.tax.models import TaxConfig

    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    db.commit()
    job = _seed_job(db, job_type=job_type)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            invoice_date=shop_today_from_settings(db),
            line_items=[
                InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=100.0, taxable=True),
            ],
        ),
        _=_user(), db=db,
    )
    assert inv["tax_rate"] == pytest.approx(expected_rate)
    assert round(inv["tax_amount"], 2) == expected_tax
    assert round(inv["total"], 2) == round(100.0 + expected_tax, 2)


@pytest.mark.parametrize(
    ("job_type", "exempt", "estimate_rate", "payload_rate", "expected_rate", "expected_tax"),
    [
        # GDXA-842: a 0% quote stays 0% on the bill — the tenant default
        # (7.375%) used to win here and bill $7.38 tax.
        ("Service Call", False, Decimal("0"), None, 0.0, 0.0),
        ("Service Call", False, Decimal("0.05"), None, 0.05, 5.0),
        # No override on the estimate: the job lane / tenant default, unchanged.
        ("Service Call", False, None, None, 0.07375, 7.38),
        # The caller's explicit rate still wins over the estimate's.
        ("Service Call", False, Decimal("0"), 0.07375, 0.07375, 7.38),
        ("Service Call", False, Decimal("0.05"), 0.0, 0.0, 0.0),
        # A quote cannot tax an installation or an exempt customer: both are
        # never taxed (Doug, 2026-10-12), so the lane and the exemption come
        # before the quoted rate.
        ("Installation", False, Decimal("0.05"), None, 0.0, 0.0),
        ("Service Call", True, Decimal("0.05"), None, 0.0, 0.0),
    ],
)
def test_source_estimate_rate_is_the_quoted_rate(
    db, job_type, exempt, estimate_rate, payload_rate, expected_rate, expected_tax
):
    """`source_estimate_id` honors the estimate's rate override (M24) the same
    as `estimate_id`, after the job lane and the customer's exemption."""
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption

    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    db.commit()
    job = _seed_job(db, job_type=job_type)
    if exempt:
        db.add(TaxExemption(customer_id=job.customer_id, exempt=True, reason="non-profit"))
        db.commit()
    est = Estimate(
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        proposal_mode=False,
        total=Decimal("100.00"),
        tax_rate=estimate_rate,
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-test",
    )
    db.add(est)
    db.commit()
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            invoice_date=shop_today_from_settings(db),
            source_estimate_id=est.id,
            tax_rate=payload_rate,
            line_items=[
                InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=100.0, taxable=True),
            ],
        ),
        _=_user(), db=db,
    )
    assert inv["tax_rate"] == pytest.approx(expected_rate)
    assert round(inv["tax_amount"], 2) == expected_tax
    assert round(inv["total"], 2) == round(100.0 + expected_tax, 2)
    # Read the row back, not the response dict.
    db.expire_all()
    row = db.get(Invoice, UUID(inv["id"]))
    assert float(row.tax_rate) == pytest.approx(expected_rate)
    assert round(float(row.tax_amount), 2) == expected_tax
    assert row.source_estimate_id == est.id


def test_failed_exemption_read_never_taxes_at_the_quote(db, monkeypatch):
    """GDXA-842: when the exemption read fails on an estimate-quoted bill, the
    invoice falls back to NULL (untaxed), not the quoted rate — an exempt
    customer must never be charged tax because a read failed."""
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.tax import service as tax_service
    from gdx_dispatch.modules.tax.models import TaxConfig

    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    db.commit()
    job = _seed_job(db, job_type="Service Call")
    est = Estimate(
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        proposal_mode=False,
        total=Decimal("100.00"),
        tax_rate=Decimal("0.05"),
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-test",
    )
    db.add(est)
    db.commit()

    def _boom(*_a, **_k):
        raise RuntimeError("exemption read failed")

    monkeypatch.setattr(tax_service, "is_customer_exempt", _boom)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=job.customer_id,
            invoice_date=shop_today_from_settings(db),
            source_estimate_id=est.id,
            line_items=[
                InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=100.0, taxable=True),
            ],
        ),
        _=_user(), db=db,
    )
    assert inv["tax_rate"] is None
    assert round(inv["tax_amount"], 2) == 0.0
    assert round(inv["total"], 2) == 100.0


@pytest.mark.parametrize(
    ("job_type", "estimate_rate", "expected"),
    [
        ("Service Call", Decimal("0"), 0.0),
        ("Service Call", Decimal("0.05"), 0.05),
        ("Service Call", None, 0.07375),
        ("Installation", Decimal("0.05"), 0.0),
        ("New Construction", Decimal("0.05"), 0.0),  # GDXA-889
        ("New Construction", None, 0.0),
    ],
)
def test_resolve_endpoint_orders_the_estimate_rate(db, job_type, estimate_rate, expected):
    """GDXA-842: `/api/tax/resolve?estimate_id=` is what the office create
    screen seeds from, so it answers with the same order as invoice create."""
    from gdx_dispatch.modules.proposals.models import Estimate
    from gdx_dispatch.modules.tax.models import TaxConfig
    from gdx_dispatch.modules.tax.router import resolve_rate_endpoint

    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    db.commit()
    job = _seed_job(db, job_type=job_type)
    est = Estimate(
        job_id=job.id,
        customer_id=job.customer_id,
        estimate_number=f"EST-{uuid4().hex[:8]}",
        proposal_mode=False,
        total=Decimal("100.00"),
        tax_rate=estimate_rate,
        status="accepted",
        public_token=uuid4().hex,
        company_id="tenant-test",
    )
    db.add(est)
    db.commit()
    out = resolve_rate_endpoint(
        customer_id=str(job.customer_id), job_id=str(job.id), estimate_id=str(est.id),
        user=_user(), db=db,
    )
    assert out["rate"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("job_type", "exempt", "expected"),
    [
        ("Service Call", False, Decimal("0.07375")),
        ("Repair", False, Decimal("0.07375")),
        (None, False, Decimal("0.07375")),  # unlaned job / no job: tenant default
        ("Installation", False, Decimal("0")),
        # Exempt customers are never taxed, on any lane (Doug, 2026-10-12).
        ("Service Call", True, Decimal("0")),
        ("Installation", True, Decimal("0")),
    ],
)
def test_resolve_rate_for_job_follows_lane_and_exemption(db, job_type, exempt, expected):
    """GDXA-796: the shared job-lane tax helper. Installations are 0, repairs
    and service calls get the tenant default, exempt customers are 0 on both."""
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption
    from gdx_dispatch.modules.tax.service import resolve_rate_for_job

    customer_id = uuid4()
    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    if exempt:
        db.add(TaxExemption(customer_id=customer_id, exempt=True, reason="non-profit"))
    db.commit()
    assert resolve_rate_for_job(db, customer_id, job_type) == expected


@pytest.mark.parametrize(
    ("job_type", "expected_rate"),
    [
        # Exempt on a service call: rate stays NULL (legacy flat path), no tax.
        ("Service Call", None),
        # Exempt on an installation: the lane answers first, rate-mode at 0.
        ("Installation", 0.0),
    ],
)
def test_create_without_rate_never_taxes_exempt_customer(db, job_type, expected_rate):
    """GDXA-796: an exempt customer is never taxed on either lane."""
    from gdx_dispatch.modules.tax.models import TaxConfig, TaxExemption

    customer_id = uuid4()
    db.add(TaxConfig(name="Default", default_rate=Decimal("0.07375")))
    db.add(TaxExemption(customer_id=customer_id, exempt=True, reason="non-profit"))
    db.commit()
    job = _seed_job(db, job_type=job_type, customer_id=customer_id)
    inv = create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=customer_id,
            invoice_date=shop_today_from_settings(db),
            line_items=[
                InvoiceLineCreateIn(description="Spring", quantity=1, unit_price=100.0, taxable=True),
            ],
        ),
        _=_user(), db=db,
    )
    assert inv["tax_rate"] == expected_rate
    assert round(inv["tax_amount"], 2) == 0.0
    assert round(inv["total"], 2) == 100.0
    row = db.get(Invoice, UUID(str(inv["id"])))
    assert float(row.tax_amount or 0) == 0.0


def test_patch_zero_rate_zeroes_existing_tax(db):
    # Zeroing the rate on an existing rate-mode invoice must wipe the tax
    # dollars, not preserve them (PATCH tax_rate=null preserves; 0 must not).
    inv = _create_with_lines(
        db,
        tax_rate=0.0738,
        lines=[
            {"description": "Door", "quantity": 1, "unit_price": 1000.0, "taxable": True},
        ],
    )
    assert round(inv["tax_amount"], 2) == 73.80

    updated = patch_invoice(
        invoice_id=UUID(inv["id"]),
        payload=InvoicePatchIn(tax_rate=0.0),
        current_user=_user(), db=db,
    )
    assert updated["tax_rate"] == 0.0
    assert round(updated["tax_amount"], 2) == 0.00
    assert round(updated["total"], 2) == 1000.00
