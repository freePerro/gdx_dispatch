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


# ── M13: /intent, the sibling nobody closed ────────────────────────────────
#
# `charge_method` got ownership, a server-derived amount and a fixed currency
# on 2026-08-04. `/intent` on the SAME router kept taking `amount_cents`,
# `currency` and an arbitrary `metadata` dict from the body, with no invoice
# reference required — and `core/payments.py` records the resulting payment
# against `metadata.invoice_id`. So an authenticated portal user could mint an
# intent naming ANY invoice in the system and have their money land on someone
# else's bill. Authentication enforced, authorization absent: the identical
# hole, on the endpoint next door.
#
# These mirror the charge tests above deliberately. Two siblings with the same
# blast radius should be guarded the same way, and the divergence is what let
# this one sit open for three weeks.


def _pi(amount=20000):
    m = MagicMock()
    m.id = "pi_intent_1"
    m.client_secret = "pi_intent_1_secret"
    m.amount = amount
    return m


def test_intent_cannot_target_another_customers_invoice(client, db_session, me):
    """THE ATTACK. Mint an intent naming an invoice that is not mine."""
    theirs = _invoice(db_session, uuid.uuid4(), number="INV-THEIRS-PI")
    with patch("gdx_dispatch.routers.payments.create_payment_intent") as mk:
        resp = client.post(
            "/payments/intent",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(theirs.id)}},
        )

    # 404, not 403 — don't confirm that someone else's invoice id is real.
    assert resp.status_code == 404, resp.text
    mk.assert_not_called()


def test_intent_ignores_the_client_amount(client, db_session, me):
    """ATTACK: ask for $1 against a $200 invoice."""
    mine = _invoice(db_session, me.customer_id, number="INV-PI-AMT")
    with patch("gdx_dispatch.routers.payments.create_payment_intent", return_value=_pi()) as mk:
        resp = client.post(
            "/payments/intent",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(mine.id)}},
        )

    assert resp.status_code == 200, resp.text
    assert mk.call_args[1]["amount_cents"] == 20000


def test_intent_uses_balance_not_total(client, db_session, me):
    partly = _invoice(db_session, me.customer_id, number="INV-PI-PART", total=200.00, balance=75.00)
    with patch("gdx_dispatch.routers.payments.create_payment_intent", return_value=_pi(7500)) as mk:
        resp = client.post(
            "/payments/intent",
            json={"amount_cents": 20000, "metadata": {"invoice_id": str(partly.id)}},
        )

    assert resp.status_code == 200, resp.text
    assert mk.call_args[1]["amount_cents"] == 7500


def test_intent_requires_an_invoice(client):
    with patch("gdx_dispatch.routers.payments.create_payment_intent") as mk:
        resp = client.post("/payments/intent", json={"amount_cents": 100})
    assert resp.status_code == 422
    mk.assert_not_called()


def test_intent_rejects_void_and_settled_invoices(client, db_session, me):
    void = _invoice(db_session, me.customer_id, number="INV-PI-VOID", status="void")
    settled = _invoice(db_session, me.customer_id, number="INV-PI-ZERO", balance=0.00)
    with patch("gdx_dispatch.routers.payments.create_payment_intent") as mk:
        assert client.post(
            "/payments/intent",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(void.id)}},
        ).status_code == 409
        assert client.post(
            "/payments/intent",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(settled.id)}},
        ).status_code == 409
    mk.assert_not_called()


def test_intent_drops_client_metadata_and_forces_the_currency(client, db_session, me):
    """Stripe metadata rides into the webhook and some of it is read back as
    fact. An arbitrary client-controlled dict on a money object is a channel
    nobody audits — and a second `invoice_id` spelling must not survive.
    """
    mine = _invoice(db_session, me.customer_id, number="INV-PI-META")
    with patch("gdx_dispatch.routers.payments.create_payment_intent", return_value=_pi()) as mk:
        resp = client.post(
            "/payments/intent",
            json={
                "amount_cents": 20000,
                "currency": "eur",
                "metadata": {
                    "invoice_id": str(mine.id),
                    "note": "anything at all",
                    "amount": "1",
                },
            },
        )

    assert resp.status_code == 200, resp.text
    assert mk.call_args[1]["metadata"] == {"invoice_id": str(mine.id)}
    assert mk.call_args[1]["currency"] == "usd"


