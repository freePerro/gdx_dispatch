"""DELETE /api/customers/{id} refuses while the customer owns billed invoices.

GDXA-418: the delete soft-deleted with no look at invoices, and its only gate
was get_current_user. A customer with a sent, unpaid $50 invoice was deleted,
leaving that receivable under a record every customer list filters out. Merge
and absorb move invoices to the surviving customer before retiring a row;
delete has no such step, so it must refuse instead.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.audit import AuditLog
from gdx_dispatch.core.database import get_db
from gdx_dispatch.core.modules import require_module
from gdx_dispatch.models.tenant_models import Customer, Invoice, Payment
from gdx_dispatch.routers import customers as customers_router
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.tests.conftest import make_fresh_db

TENANT = "tenant-a"
USER = "user-1"


def _client(monkeypatch, role: str):
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    engine = make_fresh_db()
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    user = {"user_id": USER, "tenant_id": TENANT, "role": role}

    app = FastAPI()
    app.include_router(customers_router.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_module("customers")] = lambda: True

    @app.middleware("http")
    async def _stamp(request, call_next):
        request.state.tenant = {"id": TENANT, "slug": "test"}
        request.state.user = user
        return await call_next(request)

    return TestClient(app), db, engine


@pytest.fixture
def admin(monkeypatch):
    client, db, engine = _client(monkeypatch, "admin")
    yield client, db
    db.close()
    engine.dispose()


def _customer(db) -> Customer:
    c = Customer(id=uuid4(), name="Overdue Check", phone="555-1111", company_id=TENANT)
    db.add(c)
    db.commit()
    return c


def _invoice(db, customer: Customer, status: str, *, deleted: bool = False) -> Invoice:
    inv = Invoice(
        company_id=TENANT,
        customer_id=customer.id,
        job_id=None,
        invoice_number=f"INV-{uuid4().hex[:8].upper()}",
        billing_type="standard",
        sequence_number=1,
        subtotal=Decimal("50"),
        tax_amount=Decimal("0"),
        total=Decimal("50"),
        balance_due=Decimal("0") if status in ("paid", "void") else Decimal("50"),
        status=status,
        invoice_date=date.today(),
        due_date=date.today(),
        public_token=uuid4().hex,
        locked=False,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(inv)
    db.commit()
    return inv


def _deleted_at(db, customer: Customer):
    db.expire_all()
    return db.get(Customer, customer.id).deleted_at


def test_sent_unpaid_invoice_refuses_the_delete_and_changes_nothing(admin):
    client, db = admin
    c = _customer(db)
    _invoice(db, c, "sent")

    r = client.delete(f"/api/customers/{c.id}")

    assert r.status_code == 409, r.text
    assert "1 invoice past draft" in r.json()["detail"]
    assert _deleted_at(db, c) is None
    assert db.execute(
        select(AuditLog).where(AuditLog.action == "customer_deleted")
    ).first() is None


@pytest.mark.parametrize("status", ["overdue", "paid", "void"])
def test_any_invoice_past_draft_refuses(admin, status):
    client, db = admin
    c = _customer(db)
    _invoice(db, c, status)
    _invoice(db, c, "sent")

    r = client.delete(f"/api/customers/{c.id}")

    assert r.status_code == 409, r.text
    assert "2 invoices past draft" in r.json()["detail"]
    assert _deleted_at(db, c) is None


def test_draft_only_customer_deletes(admin):
    client, db = admin
    c = _customer(db)
    _invoice(db, c, "draft")

    r = client.delete(f"/api/customers/{c.id}")

    assert r.status_code == 204, r.text
    assert _deleted_at(db, c) is not None
    db.expire_all()
    (row,) = db.execute(
        select(AuditLog).where(AuditLog.action == "customer_deleted")
    ).scalars().all()
    assert row.entity_id == str(c.id)


def _payment(db, inv: Invoice, *, voided: bool = False) -> None:
    db.add(Payment(
        company_id=TENANT,
        invoice_id=inv.id,
        amount=Decimal("20"),
        method="check",
        payment_date=date.today(),
        voided_at=datetime.now(UTC) if voided else None,
    ))
    db.commit()


def test_a_draft_with_a_payment_refuses(admin):
    # A draft takes a payment and stays draft until its balance reaches zero
    # (routers/invoices.py record-payment), so status alone would let this
    # customer go with $20 received and $30 owed.
    client, db = admin
    c = _customer(db)
    _payment(db, _invoice(db, c, "draft"))

    r = client.delete(f"/api/customers/{c.id}")

    assert r.status_code == 409, r.text
    assert "1 invoice past draft" in r.json()["detail"]
    assert _deleted_at(db, c) is None


def test_a_draft_whose_only_payment_is_voided_deletes(admin):
    client, db = admin
    c = _customer(db)
    _payment(db, _invoice(db, c, "draft"), voided=True)

    assert client.delete(f"/api/customers/{c.id}").status_code == 204
    assert _deleted_at(db, c) is not None


def test_a_soft_deleted_invoice_does_not_block(admin):
    client, db = admin
    c = _customer(db)
    _invoice(db, c, "sent", deleted=True)

    assert client.delete(f"/api/customers/{c.id}").status_code == 204
    assert _deleted_at(db, c) is not None


def test_another_customers_invoice_does_not_block(admin):
    client, db = admin
    c, other = _customer(db), _customer(db)
    _invoice(db, other, "sent")

    assert client.delete(f"/api/customers/{c.id}").status_code == 204


def test_a_role_without_customers_write_is_refused(monkeypatch):
    # The builtin technician holds customers.contact_write, never customers.write.
    client, db, engine = _client(monkeypatch, "technician")
    try:
        c = _customer(db)
        r = client.delete(f"/api/customers/{c.id}")
        assert r.status_code == 403, r.text
        assert "customers.write" in r.json()["detail"]
        assert _deleted_at(db, c) is None
    finally:
        db.close()
        engine.dispose()


def test_an_office_role_with_customers_write_may_delete(monkeypatch):
    client, db, engine = _client(monkeypatch, "dispatcher")
    try:
        c = _customer(db)
        assert client.delete(f"/api/customers/{c.id}").status_code == 204
        assert _deleted_at(db, c) is not None
    finally:
        db.close()
        engine.dispose()
