"""GET /api/invoices/{id} surfaces customer contact (name/email/phone/address)
so the frontend Bill-To card renders without a second roundtrip. Regression
guard for the 2026-05-21 invoice-detail customer-contact slice."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import (
    Customer,
    Invoice,
    InvoiceLine,
    Job,
    JobPartNeeded,
    Payment,
)
from gdx_dispatch.modules.proposals.models import Estimate, EstimateLine
from gdx_dispatch.routers.invoices import InvoiceCreateIn, create_invoice, get_invoice


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    for tbl in [
        Job.__table__,
        Estimate.__table__,
        EstimateLine.__table__,
        Invoice.__table__,
        InvoiceLine.__table__,
        Payment.__table__,
        JobPartNeeded.__table__,
    ]:
        tbl.create(bind=engine, checkfirst=True)
    TenantBase.metadata.create_all(bind=engine, checkfirst=True)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _current_user() -> dict[str, str]:
    return {"user_id": "user-1", "tenant_id": "tenant-1", "role": "admin"}


def _seed_customer(db, **overrides) -> Customer:
    defaults = dict(
        name="Acme Door Co",
        email="ops@acme.example",
        phone="555-0142",
        address="123 Main St, Smalltown, MN",
        company_id="tenant-test",
    )
    defaults.update(overrides)
    cust = Customer(**defaults)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _create_invoice_for(db, cust: Customer) -> dict:
    job = Job(
        customer_id=cust.id,
        title="Service call",
        lifecycle_stage="estimate",
        dispatch_status="unassigned",
        billing_status="unbilled",
        company_id="tenant-test",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return create_invoice(
        payload=InvoiceCreateIn(
            job_id=job.id,
            customer_id=cust.id,
            due_date=date.today() + timedelta(days=30),
            line_items=[{"description": "Spring", "quantity": 1, "unit_price": Decimal("75.00")}],
        ),
        _=_current_user(),
        db=db,
    )


def test_get_invoice_returns_customer_contact_fields(tenant_db_session):
    cust = _seed_customer(tenant_db_session)
    created = _create_invoice_for(tenant_db_session, cust)

    payload = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_name"] == "Acme Door Co"
    assert payload["customer_email"] == "ops@acme.example"
    assert payload["customer_phone"] == "555-0142"
    assert payload["customer_address"] == "123 Main St, Smalltown, MN"


def test_get_invoice_returns_empty_strings_when_customer_fields_blank(tenant_db_session):
    """Customer exists but has no email/phone/address yet — fields are
    present-and-empty so the frontend renders the `+ Add email` affordances
    instead of treating them as missing."""
    cust = _seed_customer(
        tenant_db_session,
        email=None,
        phone=None,
        address=None,
    )
    created = _create_invoice_for(tenant_db_session, cust)

    payload = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)

    assert payload["customer_id"] == str(cust.id)
    assert payload["customer_email"] == ""
    assert payload["customer_phone"] == ""
    assert payload["customer_address"] == ""


def test_get_invoice_handles_missing_customer_row_gracefully(tenant_db_session):
    """Customer row was hard-deleted (cleanup, merge, QB resync). The
    enrichment lookup returns None and the response stays well-formed
    instead of 500-ing — the frontend then falls back to "Unknown customer"."""
    cust = _seed_customer(tenant_db_session)
    created = _create_invoice_for(tenant_db_session, cust)

    # Drop the customer row. sqlite doesn't enforce the FK so the invoice
    # stays put — same shape as a Postgres tenant where the customer was
    # cascaded out from a different code path.
    tenant_db_session.delete(cust)
    tenant_db_session.commit()

    payload = get_invoice(UUID(created["id"]), _=_current_user(), db=tenant_db_session)

    # Lookup returned None — no contact fields set, no exception raised.
    assert payload["id"] == created["id"]
    assert not payload.get("customer_email")
    assert not payload.get("customer_phone")
    assert not payload.get("customer_address")
