"""Authorization tests for the customer-portal charge path.

``POST /payments/methods/{method_id}/charge`` was AUTHENTICATED but not
AUTHORIZED: it took ``amount_cents`` from the request body and pulled
``invoice_id`` out of client-supplied ``metadata`` with no ownership check. A
logged-in portal customer could therefore charge their own saved card and have
the payment recorded against ANY invoice in the system — including one
belonging to a different customer.

These tests pin the fix: the invoice must belong to the caller, and the amount
charged is the invoice's balance due, not the client's number.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models.tenant_models import Invoice, InvoiceLine, Payment
from gdx_dispatch.modules.customer_portal.models import CustomerUser


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    CustomerUser.__table__.create(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _invoice(db, customer_id, *, number, total=200.00, balance=None, status="sent"):
    """A faithful invoice: one line item summing to ``total``.

    The line matters. ``_recalculate_invoice`` recomputes the total from
    InvoiceLine rows and only flips an invoice to "paid" when that recomputed
    total is > 0, so a line-less fixture silently never settles and the test
    would be asserting against a shape no real invoice has.
    """
    inv = Invoice(
        customer_id=customer_id,
        job_id=uuid.uuid4(),
        invoice_number=number,
        billing_type="standard",
        subtotal=total,
        tax_amount=0,
        total=total,
        balance_due=total if balance is None else balance,
        status=status,
        public_token=f"tok-{number}",
        company_id="tenant-test",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    db.add(
        InvoiceLine(
            invoice_id=inv.id,
            company_id="tenant-test",
            description=f"Work for {number}",
            quantity=1,
            unit_price=total,
            line_total=total,
            taxable=False,
        )
    )
    db.commit()
    return inv


@pytest.fixture
def me(db_session):
    """The logged-in portal customer."""
    user = CustomerUser(
        customer_id=uuid.uuid4(), email="me@example.com", is_active=True
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def client(db_session, me, monkeypatch):
    """Portal client with the Stripe-customer lookup stubbed.

    IMPORTANT: ``CustomerUser`` has no ``stripe_customer_id`` column, so
    ``_require_stripe_customer`` reads ``None`` and 400s for every real caller
    — this endpoint is effectively unreachable in production today. The stub
    is here so the AUTHORIZATION logic below is actually exercised rather than
    short-circuited; do NOT read these passing tests as evidence the portal
    charge flow works end-to-end. (An earlier version of this file assigned
    ``user.stripe_customer_id`` directly — setting an unmapped attribute, i.e.
    inventing a schema the app doesn't have, purely to reach the code.)
    """
    import gdx_dispatch.routers.payments as portal_payments
    from gdx_dispatch.core.database import get_db
    from gdx_dispatch.core.modules import require_module

    monkeypatch.setattr(portal_payments, "_require_stripe_customer", lambda user: "cus_me")

    app = FastAPI()
    app.include_router(portal_payments.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[portal_payments._current_portal_user] = lambda: me
    app.dependency_overrides[require_module("invoices")] = lambda: None
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_portal_charge_is_unreachable_without_a_stripe_customer_column(db_session, me):
    """Documents live reality: CustomerUser has no stripe_customer_id, so the
    charge endpoint 400s before any of the authorization logic runs.

    Kept so this is a known, asserted fact rather than a surprise. If a
    ``stripe_customer_id`` column is ever added, this test fails and the
    authorization tests below become load-bearing for real traffic.
    """
    from gdx_dispatch.routers.payments import _require_stripe_customer

    assert not hasattr(type(me), "stripe_customer_id"), (
        "CustomerUser gained a stripe_customer_id column — the portal charge "
        "path is now reachable; re-verify it end-to-end."
    )
    with pytest.raises(Exception) as exc:
        _require_stripe_customer(me)
    assert "400" in str(exc.value) or "Stripe customer" in str(exc.value)


def _intent(amount=20000):
    m = MagicMock()
    m.id = "pi_portal_1"
    m.status = "succeeded"
    m.amount = amount
    return m


def test_charge_records_against_own_invoice(client, db_session, me):
    mine = _invoice(db_session, me.customer_id, number="INV-MINE")
    with patch("gdx_dispatch.routers.payments.charge_saved_method", return_value=_intent()) as ch:
        resp = client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 20000, "metadata": {"invoice_id": str(mine.id)}},
        )

    assert resp.status_code == 200, resp.text
    assert ch.call_args[1]["amount_cents"] == 20000
    db_session.refresh(mine)
    assert mine.status == "paid"


def test_charge_cannot_target_another_customers_invoice(client, db_session, me):
    """ATTACK: credit my charge to someone else's invoice."""
    theirs = _invoice(db_session, uuid.uuid4(), number="INV-THEIRS")
    with patch("gdx_dispatch.routers.payments.charge_saved_method") as ch:
        resp = client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(theirs.id)}},
        )

    # 404, not 403 — don't confirm that someone else's invoice id is real.
    assert resp.status_code == 404, resp.text
    ch.assert_not_called()
    db_session.refresh(theirs)
    assert theirs.status != "paid"
    assert db_session.query(Payment).filter(Payment.invoice_id == theirs.id).count() == 0


def test_charge_ignores_client_amount(client, db_session, me):
    """ATTACK: pay $1 against a $200 invoice. The balance is what gets charged."""
    mine = _invoice(db_session, me.customer_id, number="INV-AMT")
    with patch("gdx_dispatch.routers.payments.charge_saved_method", return_value=_intent()) as ch:
        resp = client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(mine.id)}},
        )

    assert resp.status_code == 200, resp.text
    assert ch.call_args[1]["amount_cents"] == 20000


def test_charge_uses_balance_not_total(client, db_session, me):
    partly = _invoice(db_session, me.customer_id, number="INV-PART", total=200.00, balance=75.00)
    with patch("gdx_dispatch.routers.payments.charge_saved_method", return_value=_intent(7500)) as ch:
        resp = client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 20000, "metadata": {"invoice_id": str(partly.id)}},
        )

    assert resp.status_code == 200, resp.text
    assert ch.call_args[1]["amount_cents"] == 7500


def test_charge_requires_an_invoice(client):
    with patch("gdx_dispatch.routers.payments.charge_saved_method") as ch:
        resp = client.post("/payments/methods/pm_1/charge", json={"amount_cents": 100})
    assert resp.status_code == 422
    ch.assert_not_called()


def test_charge_rejects_void_and_settled_invoices(client, db_session, me):
    void = _invoice(db_session, me.customer_id, number="INV-VOID", status="void")
    settled = _invoice(db_session, me.customer_id, number="INV-ZERO", balance=0.00)
    with patch("gdx_dispatch.routers.payments.charge_saved_method") as ch:
        assert client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(void.id)}},
        ).status_code == 409
        assert client.post(
            "/payments/methods/pm_1/charge",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(settled.id)}},
        ).status_code == 409
    ch.assert_not_called()