def test_intent_writes_the_audit_row_it_never_used_to(client, db_session, me):
    """The trail. This handler's audit block read `locals().get('db')` on a
    function that took no `db` parameter, so it was ALWAYS None and the
    endpoint has never written a row — a money path whose record-keeping was a
    silent no-op. It also logged `entity_id=""` and `details={}`.
    """
    from gdx_dispatch.core.audit import AuditLog

    mine = _invoice(db_session, me.customer_id, number="INV-PI-AUDIT")
    with patch("gdx_dispatch.routers.payments.create_payment_intent", return_value=_pi()):
        assert client.post(
            "/payments/intent",
            json={"amount_cents": 100, "metadata": {"invoice_id": str(mine.id)}},
        ).status_code == 200

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "payment_intent")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None, "the intent wrote no audit row"
    assert row.entity_id == str(mine.id)
    assert row.details["amount_cents"] == 20000
    # The disagreement is the interesting part: a client asking for an amount
    # the invoice does not owe is the signal that something is probing.
    assert row.details["client_amount_cents"] == 100


# ── the §11 rail reaches this helper at last ───────────────────────────────
#
# `core/payments.py:_resolve_public_invoice` has refused DRAFTS since the
# 2026-08-08 audit — "a machine-priced closeout autodraft nobody reviewed"
# must not take money, and its token is minted at creation. This router's
# `_require_own_unpaid_invoice` never learned it, so BOTH portal money
# endpoints it guards stayed 15 days behind the resolver next door.
#
# Found by an adversarial review of the /intent fix, which had measured
# parity against this helper and called it "the full treatment". It wasn't.


@pytest.mark.parametrize("path,payload_key", [
    ("/payments/intent", None),
    ("/payments/methods/pm_1/charge", None),
])
def test_neither_portal_money_path_will_charge_a_draft(client, db_session, me, path, payload_key):
    """A draft is not an issued bill. The office has not verified it, the
    customer has never been shown it, and on the autodraft lane the numbers
    were typed by a machine.

    404, not 409 — an un-issued invoice must not be confirmed to exist.
    """
    draft = _invoice(db_session, me.customer_id, number=f"INV-DRAFT-{path.count('/')}", status="draft")
    with patch("gdx_dispatch.routers.payments.create_payment_intent") as mk_i, \
         patch("gdx_dispatch.routers.payments.charge_saved_method") as mk_c:
        resp = client.post(
            path,
            json={"amount_cents": 100, "metadata": {"invoice_id": str(draft.id)}},
        )

    assert resp.status_code == 404, resp.text
    mk_i.assert_not_called()
    mk_c.assert_not_called()
    db_session.refresh(draft)
    assert draft.status == "draft"


def test_charge_also_drops_client_metadata_and_fixes_the_currency(client, db_session, me):
    """The endpoint the /intent fix was measured against was itself two
    changes short: it still honoured `body.currency` and still merged an
    arbitrary client metadata dict onto a Stripe money object. Parity now runs
    in both directions.
    """
    mine = _invoice(db_session, me.customer_id, number="INV-CH-META")
    with patch("gdx_dispatch.routers.payments.charge_saved_method", return_value=_intent()) as ch:
        resp = client.post(
            "/payments/methods/pm_1/charge",
            json={
                "amount_cents": 20000,
                "currency": "eur",
                "metadata": {"invoice_id": str(mine.id), "note": "anything"},
            },
        )

    assert resp.status_code == 200, resp.text
    assert ch.call_args[1]["currency"] == "usd"
    assert ch.call_args[1]["metadata"] == {"invoice_id": str(mine.id)}
